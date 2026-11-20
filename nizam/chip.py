"""The team chip: a full-width capsule with one segment per agent that has something going on.

Agents keep the board's order (pinned, then the order you dragged them into), never the order of
urgency, so each one stays where your eye expects it.
"""
from __future__ import annotations

import hashlib

PALETTE = ["#c2410c", "#7c3aed", "#059669", "#be185d", "#2563eb", "#0e7490", "#a16207", "#4f46e5", "#b91c1c", "#0f766e"]
STATES = ("needs", "working", "inbox", "quiet")
ROWS_MAX = 10
FOLLOWUPS_MAX = 4


def initials(name: str) -> str:
    words = [w for w in name.replace("-", " ").replace("_", " ").split() if w]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return (words[0][:2] if words else "?").upper()


def color(root: str) -> str:
    return PALETTE[int(hashlib.md5(root.encode()).hexdigest(), 16) % len(PALETTE)]


def _order(snap: dict) -> list[dict]:
    rank = {r: i for i, r in enumerate(snap.get("agent_order", []))}
    return sorted(snap["agents"], key=lambda a: (not a.get("pinned"), rank.get(a["root"], len(rank)),
                                                 a["display_name"].lower()))


def _open_followups(snap: dict, root: str) -> list[dict]:
    return [f for f in snap.get("followups", []) if f["agent"] == root and not f.get("in_session")]


def agents(snap: dict) -> list[dict]:
    out = []
    for a in _order(snap):
        root = a["root"]
        live = [s for s in snap["sessions"] if s["agent"] == root and s["bucket"] in ("needs", "working", "inbox")]
        broken = [r for r in snap.get("routines", []) if r["agent"] == root and (r["failing"] or r["attention"])]
        due = sum(f["due"] != "upcoming" for f in _open_followups(snap, root))
        first = {b: next((s for s in live if s["bucket"] == b), None) for b in ("needs", "working", "inbox")}
        if first["needs"]:
            state, status = "needs", f"{first['needs']['label']} · {first['needs']['ago']}"
        elif broken:
            r = broken[0]
            state, status = "needs", ("Routine " + r["status"].replace("_", " ")) if r["failing"] else "Routine found something"
        elif first["working"]:
            state, status = "working", first["working"]["label"]
        elif first["inbox"]:
            state, status = "inbox", f"Your turn · {first['inbox']['ago']}"
        else:
            state, status = "quiet", f"{due} follow-up{'s' * (due != 1)} due" if due else ""
        if state == "quiet" and not due and not a.get("pinned"):
            continue
        out.append({"root": root, "name": a["display_name"], "initials": initials(a["display_name"]),
                    "color": color(root), "state": state, "status": status, "due": due,
                    "compact": state == "quiet"})
    return out


def rows(snap: dict, root: str, cards: dict | None = None) -> list[dict]:
    """What hovering an agent lists: its open sessions (a closed one only when starred), routines that
    need a look, then follow-ups that are due or late."""
    cards = cards or {}
    out = []
    for s in snap["sessions"]:
        if s["agent"] != root or s["bucket"] == "done" or (s["bucket"] == "closed" and not s["starred"]):
            continue
        c = cards.get(s["id"])
        title = c["headline"] if c else s["title"]
        out.append({"id": s["id"], "session": s, "icon": s["bucket"], "title": f"★ {title}" if s["starred"] else title,
                    "sub": c["needs"] if c and s["bucket"] == "needs" else f"{s['label']} · {s['ago']}"})
    for r in snap.get("routines", []):
        if r["agent"] == root and (r["failing"] or r["attention"]):
            why = "Routine " + r["status"].replace("_", " ") if r["failing"] else "Routine found something"
            out.append({"id": r["id"], "icon": "routine", "title": r["name"], "sub": why})
    fus = sorted((f for f in _open_followups(snap, root) if f["due"] != "upcoming"), key=lambda f: f["at"])
    for f in fus[:FOLLOWUPS_MAX]:
        out.append({"id": f["id"], "icon": "followup", "title": f["title"], "sub": f"Follow-up · {f['when']}"})
    more = len(out) - ROWS_MAX
    if more > 0:
        out = out[:ROWS_MAX] + [{"id": None, "title": f"+{more} more", "sub": "Open the board"}]
    return out

