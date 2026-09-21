"""Menu-bar app helpers that must not import AppKit (used by the plain CLI)."""
from __future__ import annotations

import os
import subprocess
import sys

from .paths import NIZAM_DIR

PID_FILE = NIZAM_DIR / "app.pid"
QUIT_FLAG = NIZAM_DIR / "app.quit"
LAUNCH_AGENT = os.path.expanduser("~/Library/LaunchAgents/co.nizam.app.plist")


def login_enabled() -> bool:
    return os.path.exists(LAUNCH_AGENT)


def set_login(on: bool) -> None:
    if not on:
        subprocess.run(["launchctl", "unload", LAUNCH_AGENT], capture_output=True)
        try:
            os.remove(LAUNCH_AGENT)
        except FileNotFoundError:
            pass
        return
    import plistlib
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    plist = {
        "Label": "co.nizam.app",
        "ProgramArguments": [str(NIZAM_DIR / "venv" / "bin" / "python"), "-m", "nizam", "app"],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": str(NIZAM_DIR / "app.log"),
        "StandardErrorPath": str(NIZAM_DIR / "app.log"),
    }
    os.makedirs(os.path.dirname(LAUNCH_AGENT), exist_ok=True)
    with open(LAUNCH_AGENT, "wb") as f:
        plistlib.dump(plist, f)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def app_running() -> bool:
    return PID_FILE.exists() and pid_alive(int(PID_FILE.read_text() or 0))


def request_quit() -> int:
    if app_running():
        QUIT_FLAG.touch()
        print("asked the menu-bar app to quit")
        return 0
    print("no menu-bar app running")
    return 0


def wait_for_quit(timeout: float = 15.0) -> bool:
    """Ask the running app to quit and wait for it to exit; True once none is running."""
    import time
    if not app_running():
        return True
    pid = int(PID_FILE.read_text())
    QUIT_FLAG.touch()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return False
