"""Turn raw signals into a board.

Buckets:
  needs   — the agent is blocked on you (permission prompt, question, plan).
  working — the agent is processing.
  inbox   — the agent finished its turn in a session that is still open.
  closed  — the session exited without being marked done.
  done    — you marked it done, or it went 2 days without activity.
Done is sticky until new activity lands on the session, which reopens it.
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import agents as agents_mod
from . import followups as followups_mod
from . import requests as requests_mod
from . import routines as routines_mod
from . import providers
from .providers.base import ENDED, NEEDS, ACTIVITY, TURN_DONE, WORKING, Runtime, Transcript
from .paths import EVENTS_FILE, STATE_FILE, ensure_dirs

STUCK_AFTER = 5 * 60
AUTO_DONE_AFTER = 2 * 86400
LOOKBACK = 14 * 86400          # transcripts older than this aren't loaded at all
DONE_VISIBLE_FOR = 5 * 86400   # done sessions drop off the board after this
DONE_MAX = 40
EVENTS_KEEP = LOOKBACK         # an event outside the transcript window can't change the board
ROTATE_EVERY = 6 * 3600

_QUESTION_PHRASES = ("should i", "want me to", "would you like", "do you want", "let me know",
                     "which would you prefer", "shall i", "ok to proceed", "does that work")


@dataclass
class HookState:
    last_event: str | None = None
    last_ts: float = 0.0
    needs_label: str | None = None
    needs_text: str = ""
    last_stop_ts: float = 0.0
    last_working_ts: float = 0.0
    ended: bool = False
    cwd: str | None = None
    permission_mode: str | None = None


def rotate_events(now: float, keep: float = EVENTS_KEEP) -> int:
    """Drop events older than `keep` seconds; returns the bytes removed.

    In place and locked, so a concurrent append is not lost with the old inode.
    """
    try:
        with open(EVENTS_FILE, "r+b") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass
            raw = f.read()
            lines = raw.splitlines(keepends=True)
            cutoff = now - keep
            cut = 0
            for i, line in enumerate(lines):
                try:
                    ts = float(json.loads(line).get("ts") or 0)
                except (ValueError, AttributeError):
                    cut = i + 1
                    continue
                if ts >= cutoff:
                    break
                cut = i + 1
            if not cut:
                return 0
            kept = b"".join(lines[cut:])
            f.seek(0)
            f.write(kept)
            f.truncate()
            return len(raw) - len(kept)
    except (OSError, ValueError):
        return 0


def events_summary() -> tuple[int, int, float]:
    """(bytes, events, span in days)."""
    try:
        raw = EVENTS_FILE.read_bytes()
    except OSError:
        return 0, 0, 0.0
    stamps = []
    for line in raw.splitlines():
        try:
            stamps.append(float(json.loads(line).get("ts") or 0))
        except (ValueError, AttributeError):
            continue
    span = (max(stamps) - min(stamps)) / 86400 if stamps else 0.0
    return len(raw), len(stamps), span


class HookTail:
    """Incrementally consume ~/.nizam/events.jsonl."""

    def __init__(self) -> None:
        self.offset = 0
        self.sessions: dict[str, HookState] = {}
        self.next_rotate = 0.0

    def poll(self) -> None:
        now = time.time()
        if now >= self.next_rotate:
            self.next_rotate = now + ROTATE_EVERY
            self.offset = max(0, self.offset - rotate_events(now))
        try:
            size = EVENTS_FILE.stat().st_size
        except OSError:
            return
        if size < self.offset:           # rotated/truncated
            self.offset = 0
            self.sessions.clear()
        if size == self.offset:
            return
        with open(EVENTS_FILE, "rb") as f:
            f.seek(self.offset)
            chunk = f.read()
        nl = chunk.rfind(b"\n")
        if nl < 0:
            return
        self.offset += nl + 1
        for raw in chunk[:nl].split(b"\n"):
            try:
                self._apply(json.loads(raw))
            except ValueError:
                continue

    def _apply(self, ev: dict) -> None:
        sid = ev.get("session_id")
        if not sid:
            return
        h = self.sessions.setdefault(sid, HookState())
        sig = providers.get(ev.get("provider")).signal(ev)
        ts = float(ev.get("ts") or 0)
        h.last_event, h.last_ts = ev.get("hook_event_name"), ts
        h.cwd = ev.get("cwd") or h.cwd
        h.permission_mode = ev.get("permission_mode") or h.permission_mode
        if sig.kind == NEEDS:
            h.needs_label, h.needs_text = sig.label, sig.text
        elif sig.kind != ACTIVITY:
            h.needs_label, h.needs_text = None, ""
        if sig.kind == TURN_DONE:
            h.last_stop_ts = ts
            h.ended = False
        if sig.kind == WORKING:
            h.last_working_ts = ts
            h.ended = False
        if sig.kind == ENDED:
            h.ended = True


class Persisted:
    """~/.nizam/state.json — the user's decisions (done flags, names)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data: dict = {"done": {}, "names": {}, "prefs": {}}
        self.load()

    def load(self) -> None:
        try:
            self.data.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        ensure_dirs()
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        os.replace(tmp, STATE_FILE)

    def mark_done(self, sid: str, by: str = "user", at: float | None = None) -> None:
        with self.lock:
            self.data["done"][sid] = {"at": at or time.time(), "by": by}
            self.save()

    def reopen(self, sid: str) -> None:
        with self.lock:
            if self.data["done"].pop(sid, None) is not None:
                self.data.setdefault("reopened", {})[sid] = time.time()
                self.save()

    def rename(self, sid: str, name: str) -> None:
        with self.lock:
            if name.strip():
                self.data["names"][sid] = name.strip()
            else:
                self.data["names"].pop(sid, None)
            self.save()

    def set_pref(self, key: str, value) -> None:
        with self.lock:
            self.data["prefs"][key] = value
            self.save()

    def ack_routine(self, routine_id: str, run_id: str) -> None:
        with self.lock:
            self.data.setdefault("routine_acks", {})[routine_id] = run_id
            self.save()

    def set_agent(self, root: str, **fields) -> None:
        with self.lock:
            a = self.data.setdefault("agents", {}).setdefault(root, {})
            for k, v in fields.items():
                if v in (None, ""):
                    a.pop(k, None)
                else:
                    a[k] = v
            if not a:
                self.data["agents"].pop(root, None)
            self.save()

    def set_agent_order(self, roots: list[str]) -> None:
        with self.lock:
            self.data["agent_order"] = [r for r in roots if isinstance(r, str)]
            self.save()


