"""A fake ~/.claude and ~/.nizam on disk, in the formats Claude Code really writes."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nizam import cli, state
from nizam.providers import claude


def point_at(home: Path) -> list:
    dot = home / ".claude"
    (home / ".nizam").mkdir(exist_ok=True)
    return [mock.patch.object(claude, "CLAUDE_PROJECTS", dot / "projects"),
            mock.patch.object(claude, "CLAUDE_SESSIONS", dot / "sessions"),
            mock.patch.object(claude, "CLAUDE_SETTINGS", dot / "settings.json"),
            mock.patch.object(claude, "SKILL_DST", dot / "skills" / "nizam" / "SKILL.md"),
            mock.patch.object(state, "EVENTS_FILE", home / ".nizam" / "events.jsonl"),
            mock.patch.object(state, "STATE_FILE", home / ".nizam" / "state.json"),
            mock.patch.object(state.requests_mod, "board_rows", lambda now: []),
            mock.patch.object(state.routines_mod, "hidden_sessions", lambda: {}),
            mock.patch.object(state.routines_mod, "board_rows", lambda agent, acks: []),
            mock.patch.object(state.followups_mod, "for_agent", lambda agent: [])]


def uninstall_hooks() -> None:
    from nizam import launch, routines
    with mock.patch.object(launch, "login_enabled", lambda: False), \
         mock.patch.object(routines, "remove_all", lambda: 0):
        cli.uninstall_hooks()


def read_transcript(path: Path):
    return claude.read_transcript(path)


def read_runtimes() -> dict:
    return claude.read_runtimes()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def user(text, ts: float, cwd: str, **extra) -> dict:
    return {"type": "user", "cwd": cwd, "timestamp": iso(ts), "message": {"role": "user", "content": text}, **extra}


def assistant(text: str, ts: float, cwd: str, tools: tuple = (), **extra) -> dict:
    content = ([{"type": "text", "text": text}] if text else []) + \
              [{"type": "tool_use", "name": t, "input": {}} for t in tools]
    return {"type": "assistant", "cwd": cwd, "timestamp": iso(ts),
            "message": {"role": "assistant", "content": content}, **extra}


def write_transcript(home: Path, sid: str, cwd: str, entries: list, mtime: float | None = None) -> Path:
    d = home / ".claude" / "projects" / cwd.replace("/", "-")
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def write_runtime(home: Path, sid: str, cwd: str, pid: int, status: str = "idle", updated: float | None = None) -> None:
    d = home / ".claude" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "sessionId": sid, "cwd": cwd, "status": status,
        "statusUpdatedAt": int((updated or time.time()) * 1000)}), encoding="utf-8")


def append_event(home: Path, sid: str, name: str, ts: float, **fields) -> None:
    f = home / ".nizam" / "events.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a", encoding="utf-8") as out:
        out.write(json.dumps({"session_id": sid, "hook_event_name": name, "ts": ts, **fields}) + "\n")


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid
