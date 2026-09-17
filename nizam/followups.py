"""Read-only view of each agent's FOLLOWUPS.md.

The file stays the source of truth and the agent stays its editor; Nizam
only parses `## YYYY-MM-DD (Day) — [area] Title` sections and their body.
The root file is the default queue, where an optional [area] tag says which
area an item belongs to; a self-contained area may keep its own file.
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import date
from pathlib import Path

FILENAME = "FOLLOWUPS.md"
_TAG = re.compile(r"^\[([^\]]+)\]\s*(.*)$")
_HEAD = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})(?:\s*\([^)]*\))?\s*(?:[—–-]+\s*)?(.*)$")
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
            cur = {"date": m.group(1), "title": m.group(2).strip() or "(untitled)", "body": []}
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


def for_agent(agent) -> list[dict]:
    """Follow-ups at the agent root and inside each of its areas."""
    today = date.today()
    out: list[dict] = []
    places = [(None, agent.root)] + [(a.rel, a.path) for a in agent.areas.values()]
    for rel, folder in places:
        for it in _parse(Path(folder) / FILENAME):
            try:
                due = date.fromisoformat(it["date"])
            except ValueError:
                continue
            days = (due - today).days
            title, area_rel, cwd = it["title"], rel, folder
            m = _TAG.match(title)
            if m:
                area = _resolve_tag(agent, m.group(1))
                if area is not None:
                    title, area_rel, cwd = m.group(2).strip() or title, area.rel, area.path
            fid = hashlib.md5(f"{agent.root}|{rel}|{it['date']}|{it['title']}".encode()).hexdigest()[:12]
            out.append({
                "id": "f:" + fid, "agent": str(agent.root), "area": area_rel,
                "cwd": str(cwd), "file": str(Path(folder) / FILENAME),
                "date": it["date"], "title": title, "body": it["body"],
                "days": days, "due": "overdue" if days < 0 else "today" if days == 0 else "upcoming",
            })
    out.sort(key=lambda f: f["date"])
    return out
