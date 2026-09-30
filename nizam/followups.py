"""Read-only view of each agent's FOLLOWUPS.md.

The file stays the source of truth and the agent stays its editor; Nizam
only parses `## YYYY-MM-DD (Day) HH:MM — [area] Title` sections and their body,
the time being optional.
The root file is the default queue, where an optional [area] tag says which
area an item belongs to; a self-contained area may keep its own file.
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import reminders

FILENAME = "FOLLOWUPS.md"
DEFAULT_HOUR = (10, 0)
_TAG = re.compile(r"^\[([^\]]+)\]\s*(.*)$")
_HEAD = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})(?:\s*\([^)]*\))?"
                   r"(?:\s+(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?|\d{1,2}:\d{2})(?![\w:]))?"
                   r"\s*(?:[—–-]+\s*)?(.*)$", re.IGNORECASE)
_cache: dict[Path, tuple[float, list[dict]]] = {}


def _parse(path: Path) -> list[dict]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    items: list[dict] = []
    cur: dict | None = None
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    for line in lines:
        m = _HEAD.match(line)
        if m:
            try:
                hm = reminders.clock(m.group(2).lower()) if m.group(2) else None
            except ValueError:
                hm = None
            cur = {"date": m.group(1), "time": hm, "title": m.group(3).strip() or "(untitled)", "body": []}
            items.append(cur)
            continue
        if line.startswith("## ") or line.strip() == "---" or line.startswith("# "):
            cur = None
            continue
        if cur is not None:
            cur["body"].append(line)
    for it in items:
        it["body"] = "\n".join(it["body"]).strip()
    _cache[path] = (mtime, items)
    return items


def _resolve_tag(agent, tag: str):
    """Area whose rel path or folder name equals the tag (case-insensitive)."""
    t = tag.strip().strip("/").lower()
    for a in agent.areas.values():
        if a.rel.lower() == t:
            return a
    named = [a for a in agent.areas.values() if a.name.lower() == t]
    return named[0] if len(named) == 1 else None


def default_hour(prefs: dict) -> tuple[int, int]:
    """When follow-ups without a time of their own come up."""
    try:
        return reminders.clock(str(prefs.get("followup_hour", "")).lower())
    except ValueError:
        return DEFAULT_HOUR


def _when(days: int, due: str, clock: str, timed: bool) -> str:
    day = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(days) or (f"{-days}d late" if days < 0 else f"in {days}d")
    if abs(days) <= 1 and (timed or (days == 0 and due == "upcoming")):
        day += " " + clock
    return day


def for_agent(agent, hour: tuple[int, int] = DEFAULT_HOUR, now: float | None = None) -> list[dict]:
    """Follow-ups at the agent root and inside each of its areas.

    An item comes up at its own time, or at `hour` when it has none, and is
    late once the next day's untimed items come up.
    """
    now_dt = datetime.fromtimestamp(time.time() if now is None else now)
    today = now_dt.date()
    out: list[dict] = []
    places = [(None, agent.root)] + [(a.rel, a.path) for a in agent.areas.values()]
    for rel, folder in places:
        for it in _parse(Path(folder) / FILENAME):
            try:
                day = date.fromisoformat(it["date"])
            except ValueError:
                continue
            days = (day - today).days
            h, m = it["time"] or hour
            at = datetime(day.year, day.month, day.day, h, m)
            nxt = day + timedelta(days=1)
            late = datetime(nxt.year, nxt.month, nxt.day, *hour)
            due = "overdue" if now_dt >= late else "today" if now_dt >= at else "upcoming"
            clock = at.strftime("%I:%M %p").lstrip("0")
            title, area_rel, cwd = it["title"], rel, folder
            tag = _TAG.match(title)
            if tag:
                area = _resolve_tag(agent, tag.group(1))
                if area is not None:
                    title, area_rel, cwd = tag.group(2).strip() or title, area.rel, area.path
            fid = hashlib.md5(f"{agent.root}|{rel}|{it['date']}|{it['title']}".encode()).hexdigest()[:12]
            out.append({
                "id": "f:" + fid, "agent": str(agent.root), "area": area_rel,
                "cwd": str(cwd), "file": str(Path(folder) / FILENAME),
                "date": it["date"], "time": f"{h:02d}:{m:02d}" if it["time"] else None,
                "at": at.timestamp(), "clock": clock,
                "title": title, "body": it["body"], "days": days, "due": due,
                "when": _when(days, due, clock, bool(it["time"])),
            })
    out.sort(key=lambda f: f["at"])
    return out
