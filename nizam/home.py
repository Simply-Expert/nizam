"""Home: what is fresh and waits on the person as one ordered queue, with what routines did lately and what comes next.

Follow-ups and requests can sit for days, so they live on their own page, not here."""
from __future__ import annotations

from datetime import datetime, timedelta

from .routines import FAILED
from .triage import waiting

RECENT_SECS = 86400
RECENT_MAX = 12
UPCOMING_ROUTINES = 3
STATES = ("failing", "found", "running", "recovered", "handled")
BLOCKED, ROUTINE, YOUR_TURN = range(3)


def _first_line(text: str) -> str:
    return next((l.strip() for l in (text or "").splitlines() if l.strip()), "")


def _queue(snap: dict, tri: dict) -> list[dict]:
    pos = {i["id"]: n for n, i in enumerate(tri.get("items", []))} if tri.get("enabled") else {}
    rows: list[tuple[tuple, dict]] = []
    for s in waiting(snap):
        tier = BLOCKED if s["bucket"] == "needs" else YOUR_TURN
        rows.append(((not s.get("starred"), tier, pos.get(s["id"], len(pos)), -s["last_activity"]),
                     {"id": s["id"], "kind": "session"}))
    for r in snap.get("routines", []):
        if r["failing"] or r["attention"]:
            rows.append(((True, ROUTINE, not r["failing"], r["name"]), {"id": r["id"], "kind": "routine"}))
    return [item for _, item in sorted(rows, key=lambda r: r[0])]


def _group(status: str | None) -> str | None:
    if status in FAILED:
        return "failed"
    return {"no_change": "quiet", "completed": "ok", "needs_attention": "found", "running": "running"}.get(status or "")


def _recent(snap: dict, runs: list[dict], now: float) -> dict:
    """The last day of routine runs as where each routine stands now, not as a log of every run."""
    by_id = {r["id"]: r for r in snap.get("routines", [])}
    counts = {"total": 0, "quiet": 0, "ok": 0, "found": 0, "failed": 0, "running": 0}
    per: dict[str, list[tuple[float, str, dict]]] = {}
    for run in sorted(runs, key=lambda r: r.get("started") or 0, reverse=True):
        rid = "r:" + str(run.get("routine", ""))
        started, group = run.get("started") or 0, _group(run.get("status"))
        if rid not in by_id or not group or started < now - RECENT_SECS or started > now:
            continue
        counts["total"] += 1
        counts[group] += 1
        per.setdefault(rid, []).append((started, group, run))
    notable, clean = [], []
    for rid, seen in per.items():
        r = by_id[rid]
        base = {"id": rid, "name": r["name"], "agent": r["agent"], "runs": len(seen)}
        failed = next((x for x in seen if x[1] == "failed"), None)
        found = next((x for x in seen if x[1] == "found"), None)
        if seen[0][1] == "failed":
            state, src = "failing", seen[0]
        elif found and r.get("attention"):
            state, src = "found", found
        elif seen[0][1] == "running":
            state, src = "running", seen[0]
        elif failed:
            state, src = "recovered", failed
        elif found:
            state, src = "handled", found
        else:
            clean.append({**base, "at": seen[0][0]})
            continue
        notable.append({**base, "state": state, "at": src[0], "since": seen.index(src),
                        "summary": _first_line(src[2].get("summary", ""))[:200],
                        "open": bool(r.get("failing") or r.get("attention"))})
    notable.sort(key=lambda i: (STATES.index(i["state"]), -i["at"]))
    clean.sort(key=lambda i: i["name"].lower())
    return {**counts, "routines": len(per), "notable": notable[:RECENT_MAX], "clean": clean}


def _upcoming(snap: dict, now: float) -> tuple[list[dict], int]:
    """Sessions snoozed to come back, and only the next few routine runs before the end of tomorrow."""
    day = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    end = (day + timedelta(days=2)).timestamp()
    snoozed = [{"id": s["id"], "kind": "session", "agent": s["agent"], "title": s["title"], "at": s["remind_at"],
                "when": s["remind_label"]} for s in snap["sessions"] if s["bucket"] == "later"]
    routines = sorted(({"id": r["id"], "kind": "routine", "agent": r["agent"], "title": r["name"], "at": r["next_at"],
                        "when": r["next"]} for r in snap.get("routines", [])
                       if r.get("next_at") and now < r["next_at"] < end), key=lambda u: (u["at"], u["title"]))
    out = snoozed + routines[:UPCOMING_ROUTINES]
    out.sort(key=lambda u: (u["at"], u["title"]))
    return out, max(0, len(routines) - UPCOMING_ROUTINES)


def build(snap: dict, tri: dict, runs: list[dict], now: float) -> dict:
    upcoming, more = _upcoming(snap, now)
    return {"queue": _queue(snap, tri), "recent": _recent(snap, runs, now), "upcoming": upcoming,
            "upcoming_more": more}
