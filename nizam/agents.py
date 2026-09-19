"""Resolve Agents and Areas from the filesystem.

An Agent is the nearest ancestor of a session's cwd that carries an agent
marker (CLAUDE.md, AGENTS.md or a .claude dir), else the cwd itself. An Area is any descendant folder of that agent that contains
INSTRUCTIONS.md; areas nest, and a session belongs to the deepest area
containing its cwd. A folder with neither marker is not an area (data,
scripts, shared ...).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

AGENT_MARKERS = ("CLAUDE.md", "AGENTS.md")
AREA_MARKER = "INSTRUCTIONS.md"
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist",
             "build", ".next", "archive", ".archive", "data", "cache"}
MAX_AREA_DEPTH = 4
HOME = Path.home()


@dataclass
class Area:
    path: Path
    rel: str                 # relative to the agent root, e.g. "channels/email"
    name: str                # last path component
    depth: int               # 0 for a top-level area
    parent: str | None       # rel of the parent area, if nested inside one
    children: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"path": str(self.path), "rel": self.rel, "name": self.name,
                "depth": self.depth, "parent": self.parent, "children": self.children}


@dataclass
class Agent:
    root: Path
    name: str
    areas: dict[str, Area]
    git: bool = False

    def area_for(self, cwd: Path) -> Area | None:
        """Deepest area whose folder contains cwd, or None (agent root / non-area folder)."""
        best: Area | None = None
        for a in self.areas.values():
            if cwd == a.path or a.path in cwd.parents:
                if best is None or a.depth > best.depth:
                    best = a
        return best

    def to_dict(self) -> dict:
        return {"root": str(self.root), "name": self.name,
                "display": display_path(self.root), "git": self.git,
                "areas": [a.to_dict() for a in sorted(self.areas.values(), key=lambda a: a.rel)]}


def display_path(p: Path) -> str:
    s = str(p)
    h = str(HOME)
    return "~" + s[len(h):] if s.startswith(h) else s


def _is_agent_dir(p: Path) -> bool:
    if any((p / m).is_file() for m in AGENT_MARKERS):
        return True
    dot = p / ".claude"
    if dot.is_dir():
        # Claude drops settings.local.json wherever you grant a permission;
        # only a deliberately configured .claude folder marks an agent.
        return any(c.name != "settings.local.json" for c in dot.iterdir())
    return False


def find_agent_root(cwd: Path) -> Path:
    """Nearest ancestor (inclusive) carrying an agent marker, stopping at
    $HOME. A folder with no marker anywhere above it is its own agent:
    the folder you launched Claude in is the role you were talking to."""
    cwd = Path(cwd)
    for p in (cwd, *cwd.parents):
        if p == HOME or p == p.parent:
            break
        if _is_agent_dir(p):
            return p
    return cwd


def _scan_areas(root: Path) -> dict[str, Area]:
    areas: dict[str, Area] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        rel_parts = d.relative_to(root).parts
        dirnames[:] = [n for n in dirnames
                       if n not in SKIP_DIRS and not n.startswith(".")]
        if len(rel_parts) >= MAX_AREA_DEPTH:
            dirnames[:] = []
        if d != root and AREA_MARKER in filenames:
            rel = "/".join(rel_parts)
            parent = None
            for i in range(len(rel_parts) - 1, 0, -1):
                cand = "/".join(rel_parts[:i])
                if cand in areas:
                    parent = cand
                    break
            areas[rel] = Area(path=d, rel=rel, name=d.name,
                              depth=0 if parent is None else areas[parent].depth + 1,
                              parent=parent)
            if parent:
                areas[parent].children.append(rel)
    return areas


def _in_git_repo(root: Path) -> bool:
    return any((p / ".git").exists() for p in (root, *root.parents))


@lru_cache(maxsize=256)
def _load_agent(root_str: str, _mtime_bucket: int) -> Agent:
    root = Path(root_str)
    return Agent(root=root, name=root.name or "/", areas=_scan_areas(root), git=_in_git_repo(root))


def load_agent(root: Path) -> Agent:
    # Re-scan at most every 30s per agent; markers change rarely.
    import time
    return _load_agent(str(root), int(time.time() // 30))


def resolve(cwd: str | Path) -> tuple[Agent, Area | None]:
    cwd = Path(cwd)
    agent = load_agent(find_agent_root(cwd))
    return agent, agent.area_for(cwd)
