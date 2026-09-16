"""Read what Claude Code leaves on disk.

Two sources:
  * ~/.claude/sessions/<pid>.json — live runtime status per running
    process (sessionId, cwd, name, status busy|idle). Authoritative for
    "is Claude processing right now".
  * ~/.claude/projects/<slug>/<sessionId>.jsonl — transcripts. Give us
    cwd, timestamps, the last user prompt and the last assistant text.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .paths import CLAUDE_PROJECTS, CLAUDE_SESSIONS

TAIL_BYTES = 256 * 1024
INTERACTIVE_TOOLS = {"AskUserQuestion", "ExitPlanMode"}


@dataclass
class Runtime:
    pid: int
    session_id: str
    cwd: str
    status: str            # busy | idle | exited
    name: str | None
    updated_at: float
    started_at: float

    @property
    def alive(self) -> bool:
        return self.status != "exited"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_runtimes() -> dict[str, Runtime]:
    """Live runtime records keyed by sessionId. Stale files (dead pid) are
    reported with status 'exited' so callers can distinguish 'Claude is
    idle' from 'Claude is gone'."""
    out: dict[str, Runtime] = {}
    if not CLAUDE_SESSIONS.is_dir():
        return out
    for f in CLAUDE_SESSIONS.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            pid = int(d.get("pid") or f.stem)
            sid = d.get("sessionId")
            if not sid:
                continue
            status = d.get("status") or "idle"
            if not _pid_alive(pid):
                status = "exited"
            rt = Runtime(pid=pid, session_id=sid, cwd=d.get("cwd") or "",
                         status=status, name=d.get("name"),
                         updated_at=(d.get("statusUpdatedAt") or d.get("updatedAt") or 0) / 1000,
                         started_at=(d.get("startedAt") or 0) / 1000)
            prev = out.get(sid)
            if prev is None or rt.updated_at > prev.updated_at:
                out[sid] = rt
        except (OSError, ValueError):
            continue
    return out


@dataclass
class Transcript:
    session_id: str
    path: Path
    cwd: str
    mtime: float
    first_ts: float | None = None
    last_ts: float | None = None
    last_role: str | None = None
    last_user_prompt: str = ""
    first_user_prompt: str = ""
    last_assistant_text: str = ""
    last_assistant_tools: list[str] = field(default_factory=list)
    summary: str | None = None
    custom_title: str | None = None
    entries: int = 0

    @property
    def pending_interactive(self) -> bool:
        return self.last_role == "assistant" and any(t in INTERACTIVE_TOOLS for t in self.last_assistant_tools)


def _parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _user_text(content) -> str | None:
    """Text of a real user prompt; None for tool_result-only entries."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        if parts:
            return "\n".join(parts)
    return None


def _strip_system_noise(text: str) -> str:
    # Local command echoes and system-reminder wrappers aren't the user's words.
    if text.startswith("<") and any(k in text[:80] for k in (
            "command-name", "system-reminder", "local-command", "task-notification",
            "task-status")):
        return ""
    if text.startswith("[Image:") and text.count("\n") == 0:
        return ""
    return text.strip()


def read_transcript(path: Path, full: bool = False) -> Transcript | None:
    try:
        st = path.stat()
    except OSError:
        return None
    t = Transcript(session_id=path.stem, path=path, cwd="", mtime=st.st_mtime)
    try:
        with open(path, "rb") as f:
            if not full and st.st_size > TAIL_BYTES:
                # Head for cwd/first prompt/summary, tail for the live state.
                head = f.read(64 * 1024)
                f.seek(-TAIL_BYTES, os.SEEK_END)
                tail = f.read()
                blob = head + b"\n" + tail[tail.find(b"\n") + 1:]
            else:
                blob = f.read()
    except OSError:
        return None
    for raw in blob.split(b"\n"):
        if not raw.strip():
            continue
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        typ = e.get("type")
        if typ == "summary":
            t.summary = e.get("summary") or t.summary
            continue
        if typ in ("custom-title", "ai-title"):
            t.custom_title = e.get("customTitle") or e.get("aiTitle") or e.get("title") or t.custom_title
            continue
        if typ not in ("user", "assistant") or e.get("isSidechain"):
            continue
        t.entries += 1
        if not t.cwd and e.get("cwd"):
            t.cwd = e["cwd"]
        ts = _parse_ts(e.get("timestamp"))
        if ts:
            t.first_ts = t.first_ts or ts
            t.last_ts = ts
        msg = e.get("message") or {}
        content = msg.get("content")
        if typ == "user":
            txt = _user_text(content)
            if txt is not None:
                txt = _strip_system_noise(txt)
                if txt:
                    t.last_role = "user"
                    t.last_user_prompt = txt
                    if not t.first_user_prompt:
                        t.first_user_prompt = txt
        else:
            t.last_role = "assistant"
            texts, tools = [], []
            for b in (content if isinstance(content, list) else []):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    texts.append(b["text"])
                elif b.get("type") == "tool_use":
                    tools.append(b.get("name", ""))
            if texts:
                t.last_assistant_text = "\n".join(texts)
            t.last_assistant_tools = tools
    if not t.cwd:
        return None
    return t


_cache: dict[Path, tuple[float, Transcript | None]] = {}


def iter_transcripts(max_age_secs: float) -> list[Transcript]:
    """Top-level transcripts touched within max_age_secs. Sub-agent
    transcripts live in <session>/subagents/ and are skipped on purpose.
    Parsed results are cached by mtime so a quiet refresh costs stats only."""
    cutoff = time.time() - max_age_secs
    out: list[Transcript] = []
    if not CLAUDE_PROJECTS.is_dir():
        return out
    for d in CLAUDE_PROJECTS.iterdir():
        if not d.is_dir():
            continue
        for f in d.glob("*.jsonl"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            hit = _cache.get(f)
            if hit and hit[0] == mtime:
                t = hit[1]
            else:
                t = read_transcript(f)
                _cache[f] = (mtime, t)
            if t and t.entries >= 2:
                out.append(t)
    return out