def _preview(text: str, limit: int = 320) -> str:
    """Last paragraph(s) of the assistant's message, cut at a paragraph
    boundary so it never starts mid-sentence."""
    import re
    text = re.sub(r"```.*?```", "[code]", text, flags=re.S)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    out: list[str] = []
    for p in reversed(paras):
        if out and len("\n".join(out)) + len(p) > limit:
            break
        out.insert(0, p)
        if len("\n".join(out)) >= limit:
            break
    s = "\n".join(out)
    return s if len(s) <= limit + 80 else "…" + s[-limit:]


def _looks_like_question(text: str) -> bool:
    if not text:
        return False
    tail = text.rstrip().rstrip("\"'*])")
    low = text.lower()[-300:]
    return tail.endswith("?") or any(p in low for p in _QUESTION_PHRASES)


def _title(t: Transcript, override: str | None) -> str:
    if override:
        return override
    if t.custom_title:
        return t.custom_title
    if t.summary:
        return t.summary
    p = t.first_user_prompt.strip().splitlines()
    first = p[0].strip() if p else ""
    return (first[:90] + "…") if len(first) > 90 else (first or t.session_id[:8])


def _ago(secs: float) -> str:
    secs = max(0, int(secs))
    if secs < 60: return f"{secs}s"
    if secs < 3600: return f"{secs // 60}m"
    if secs < 86400: return f"{secs // 3600}h"
    return f"{secs // 86400}d"


