"""`nizam` command line: serve, open, install/uninstall hooks, doctor."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .paths import CLAUDE_SETTINGS, EVENTS_FILE, PORT_FILE, NIZAM_DIR, ensure_dirs



def _python() -> str:
    return sys.executable or "python3"


def _hook_command() -> str:
    return f"{_python()} {Path(__file__).with_name('hook.py')}"


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
MARK = "nizam/hook.py"


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


def install_hooks() -> None:
    d = _load_settings()
    hooks = _strip_ours(d.get("hooks") or {})
    for ev, matcher in HOOK_EVENTS.items():
        g = {"hooks": [{"type": "command", "command": _hook_command(), "timeout": 5, "async": True}]}
        if matcher:
            g["matcher"] = matcher
        hooks.setdefault(ev, []).append(g)
    d["hooks"] = hooks
    _save_settings(d)
    ensure_dirs()
    print(f"✓ hooks installed in {CLAUDE_SETTINGS} (backup: settings.json.bak-nizam)")
    print("  Live sessions pick them up on their next event; no restart needed.")


def uninstall_hooks() -> None:
    d = _load_settings()
    d["hooks"] = _strip_ours(d.get("hooks") or {})
    if not d["hooks"]:
        d.pop("hooks")
    _save_settings(d)
    print("✓ nizam hooks removed")


def doctor() -> int:
    ok = True
    d = _load_settings()
    ours = sum(1 for gs in (d.get("hooks") or {}).values() for g in gs
               for h in g.get("hooks", []) if MARK in str(h.get("command", "")))
    print(f"hooks installed: {ours}/{len(HOOK_EVENTS)}")
    ok &= ours == len(HOOK_EVENTS)
    print(f"events file: {EVENTS_FILE} ({EVENTS_FILE.stat().st_size if EVENTS_FILE.exists() else 0} bytes)")
    from .paths import CLAUDE_SESSIONS
    print(f"runtime files: {len(list(CLAUDE_SESSIONS.glob('*.json'))) if CLAUDE_SESSIONS.is_dir() else 0}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="nizam")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the local server")
    s.add_argument("--port", type=int, default=7331)
    s.add_argument("--open", action="store_true", help="open the board in the browser")
    sub.add_parser("open", help="open the board in the browser")
    sub.add_parser("install", help="install Claude Code hooks")
    sub.add_parser("uninstall", help="remove Claude Code hooks")
    sub.add_parser("doctor", help="check wiring")
    a = p.parse_args(argv)
    if a.cmd == "serve":
        from .server import serve
        serve(a.port, a.open)
        return 0
    if a.cmd == "open":
        port = PORT_FILE.read_text().strip() if PORT_FILE.exists() else "7331"
        subprocess.Popen(["open", f"http://127.0.0.1:{port}/"])
        return 0
    if a.cmd == "install":
        install_hooks(); return 0
    if a.cmd == "uninstall":
        uninstall_hooks(); return 0
    if a.cmd == "doctor":
        return doctor()
    return 1


if __name__ == "__main__":
    sys.exit(main())
