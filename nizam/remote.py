"""Agents awake for the phone: one remote server per agent folder.

The server lets the Claude app on a phone start sessions in the agent's
folder. Each runs in its own process group so it outlives a restart of
Nizam, and awake.json remembers it so the next Nizam can find and stop it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import providers
from .paths import NIZAM_DIR
from .runner import login_path
from .terminal import clean_env

REMOTE_DIR = NIZAM_DIR / "remote"
AWAKE_FILE = REMOTE_DIR / "awake.json"
CONNECT_WAIT = 60
SESSION_WAIT = 10          # once connected, how long to wait for the session it opens to be linkable
LOG_CAP = 256 * 1024       # the server redraws its status about twice a second for as long as it runs
_ANSI = re.compile(r"\x1b\][^\x07]*\x07|\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]")
_DETACHED = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
             "start_new_session": True, "close_fds": True}
_lock = threading.Lock()
_waking: dict[str, threading.Lock] = {}


class WakeError(Exception):
    pass


def _read() -> dict[str, dict]:
    try:
        d = json.loads(AWAKE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _write(d: dict[str, dict]) -> None:
    REMOTE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = AWAKE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    os.replace(tmp, AWAKE_FILE)


def _running(rec: dict) -> bool:
    # A pid in awake.json can outlive its process and be reused; check it still runs our command.
    try:
        out = subprocess.run(["ps", "-p", str(int(rec["pid"])), "-o", "command="], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError, KeyError, ValueError):
        return False
    return bool(rec.get("argv")) and rec["argv"][0] in out


def awake() -> dict[str, dict]:
    """Agent root -> its server's record, for the servers still running."""
    with _lock:
        d = _read()
        live = {root: r for root, r in d.items() if _running(r)}
        if live != d:
            _write(live)
        return live


def _save(rec: dict) -> None:
    with _lock:
        d = _read()
        d[rec["root"]] = rec
        _write(d)


def _forget(root: str) -> dict | None:
    with _lock:
        d = _read()
        rec = d.pop(root, None)
        if rec is not None:
            _write(d)
        return rec


def _log_text(log: Path) -> str:
    try:
        return log.read_bytes()[-LOG_CAP:].decode("utf-8", "replace")
    except OSError:
        return ""


def _relinked(rec: dict) -> dict:
    """rec with the newest links its log shows, saved when they changed."""
    session, page = providers.get(rec.get("provider")).remote_links(_log_text(Path(rec["log"])))
    new = {**rec, "session_url": session or rec.get("session_url"), "page_url": page or rec.get("page_url")}
    if new != rec:
        _save(new)
    return new


def tail(text: str, lines: int = 6) -> str:
    """The last distinct lines of a status display that redraws itself."""
    out: list[str] = []
    for line in _ANSI.sub("", text).replace("\r", "\n").splitlines():
        line = line.strip()
        if line and line not in out:
            out.append(line)
    return "\n".join(out[-lines:])


def wake(root: Path, name: str, permission_mode: str = "default", provider: str | None = None) -> dict:
    """Start the agent's server, or find the one already running, and wait until the phone can reach it."""
    key = str(root)
    with _lock:
        gate = _waking.setdefault(key, threading.Lock())
    with gate:
        rec = awake().get(key)
        if rec:
            return {**_relinked(rec), "already": True}
        if not root.is_dir():
            raise WakeError("That agent's folder is missing.")
        p = providers.get(provider)
        env = clean_env()
        env["PATH"] = login_path()
        exe = shutil.which(p.cli, path=env["PATH"])
        argv = p.remote_command(exe, name, permission_mode) if exe else None
        if not exe:
            raise WakeError(f"The {p.cli} command was not found on the login shell's PATH.")
        if not argv:
            raise WakeError(f"{p.cli} cannot be reached from the phone.")
        REMOTE_DIR.mkdir(parents=True, exist_ok=True)
        log = REMOTE_DIR / f"{hashlib.md5(key.encode()).hexdigest()[:12]}.log"
        log.write_bytes(b"")
        # Append mode, so trim() can empty the file under the running server.
        with open(log, "ab") as out:
            proc = subprocess.Popen(argv, cwd=key, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        threading.Thread(target=proc.wait, daemon=True).start()
        if shutil.which("caffeinate"):
            # -s holds a closed lid only on AC power; on battery macOS sleeps anyway.
            subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(proc.pid)], **_DETACHED)
        rec = {"root": key, "name": name, "pid": proc.pid, "argv": argv[1:], "provider": p.name,
               "log": str(log), "started": time.time(), "session_url": None, "page_url": None}
        _save(rec)
        deadline, connected = time.time() + CONNECT_WAIT, None
        while time.time() < deadline:
            time.sleep(0.5)
            text = _log_text(log)
            if proc.poll() is not None:
                _forget(key)
                raise WakeError(tail(text) or f"{p.cli} exited with code {proc.returncode}.")
            rec["session_url"], rec["page_url"] = p.remote_links(text)
            if rec["page_url"] and connected is None:
                connected = time.time()
            if rec["session_url"] or (connected and time.time() - connected > SESSION_WAIT):
                _save(rec)
                return rec
        last = tail(_log_text(log))
        stop(root)
        raise WakeError(f"It did not connect within {CONNECT_WAIT}s." + (f"\n{last}" if last else ""))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop(root: Path) -> bool:
    """Stop the agent's server and every session it started; False when none was running."""
    rec = _forget(str(root))
    if not rec or not _running(rec):
        return False
    pid = int(rec["pid"])
    for sig, grace in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        deadline = time.time() + grace
        while _alive(pid) and time.time() < deadline:
            time.sleep(0.2)
        if not _alive(pid):
            break
    Path(rec["log"]).unlink(missing_ok=True)
    return True


def trim() -> None:
    """Keep each server's newest links, and its log from growing without end."""
    for rec in awake().values():
        log = Path(rec["log"])
        try:
            size = log.stat().st_size
        except OSError:
            continue
        if size <= LOG_CAP:
            continue
        _relinked(rec)
        try:
            os.truncate(log, 0)
        except OSError:
            pass
