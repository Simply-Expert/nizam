"""Agents awake for the phone: one remote server per agent folder.

The server lets the Claude app on a phone start sessions in the agent's
folder; a task, such as a follow-up, gets a session of its own that starts
on its prompt. Each runs in its own process group so it outlives a restart
of Nizam, and awake.json remembers it so the next Nizam can find and stop it.
A task can instead open in a window of the person's terminal: an ordinary
session with remote control on, remembered only so the same task finds it.
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

from . import providers, terminal
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


def _live() -> dict[str, dict]:
    with _lock:
        d = _read()
        live = {k: r for k, r in d.items() if _running(r)}
        if live != d:
            _write(live)
        return live


def awake() -> dict[str, dict]:
    """Agent root -> its server's record, or its oldest task session's, for those still running."""
    out: dict[str, dict] = {}
    for key, rec in sorted(_live().items(), key=lambda kv: (kv[0] != kv[1]["root"], kv[1]["started"])):
        if rec.get("host") != "terminal":
            out.setdefault(rec["root"], rec)
    return out


def tasks() -> set[str]:
    """Keys ("<root>#<task>") of the task sessions still running."""
    return {k for k, r in _live().items() if r.get("task")}


def _save(rec: dict) -> None:
    with _lock:
        d = _read()
        d[rec.get("key", rec["root"])] = rec
        _write(d)


def _forget(key: str) -> dict | None:
    with _lock:
        d = _read()
        rec = d.pop(key, None)
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
    if not rec.get("log"):
        return rec
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


def wake(root: Path, name: str, permission_mode: str = "default", provider: str | None = None, *,
         task: str = "", prompt: str = "", title: str = "", cwd: Path | None = None) -> dict:
    """Start the agent's server, or find the one already running, and wait until the phone can reach it.

    With a task, start instead a session of its own in cwd that begins on the prompt; pressing
    the same task again finds that session while it runs.
    """
    key = f"{root}#{task}" if task else str(root)
    with _lock:
        gate = _waking.setdefault(key, threading.Lock())
    with gate:
        rec = _live().get(key)
        if rec:
            return {**_relinked(rec), "already": True}
        cwd = cwd or root
        if not root.is_dir() or not cwd.is_dir():
            raise WakeError("That agent's folder is missing.")
        p = providers.get(provider)
        env = clean_env()
        env["PATH"] = login_path()
        exe = shutil.which(p.cli, path=env["PATH"])
        if not exe:
            raise WakeError(f"The {p.cli} command was not found on the login shell's PATH.")
        argv = (p.remote_task_command(exe, f"{name}: {title}" if title else name, permission_mode, prompt)
                if task else p.remote_command(exe, name, permission_mode))
        if not argv:
            raise WakeError(f"{p.cli} cannot be reached from the phone.")
        REMOTE_DIR.mkdir(parents=True, exist_ok=True)
        log = REMOTE_DIR / f"{hashlib.md5(key.encode()).hexdigest()[:12]}.log"
        log.write_bytes(b"")
        # Append mode, so trim() can empty the file under the running server.
        with open(log, "ab") as out:
            proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        threading.Thread(target=proc.wait, daemon=True).start()
        if shutil.which("caffeinate"):
            # -s holds a closed lid only on AC power; on battery macOS sleeps anyway.
            subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(proc.pid)], **_DETACHED)
        # What follows the tool itself is what _running() looks for, even under a wrapper.
        rec = {"key": key, "root": str(root), "name": name, "pid": proc.pid, "argv": argv[argv.index(exe) + 1:],
               "provider": p.name, "log": str(log), "started": time.time(), "session_url": None, "page_url": None}
        if task:
            rec.update(task=task, title=title)
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
        _stop(_forget(key))
        raise WakeError(f"It did not connect within {CONNECT_WAIT}s." + (f"\n{last}" if last else ""))


def start_in_terminal(root: Path, name: str, permission_mode: str, provider: str | None = None, *,
                      task: str, prompt: str, title: str, cwd: Path, launcher: str) -> dict:
    """Start a task's session in a new window of the launcher, reachable from the phone, and wait until it is.

    Pressing the same task again finds that session while it runs.
    """
    key = f"{root}#{task}"
    with _lock:
        gate = _waking.setdefault(key, threading.Lock())
    with gate:
        rec = _live().get(key)
        if rec:
            return {**_relinked(rec), "already": True}
        if not root.is_dir() or not cwd.is_dir():
            raise WakeError("That agent's folder is missing.")
        p = providers.get(provider)
        if not p.remote_ready():
            raise WakeError(f"{p.cli} cannot be reached from the phone.")
        sid = terminal.start(str(cwd), prompt, permission_mode, launcher=launcher, provider=p.name,
                             remote=f"{name}: {title}")
        if not sid:
            raise WakeError(f"{p.cli} cannot be reached from the phone.")
        deadline = time.time() + CONNECT_WAIT
        while time.time() < deadline:
            time.sleep(0.5)
            rt = p.runtimes().get(sid)
            if rt and rt.alive and rt.remote_url:
                # The session id is on its command line, which is what _running() looks for.
                rec = {"key": key, "root": str(root), "name": name, "pid": rt.pid, "argv": [sid], "host": "terminal",
                       "provider": p.name, "log": None, "started": time.time(), "session_url": rt.remote_url,
                       "page_url": None, "task": task, "title": title}
                _save(rec)
                if shutil.which("caffeinate"):
                    subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(rt.pid)], **_DETACHED)
                return rec
        raise WakeError(f"It did not connect within {CONNECT_WAIT}s. Its window on this Mac may say why.")


def hosted() -> dict[str, dict]:
    """Key -> record of what Nizam runs where no window shows it: servers and their task sessions."""
    return {k: r for k, r in _live().items() if r.get("host") != "terminal"}


def host_key(pid: int, records: dict[str, dict] | None = None) -> str | None:
    """The key of the record that runs the session with this pid: its task's, or the server's that opened it."""
    try:
        ppid = int(subprocess.run(["ps", "-p", str(pid), "-o", "ppid="], capture_output=True,
                                  text=True, timeout=3).stdout.strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    for key, rec in (hosted() if records is None else records).items():
        if int(rec["pid"]) in (pid, ppid):
            return key
    return None


def background(pid: int) -> bool:
    """Whether Nizam runs this session where no window shows it."""
    return host_key(pid) is not None


def release(pid: int) -> bool:
    """Stop a session Nizam runs in the background, so it can be resumed in a window; False when it isn't one."""
    records = hosted()
    key = host_key(pid, records)
    if key is None:
        return False
    if records[key].get("task"):
        _stop(_forget(key))
    else:
        # One of a server's sessions: the server stays up for the phone.
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + 5.0
    while _alive(pid) and time.time() < deadline:
        time.sleep(0.2)
    return not _alive(pid)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop(root: Path) -> bool:
    """Stop the agent's server, its sessions and task sessions, except those in a terminal window; False when none ran."""
    keys = [k for k, r in _read().items() if r.get("root") == str(root) and r.get("host") != "terminal"]
    return any([_stop(_forget(k)) for k in keys])


def stop_task(root: Path, task: str) -> bool:
    """Stop one task's session, leaving the agent's server and its other sessions running."""
    return _stop(_forget(f"{root}#{task}"))


def _stop(rec: dict | None) -> bool:
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
    for rec in _live().values():
        if not rec.get("log"):
            continue
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
