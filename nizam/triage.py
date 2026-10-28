"""Check first: what each waiting session needs from you, in the order to look at them; off unless the
`triage` pref is on, since every card is a call on the user's Claude plan."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time

from . import providers
from .paths import NIZAM_DIR
from .runner import login_path

STATE_FILE = NIZAM_DIR / "triage.json"
CARD_MODEL = "haiku"
RANK_MODEL = "sonnet"
TICK_SECS = 5.0
SETTLE_SECS = 10.0      # a reply this soon means the person is on it; no card needed
BATCH = 6
TAIL_CHARS = 5000
CALL_TIMEOUT = 180
RETRY_SECS = 120
PATH_TTL = 600
WAITING = ("needs", "inbox")

CARD_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["cards"],
    "properties": {"cards": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["id", "headline", "needs", "kind", "effort", "stakes", "options"],
        "properties": {
            "id": {"type": "string"},
            "headline": {"type": "string"},
            "needs": {"type": "string"},
            "kind": {"enum": ["decide", "approve", "answer", "review", "unblock", "fyi"]},
            "effort": {"enum": ["quick", "read", "deep"]},
            "stakes": {"enum": ["high", "normal", "low"]},
            "options": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        }}}},
}
CARD_PROMPT = """You triage Claude Code sessions for one busy person who runs many in parallel and switches between them.
You get several sessions that have stopped and wait for them, each with its id, agent, title and the end of its
conversation. For every session return one card saying what it needs from the person, so they can decide whether
to open it now.

- id: the session's id, exactly as given.
- headline: the session's topic in at most 7 words. Not the first prompt; what the work is about.
- needs: one plain sentence, starting with a verb, of what the person must do or decide. Name the concrete choice
  (e.g. "Choose whether to release 10.30 now or wait for QA"). If the session only reported and asks nothing,
  say "Nothing; it reported X."
- kind: decide (pick between options), approve (say yes to a proposed action), answer (give facts only they have),
  review (read output before it goes on), unblock (it cannot continue: credentials, permission, access), fyi.
- effort: quick (a reply under a minute), read (a few minutes of reading), deep (needs focus or own work).
- stakes: high if production, money, customers, deadlines or other people/agents wait on it; low if housekeeping.
- options: up to 3 short replies the person could type as-is (at most 6 words each), only when the session offers
  or implies clear choices. Otherwise empty."""

RANK_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["order"],
    "properties": {
        "order": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "why"],
            "properties": {"id": {"type": "string"}, "why": {"type": "string"}}}},
    },
}
RANK_PROMPT = """You are the chief of staff for one person running many Claude Code agents at once.
You get cards for every session waiting on them. Put them in the order they should be checked.
Favour, roughly in this order: sessions blocked on something only the person can give; high stakes;
quick replies that unblock a lot of work (clear them before deep reads); then age.
- order: every id exactly once, with why: at most 12 words on why it sits there."""


def enabled(prefs: dict) -> bool:
    return bool(prefs.get("triage"))


def waiting(snap: dict) -> list[dict]:
    """Sessions whose next move is the person's: live ones that finished a turn, and anything blocked."""
    return [s for s in snap["sessions"] if s["bucket"] in WAITING and (s["live"] or s["bucket"] == "needs")]


_env: tuple[float, dict] | None = None


def _call_env() -> dict:
    # launchd gives the app a bare PATH that misses where the CLI lives.
    global _env
    if not _env or time.time() - _env[0] > PATH_TTL:
        env = providers.clean_env()
        env["PATH"] = login_path()
        _env = (time.time(), env)
    return _env[1]


def _ask(system: str, schema: dict, text: str, model: str) -> dict | None:
    p = providers.get()
    env = _call_env()
    exe = shutil.which(p.cli, path=env["PATH"])
    argv = p.structured_command(exe, system, schema) if exe else None
    if not argv:
        sys.stderr.write(f"triage: no {p.cli} command to ask\n")
        return None
    if "--model" in argv:
        argv[argv.index("--model") + 1] = model
    try:
        r = subprocess.run(argv, input=text, capture_output=True, text=True, timeout=CALL_TIMEOUT,
                           env=env, cwd=str(NIZAM_DIR))
    except (OSError, subprocess.SubprocessError) as e:
        sys.stderr.write(f"triage: {model} call failed: {e}\n")
        return None
    out = p.structured_result(r.stdout)
    if out is None:
        sys.stderr.write(f"triage: {model} gave no answer (exit {r.returncode}): {(r.stderr or r.stdout)[-200:]}\n")
    return out


def _where(s: dict) -> str:
    return s["agent_name"] + (f" / {s['area']}" if s["area"] else "")


