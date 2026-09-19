"""The agentic CLIs Nizam can sit on top of. Only Claude Code, for now."""
from __future__ import annotations

import os

from .base import Provider
from .claude import Claude

_REGISTRY: dict[str, Provider] = {p.name: p for p in (Claude(),)}
DEFAULT = "claude"


def get(name: str | None = None) -> Provider:
    return _REGISTRY.get(name or DEFAULT) or _REGISTRY[DEFAULT]


def active() -> list[Provider]:
    return list(_REGISTRY.values())


def clean_env() -> dict[str, str]:
    env = dict(os.environ)
    for p in active():
        env = p.clean_env(env)
    return env


def current_session_id() -> str | None:
    return next((sid for p in active() if (sid := p.session_id(dict(os.environ)))), None)