class Board:
    def __init__(self) -> None:
        self.hooks = HookTail()
        self.persist = Persisted()
        self._cache: tuple[float, dict] | None = None
        self._lock = threading.Lock()

    def snapshot(self, max_age: float = 2.0) -> dict:
        with self._lock:
            now = time.time()
            if self._cache and now - self._cache[0] < max_age:
                return self._cache[1]
            snap = self._build(now)
            self._cache = (now, snap)
            return snap

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None

    def _build(self, now: float) -> dict:
        self.hooks.poll()
        found: list[tuple[Transcript, Runtime | None]] = []
        for p in providers.active():
            runtimes = p.runtimes()
            found += [(t, runtimes.get(t.session_id)) for t in p.transcripts(LOOKBACK)]
        done_map: dict = self.persist.data["done"]
        reopened: dict = self.persist.data.get("reopened", {})
        names: dict = self.persist.data["names"]

        sessions = []
        agents: dict[str, dict] = {}
        routine_sessions = routines_mod.hidden_sessions()
        for t, rt in found:
            sid = t.session_id
            h = self.hooks.sessions.get(sid)
            # Launch cwd, not the hook's: a `cd` inside the session must not
            # move the card to another agent.
            cwd = (rt and rt.cwd) or t.cwd
            if cwd in ("/", str(agents_mod.HOME)) or cwd.startswith(("/tmp", "/private/tmp")):
                continue          # stray sessions launched from /, ~ or a temp dir aren't an agent
            agent, area = agents_mod.resolve(cwd)
            live = bool(rt and rt.alive)
            if sid in routine_sessions and (not live or routines_mod.is_running(routine_sessions[sid])):
                # A routine's headless run shows as the routine; it becomes a session once you resume it.
                agents.setdefault(str(agent.root), agent.to_dict())
                continue
            last_activity = max(t.mtime, t.last_ts or 0, h.last_ts if h else 0,
                                rt.updated_at if rt else 0)

            done = done_map.get(sid)
            if done and last_activity > done["at"] + 1:
                self.persist.reopen(sid)
                done = None
            if not done and now - last_activity > AUTO_DONE_AFTER and reopened.get(sid, 0) < last_activity:
                # Dated at the moment it went stale so old sessions age out of Done.
                self.persist.mark_done(sid, by="auto", at=last_activity + AUTO_DONE_AFTER)
                done = done_map.get(sid)

            bucket, label, detail = self._classify(now, t, rt, h, live, last_activity)
            if done:
                bucket, label = "done", ("Done" if done["by"] == "user" else "Auto-done")
            if bucket == "done" and now - done["at"] > DONE_VISIBLE_FOR:
                continue

            preview = t.last_assistant_text.strip()
            a_key = str(agent.root)
            agents.setdefault(a_key, agent.to_dict())
            sessions.append({
                "id": sid,
                "provider": t.provider,
                "title": _title(t, names.get(sid)),
                "auto_title": _title(t, None),
                "agent": a_key,
                "agent_name": agent.name,
                "area": area.rel if area else None,
                "cwd": cwd,
                "bucket": bucket,
                "label": label,
                "detail": detail,
                "live": live,
                "pid": rt.pid if rt else None,
                "runtime_status": rt.status if rt else None,
                "last_activity": last_activity,
                "ago": _ago(now - last_activity),
                "started": t.first_ts or t.mtime,
                "last_prompt": t.last_user_prompt[:300],
                "preview": _preview(preview),
                "question": _looks_like_question(preview) if bucket in ("inbox", "closed") else False,
                "done": done,
                "hooked": h is not None,
            })
        order = {"needs": 0, "working": 1, "inbox": 2, "closed": 3, "done": 4}
        sessions.sort(key=lambda s: (order[s["bucket"]], -s["last_activity"]))
        done_seen = 0
        kept = []
        for s in sessions:
            if s["bucket"] == "done":
                done_seen += 1
                if done_seen > DONE_MAX:
                    continue
            kept.append(s)
        sessions = kept
        overrides = self.persist.data.get("agents", {})
        # Pinned agents stay on the board even with no recent sessions.
        for root, o in overrides.items():
            if o.get("pinned") and root not in agents and Path(root).is_dir():
                agents[root] = agents_mod.load_agent(Path(root)).to_dict()
        requests = requests_mod.board_rows(now)
        for q in requests:
            if q["agent"] not in agents and Path(q["agent"]).is_dir():
                agents[q["agent"]] = agents_mod.load_agent(Path(q["agent"])).to_dict()
        requests = [q for q in requests if q["agent"] in agents]
        followups: list[dict] = []
        routines: list[dict] = []
        acks = self.persist.data.get("routine_acks", {})
        for root in agents:
            loaded = agents_mod.load_agent(Path(root))
            followups.extend(followups_mod.for_agent(loaded))
            routines.extend(routines_mod.board_rows(loaded, acks))
        for a in agents.values():
            o = overrides.get(a["root"], {})
            a["display_name"] = o.get("name") or a["name"]
            a["pinned"] = bool(o.get("pinned"))
        for s in sessions:
            s["agent_name"] = agents[s["agent"]]["display_name"]
        return {"generated_at": now, "agents": sorted(agents.values(), key=lambda a: a["display_name"].lower()),
                "sessions": sessions, "followups": followups, "routines": routines, "requests": requests, "prefs": self.persist.data["prefs"],
                "agent_order": self.persist.data.get("agent_order", [])}

    @staticmethod
    def _classify(now, t: Transcript, rt: Runtime | None, h: HookState | None, live: bool,
                  last_activity: float) -> tuple[str, str, str]:
        silent = now - last_activity
        if live and h and h.needs_label:
            return "needs", h.needs_label, h.needs_text
        if live and not h and t.pending_label:
            return "needs", t.pending_label, ""
        busy = bool(rt and rt.status == "busy")
        if not rt and h and not h.ended and h.last_working_ts > h.last_stop_ts:
            busy = True
        if not rt and not h and t.last_role == "user" and silent < STUCK_AFTER:
            busy = True
        if busy:
            if silent > STUCK_AFTER:
                return "needs", "Maybe stuck", f"No progress for {_ago(silent)}"
            return "working", "Working", ""
        if live:
            return "inbox", "Turn finished", ""
        return "closed", "Closed", ""
