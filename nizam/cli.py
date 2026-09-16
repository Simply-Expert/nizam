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


VENV = NIZAM_DIR / "venv"
VENV_PY = VENV / "bin" / "python"


def _venv_ok() -> bool:
    return VENV_PY.exists() and subprocess.run(
        [str(VENV_PY), "-c", "import AppKit, WebKit"], capture_output=True).returncode == 0


def ensure_venv() -> bool:
    if _venv_ok():
        return True
    seed = next((p for p in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3")
                 if Path(p).exists()), None)
    if not seed:
        print("no python3 found for the menu-bar app venv"); return False
    print(f"→ creating {VENV} and installing PyObjC (one-time)…")
    subprocess.run([seed, "-m", "venv", str(VENV)], check=False)
    subprocess.run([str(VENV_PY), "-m", "pip", "install", "--quiet",
                    "pyobjc-framework-Cocoa", "pyobjc-framework-WebKit"], check=False)
    return _venv_ok()


def run_app(port: int) -> int:
    from .terminal import clean_env
    if not ensure_venv():
        return 1
    if os.path.realpath(sys.executable) != os.path.realpath(VENV_PY):
        os.execve(str(VENV_PY), [str(VENV_PY), "-m", "nizam", "app", "--port", str(port)], clean_env())
    if len(clean_env()) != len(os.environ):
        os.execve(sys.executable, [sys.executable, "-m", "nizam", "app", "--port", str(port)], clean_env())
    from .app import run
    return run(port)


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
    ap = sub.add_parser("app", help="run the menu-bar app (server included)")
    ap.add_argument("--port", type=int, default=7331)
    ap.add_argument("--quit", action="store_true", help="quit the running menu-bar app")
    lg = sub.add_parser("login", help="start at login: on | off | status")
    lg.add_argument("state", choices=("on", "off", "status"))
    a = p.parse_args(argv)
    if a.cmd == "app":
        if a.quit:
            from .launch import request_quit
            return request_quit()
        return run_app(a.port)
    if a.cmd == "login":
        from .launch import login_enabled, set_login
        if a.state != "status":
            set_login(a.state == "on")
        print("start at login:", "on" if login_enabled() else "off")
        return 0
    if a.cmd == "serve":
        from .terminal import clean_env
        if len(clean_env()) != len(os.environ):
            # Re-exec so nothing spawned later inherits a Claude session's markers.
            os.execve(sys.executable, [sys.executable, "-m", "nizam", *sys.argv[1:]], clean_env())
        from .server import serve
        serve(a.port, a.open)
        return 0
    if a.cmd == "open":
        port = PORT_FILE.read_text().strip() if PORT_FILE.exists() else "7331"
        subprocess.Popen(["open", f"http://127.0.0.1:{port}/"])
        return 0
    if a.cmd == "install":
        install_hooks(); ensure_venv(); return 0
    if a.cmd == "uninstall":
        uninstall_hooks(); return 0
    if a.cmd == "doctor":
        return doctor()
    return 1


if __name__ == "__main__":
    sys.exit(main())