class Triage:
    def __init__(self, board):
        self.board = board
        self.lock = threading.Lock()
        self.cards: dict[str, dict] = {}     # session id -> {"at": its last_activity, "card": {...}}
        self.rank: dict | None = None        # newest {"key", "order"}
        self.applied: dict | None = None     # the order on screen: {"order"}
        self.building: set[str] = set()
        self.tried: dict[str, tuple[float, float]] = {}   # session id -> (its last_activity, when we asked)
        self._load()

    def _load(self) -> None:
        try:
            d = json.loads(STATE_FILE.read_text())
        except (OSError, ValueError):
            return
        self.cards, self.rank, self.applied = d.get("cards") or {}, d.get("rank"), d.get("applied")

    def _save(self) -> None:
        tmp = STATE_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"cards": self.cards, "rank": self.rank, "applied": self.applied},
                                      ensure_ascii=False))
            os.replace(tmp, STATE_FILE)
        except OSError as e:
            sys.stderr.write(f"triage: could not save: {e}\n")

    def _fresh(self, s: dict) -> bool:
        c = self.cards.get(s["id"])
        return bool(c) and c["at"] == s["last_activity"]

    def _failed_lately(self, s: dict, now: float) -> bool:
        at, asked = self.tried.get(s["id"], (None, 0.0))
        return at == s["last_activity"] and now - asked < RETRY_SECS

    def tick(self, now: float | None = None) -> None:
        if not enabled(self.board.persist.data["prefs"]):
            return
        now = now or time.time()
        snap = self.board.snapshot()
        live = waiting(snap)
        due = [s for s in live if not self._fresh(s) and now - s["last_activity"] >= SETTLE_SECS
               and not self._failed_lately(s, now)]
        for i in range(0, len(due), BATCH):
            self._card(due[i:i + BATCH], now)
        with self.lock:
            # A card outlives a busy spell, so the session comes back showing it while the new one is made.
            ids = {s["id"] for s in live}
            on_board = {s["id"] for s in snap["sessions"]}
            self.cards = {k: v for k, v in self.cards.items() if k in on_board}
            ready = [s for s in live if s["id"] in self.cards]
            settling = any(not self._fresh(s) and not self._failed_lately(s, now) for s in live)
        if not settling:
            self._rank(ready)
        with self.lock:
            # Holding an order steady only matters while something it placed is still on screen.
            if self.rank and not any(o["id"] in ids for o in (self.applied or {}).get("order", [])):
                self.applied = {"order": self.rank["order"]}
            self._save()

    def _card(self, batch: list[dict], now: float) -> None:
        with self.lock:
            self.building |= {s["id"] for s in batch}
            self.tried.update({s["id"]: (s["last_activity"], now) for s in batch})
        p = providers.get()
        text = "\n\n".join(f"=== SESSION {s['id']} ===\nAgent: {_where(s)}\nTitle: {s['title']}\n"
                           f"State: {s['label']}, {s['ago']} ago"
                           + (f"\nBlocked on: {s['detail']}" if s["bucket"] == "needs" and s["detail"] else "")
                           + f"\n\nConversation, newest last:\n{p.conversation_tail(s['id'], TAIL_CHARS)}"
                           for s in batch)
        got = _ask(CARD_PROMPT, CARD_SCHEMA, text, CARD_MODEL) or {}
        by_id = {c.get("id"): c for c in got.get("cards") or [] if isinstance(c, dict)}
        with self.lock:
            for s in batch:
                c = by_id.get(s["id"])
                if c:
                    self.cards[s["id"]] = {"at": s["last_activity"], "card": {k: v for k, v in c.items() if k != "id"}}
            self.building -= {s["id"] for s in batch}
        if len(by_id) < len(batch):
            sys.stderr.write(f"triage: {len(batch) - len(by_id)} of {len(batch)} cards came back empty\n")

    @staticmethod
    def _key(ready: list[dict], cards: dict) -> list:
        return sorted([s["id"], s["bucket"], *(cards[s["id"]]["card"].get(k) for k in ("kind", "effort", "stakes"))]
                      for s in ready)

    def _rank(self, ready: list[dict]) -> None:
        with self.lock:
            key = self._key(ready, self.cards)
            if self.rank and self.rank["key"] == key:
                return
            listing = "\n".join(json.dumps({"id": s["id"], "agent": _where(s), "state": s["label"],
                                            "waiting_for": s["ago"], **self.cards[s["id"]]["card"]},
                                           ensure_ascii=False) for s in ready)
        r = _ask(RANK_PROMPT, RANK_SCHEMA, listing, RANK_MODEL) if ready else {"order": []}
        if r is None:
            return
        with self.lock:
            self.rank = {"key": key, "order": r.get("order") or []}
            if self.applied is None:
                self.applied = {"order": self.rank["order"]}

    def apply(self) -> None:
        with self.lock:
            if self.rank:
                self.applied = {"order": self.rank["order"]}
                self._save()

    def view(self, snap: dict) -> dict:
        if not enabled(snap["prefs"]):
            return {"enabled": False}
        with self.lock:
            live = waiting(snap)
            ids = {s["id"] for s in live}
            shown_rank = self.applied or {"order": []}
            pos = {o["id"]: i for i, o in enumerate(shown_rank["order"])}
            why = {o["id"]: o.get("why", "") for o in shown_rank["order"]}
            live.sort(key=lambda s: (pos.get(s["id"], len(pos)), s["bucket"] != "needs", -s["last_activity"]))
            shown = [s["id"] for s in live]
            proposed = [o["id"] for o in (self.rank or {}).get("order", []) if o["id"] in ids]
            items = []
            for s in live:
                c = self.cards.get(s["id"])
                items.append({"id": s["id"], "card": c and c["card"], "why": why.get(s["id"], ""),
                              "new": bool(pos) and s["id"] not in pos, "stale": bool(c) and c["at"] != s["last_activity"],
                              "building": s["id"] in self.building})
            return {"enabled": True, "items": items,
                    "reorder": proposed != [i for i in shown if i in proposed] or any(i not in pos for i in proposed)}

    def loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as e:  # keep the worker alive no matter what
                sys.stderr.write(f"triage: {e!r}\n")
            time.sleep(TICK_SECS)


_triage: Triage | None = None


def start(board) -> Triage:
    global _triage
    if _triage is None:
        _triage = Triage(board)
        threading.Thread(target=_triage.loop, daemon=True).start()
    return _triage


def get() -> Triage | None:
    return _triage
