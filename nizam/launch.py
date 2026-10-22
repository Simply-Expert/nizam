"""Menu-bar app helpers that must not import AppKit (used by the plain CLI)."""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .paths import NIZAM_DIR

PID_FILE = NIZAM_DIR / "app.pid"
QUIT_FLAG = NIZAM_DIR / "app.quit"
LAUNCH_AGENT = os.path.expanduser("~/Library/LaunchAgents/co.nizam.app.plist")
BUNDLE_ID = "co.nizam.app"
APP_BUNDLE = NIZAM_DIR / "Nizam.app"
NOTIFICATION_SOUND = "Bottle"   # the lowest-pitched of the system alert sounds
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
              "LaunchServices.framework/Support/lsregister")


def app_bundle(venv_python: Path) -> Path | None:
    """~/.nizam/Nizam.app's executable: the venv's interpreter under Nizam's own
    bundle id, so notifications and their clicks are Nizam's. None without a framework Python."""
    src = Path(os.path.realpath(venv_python)).parents[1] / "Resources/Python.app/Contents/MacOS/Python"
    if not src.exists():
        return None
    exe = APP_BUNDLE / "Contents" / "MacOS" / "Nizam"
    plist_path = APP_BUNDLE / "Contents" / "Info.plist"
    sound_src = Path(f"/System/Library/Sounds/{NOTIFICATION_SOUND}.aiff")
    # A notification's named sound is reliably found only inside the app's own bundle.
    sound = APP_BUNDLE / "Contents" / "Resources" / sound_src.name
    info = {"CFBundleIdentifier": BUNDLE_ID, "CFBundleName": "Nizam", "CFBundleDisplayName": "Nizam",
            "CFBundleExecutable": "Nizam", "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1",
            "LSUIElement": True, "NizamPython": str(src), "NizamSound": sound_src.name}
    try:
        with open(plist_path, "rb") as f:
            if plistlib.load(f) == info and exe.exists() and (sound.exists() or not sound_src.exists()):
                return exe
    except (OSError, plistlib.InvalidFileException):
        pass
    # The copy links its Python by absolute path, so a Python upgrade means a fresh copy.
    shutil.rmtree(APP_BUNDLE, ignore_errors=True)
    try:
        exe.parent.mkdir(parents=True)
        shutil.copy2(src, exe)
        if sound_src.exists():
            sound.parent.mkdir()
            shutil.copyfile(sound_src, sound)   # copy2 would carry SIP's restricted flag, which is refused
        with open(plist_path, "wb") as f:
            plistlib.dump(info, f)
        if subprocess.run(["codesign", "-f", "-s", "-", str(APP_BUNDLE)], capture_output=True).returncode:
            raise OSError("codesign failed")
    except OSError:
        shutil.rmtree(APP_BUNDLE, ignore_errors=True)
        return None
    subprocess.run([LSREGISTER, "-f", str(APP_BUNDLE)], capture_output=True)
    return exe


def login_enabled() -> bool:
    return os.path.exists(LAUNCH_AGENT)


def set_login(on: bool) -> None:
    if not on:
        # Not unloaded: when the running app is the login item's own job, that would quit it.
        try:
            os.remove(LAUNCH_AGENT)
        except FileNotFoundError:
            pass
        return
    root = Path(__file__).resolve().parents[1]
    plist = {
        "Label": BUNDLE_ID,
        "ProgramArguments": [str(NIZAM_DIR / "venv" / "bin" / "python"), "-m", "nizam", "app"],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        # Brought back after a crash, not after Quit (which exits 0).
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(NIZAM_DIR / "app.log"),
        "StandardErrorPath": str(NIZAM_DIR / "app.log"),
    }
    os.makedirs(os.path.dirname(LAUNCH_AGENT), exist_ok=True)
    with open(LAUNCH_AGENT, "wb") as f:
        plistlib.dump(plist, f)


def start_with_launchd() -> bool:
    """Start the app as the login item's job, so launchd restarts it if it crashes.
    False when there is no login item or launchd would not take it; the caller starts it itself."""
    import time
    if not login_enabled() or app_running():   # a bootout would quit an app that is the job
        return False
    set_login(True)   # a login item written by an older Nizam lacks KeepAlive
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{BUNDLE_ID}"], capture_output=True)
    for _ in range(5):
        # Right after a bootout, bootstrap can briefly fail with an I/O error.
        if subprocess.run(["launchctl", "bootstrap", domain, LAUNCH_AGENT], capture_output=True).returncode == 0:
            return True
        time.sleep(0.5)
    return False


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
