"""Claude Code: what it leaves on disk, how its hooks speak, how to run it.

Two sources on disk:
  * ~/.claude/sessions/<pid>.json — live runtime status per running
    process (sessionId, cwd, name, status busy|idle). Authoritative for
    "is Claude processing right now".
  * ~/.claude/projects/<slug>/<sessionId>.jsonl — transcripts. Give us
    cwd, timestamps, the last user prompt and the last assistant text.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path

from .base import (ACTIVITY, ANSWERED, ENDED, NEEDS, TURN_DONE, WORKING, HeadlessResult, Provider, Runtime,
                   Signal, Transcript)

CLAUDE_DIR = Path.home() / ".claude"
CLAUDE_PROJECTS = CLAUDE_DIR / "projects"
CLAUDE_SESSIONS = CLAUDE_DIR / "sessions"      # runtime status files (Claude >= 2.1.158)
CLAUDE_SETTINGS = CLAUDE_DIR / "settings.json"
SKILL_DST = CLAUDE_DIR / "skills" / "nizam" / "SKILL.md"
DESKTOP_SESSIONS = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
_DESKTOP_ID = re.compile(r"^local_[A-Za-z0-9-]{1,64}$")

TAIL_BYTES = 256 * 1024
PENDING_TOOLS = {"ExitPlanMode": "Plan to review", "AskUserQuestion": "Asking you"}

HOOK_EVENTS = {
    "SessionStart": None,
    "UserPromptSubmit": None,
    "Stop": None,
    "SessionEnd": None,
    "Notification": "permission_prompt|idle_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input",
    "PreToolUse": "AskUserQuestion|ExitPlanMode",
    "PostToolUse": "AskUserQuestion|ExitPlanMode",
    "ElicitationResult": None,
}
HOOK_SCRIPT = Path(__file__).resolve().parents[1] / "hook.py"
MARK = "nizam/hook.py"

NEEDS_EVENTS = {
    ("Notification", "permission_prompt"): "Permission needed",
    ("Notification", "elicitation_dialog"): "Input needed",
    ("Notification", "elicitation_url_dialog"): "Input needed",
    ("Notification", "agent_needs_input"): "Input needed",
    ("PreToolUse", "AskUserQuestion"): "Asking you",
    ("PreToolUse", "ExitPlanMode"): "Plan to review",
}
EVENT_KINDS = {"UserPromptSubmit": WORKING, "PostToolUse": WORKING, "SessionStart": WORKING,
               "Stop": TURN_DONE, "SessionEnd": ENDED, "ElicitationResult": ANSWERED}
ENV_MARKERS = ("AI_AGENT", "ENABLE_CLAUDEAI_MCP_SERVERS")


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
    t = Transcript(session_id=path.stem, path=path, cwd="", mtime=st.st_mtime, provider="claude")
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
    if t.last_role == "assistant":
        t.pending_label = next((PENDING_TOOLS[n] for n in PENDING_TOOLS if n in t.last_assistant_tools), None)
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


def _load_settings() -> dict:
    try:
        return json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_settings(d: dict) -> None:
    bak = CLAUDE_SETTINGS.with_suffix(".json.bak-nizam")
    if CLAUDE_SETTINGS.exists() and not bak.exists():
        shutil.copy2(CLAUDE_SETTINGS, bak)
    tmp = CLAUDE_SETTINGS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, CLAUDE_SETTINGS)


def _strip_ours(hooks: dict) -> dict:
    out = {}
    for ev, groups in hooks.items():
        kept = []
        for g in groups or []:
            inner = [h for h in g.get("hooks", []) if MARK not in str(h.get("command", ""))]
            if inner:
                kept.append({**g, "hooks": inner})
        if kept:
            out[ev] = kept
    return out


class Claude(Provider):
    name = "claude"
    cli = "claude"
    cli_dirs = (CLAUDE_DIR / "local",)
    desktop_app = "Claude"
    permission_modes = ("acceptEdits", "plan", "auto", "default")

    def transcripts(self, max_age_secs: float) -> list[Transcript]:
        return iter_transcripts(max_age_secs)

    def runtimes(self) -> dict[str, Runtime]:
        return read_runtimes()

    def marks_agent(self, folder: Path) -> bool:
        if (folder / "CLAUDE.md").is_file():
            return True
        dot = folder / ".claude"
        if dot.is_dir():
            # Claude drops settings.local.json wherever you grant a permission;
            # only a deliberately configured .claude folder marks an agent.
            return any(c.name != "settings.local.json" for c in dot.iterdir())
        return False

    def install_hooks(self) -> list[str]:
        d = _load_settings()
        hooks = _strip_ours(d.get("hooks") or {})
        command = f"{sys.executable or 'python3'} {HOOK_SCRIPT} {self.name}"
        for ev, matcher in HOOK_EVENTS.items():
            g = {"hooks": [{"type": "command", "command": command, "timeout": 5, "async": True}]}
            if matcher:
                g["matcher"] = matcher
            hooks.setdefault(ev, []).append(g)
        d["hooks"] = hooks
        _save_settings(d)
        return [f"✓ hooks installed in {CLAUDE_SETTINGS} (backup: settings.json.bak-nizam)",
                "  Live sessions pick them up on their next event; no restart needed."]

    def uninstall_hooks(self) -> list[str]:
        d = _load_settings()
        d["hooks"] = _strip_ours(d.get("hooks") or {})
        if not d["hooks"]:
            d.pop("hooks")
        _save_settings(d)
        return ["✓ nizam hooks removed"]

    def install_skill(self, src: Path) -> list[str]:
        SKILL_DST.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, SKILL_DST)
        return [f"✓ /nizam skill installed at {SKILL_DST}"]

    def uninstall_skill(self) -> list[str]:
        if not SKILL_DST.exists():
            return []
        SKILL_DST.unlink()
        return ["✓ /nizam skill removed"]

    def doctor(self) -> tuple[bool, list[str]]:
        ours = sum(1 for gs in (_load_settings().get("hooks") or {}).values() for g in gs
                   for h in g.get("hooks", []) if MARK in str(h.get("command", "")))
        runtime_files = len(list(CLAUDE_SESSIONS.glob("*.json"))) if CLAUDE_SESSIONS.is_dir() else 0
        return ours == len(HOOK_EVENTS), [f"hooks installed: {ours}/{len(HOOK_EVENTS)}",
                                          f"runtime files: {runtime_files}"]

    def signal(self, event: dict) -> Signal:
        name = event.get("hook_event_name")
        key = (name, event.get("notification_type") or event.get("tool_name") or event.get("matcher"))
        if key in NEEDS_EVENTS:
            return Signal(NEEDS, NEEDS_EVENTS[key], event.get("question") or event.get("message") or "")
        return Signal(EVENT_KINDS.get(name or "", ACTIVITY))

    def start_command(self, session_id: str, prompt: str, permission_mode: str) -> str:
        cmd = f"claude --session-id {session_id}"
        if permission_mode in self.permission_modes and permission_mode != "default":
            cmd += f" --permission-mode {permission_mode}"
        if prompt.strip():
            cmd += " " + shlex.quote(prompt.strip())
        return cmd

    def resume_command(self, session_id: str) -> str:
        return f"claude --resume {shlex.quote(session_id)}"

    def desktop_url(self, session_id: str) -> str | None:
        # The app files a session under its own id; the transcript's id is only a field inside.
        for f in DESKTOP_SESSIONS.glob("*/*/local_*.json"):
            try:
                meta = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            own = meta.get("sessionId")
            if meta.get("cliSessionId") == session_id and isinstance(own, str) and _DESKTOP_ID.match(own):
                return f"claude://code/continue?session={own}&source=nizam"
        return None

    def clean_env(self, env: dict[str, str]) -> dict[str, str]:
        # A child that inherits CLAUDE_CODE_CHILD_SESSION refuses to save transcripts.
        return {k: v for k, v in env.items() if not (k.startswith("CLAUDE") or k in ENV_MARKERS)}

    def session_id(self, env: dict[str, str]) -> str | None:
        return env.get("CLAUDE_CODE_SESSION_ID") or None

    def headless_command(self, exe: str, session_id: str | None, system_prompt: str, prompt: str,
                         meta: dict) -> tuple[list[str], str]:
        from ..routines import split_list
        cmd = [exe, "-p", "--output-format", "stream-json", "--verbose", "--session-id", session_id,
               "--append-system-prompt", system_prompt]
        if meta.get("model"):
            cmd += ["--model", meta["model"]]
        if meta.get("permission_mode"):
            cmd += ["--permission-mode", meta["permission_mode"]]
        for key, flag in (("allowed_tools", "--allowedTools"), ("disallowed_tools", "--disallowedTools")):
            tools = split_list(meta.get(key, ""))
            if tools:
                cmd += [flag, ",".join(tools)]
        return cmd, prompt

    def headless_result(self, line: str) -> HeadlessResult | None:
        try:
            ev = json.loads(line)
        except ValueError:
            return None
        if not isinstance(ev, dict) or ev.get("type") != "result":
            return None
        return HeadlessResult(text=str(ev.get("result") or ""), is_error=bool(ev.get("is_error")),
                              cost=ev.get("total_cost_usd"), turns=ev.get("num_turns"))
