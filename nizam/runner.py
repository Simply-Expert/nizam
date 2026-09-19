"""`nizam run <routine.md>`: one run of a routine, from launchd or by hand.

What each routine would otherwise carry in its own shell wrapper lives here
once: the locks, the login PATH, the timeout, the log, and the check that
the work was really done.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import providers
from . import routines as R
from .providers.base import Provider
from .terminal import clean_env

AGENT_WAIT = 60 * 60          # how long to queue behind another routine of the same agent
QUICK_FAILURE = 90            # a failure this early did no work, so it is safe to retry
RETRY_DELAY = 60
LOG_KEEP_DAYS = 30
POLL = 2.0

HEADLESS = (
    "You are running unattended as the scheduled routine \"{name}\". Nobody is watching and nobody "
    "can answer you: never ask a question, never wait for confirmation, never offer options to choose "
    "from. Do the work the routine describes. If something blocks you, say exactly what it was and stop.\n"
    "Your reply must end with one final line, exactly `OUTCOME: COMPLETE` when the routine's work was "
    "done, or `OUTCOME: INCOMPLETE - <reason>` when it was not."
)


def _login_path() -> str:
    """launchd hands jobs a bare PATH; ask the user's login shell for the real one."""
    extra = [str(Path.home() / ".local/bin"), *(str(d) for p in providers.active() for d in p.cli_dirs),
             "/opt/homebrew/bin", "/usr/local/bin"]
    found = ""
    try:
        out = subprocess.run([os.environ.get("SHELL") or "/bin/zsh", "-ilc", 'echo "__NIZAM__${PATH}__NIZAM__"'],
                             capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
                             env=clean_env()).stdout
        found = out.split("__NIZAM__")[1] if out.count("__NIZAM__") >= 2 else ""
    except (OSError, subprocess.SubprocessError):
        pass
    seen: list[str] = []
    for p in (found.split(":") + os.environ.get("PATH", "").split(":") + extra):
        if p and p not in seen:
            seen.append(p)
    return ":".join(seen)


def _lock(path: Path, wait: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    deadline = time.time() + wait
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if time.time() >= deadline:
                fh.close()
                return None
            time.sleep(5)


def _kill(proc: subprocess.Popen) -> None:
    for sig, grace in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def _watch(proc: subprocess.Popen, timeout: int, log: Path, on_line) -> tuple[bool, float]:
    """Stream the child's output into the log until it exits or the wall clock runs out."""
    assert proc.stdout
    stdout = proc.stdout
    awake = None
    if shutil.which("caffeinate"):
        # Idle sleep mid-run kills network calls; closing the lid still sleeps the Mac.
        awake = subprocess.Popen(["caffeinate", "-i", "-w", str(proc.pid)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def pump() -> None:
        with open(log, "a", encoding="utf-8") as out:
            for line in stdout:
                out.write(line)
                out.flush()
                on_line(line)

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    started = prev = time.time()
    timed_out, slept = False, 0.0
    while proc.poll() is None:
        time.sleep(POLL)
        now = time.time()
        if now - prev > POLL + 30:
            slept += now - prev
        prev = now
        if now - started > timeout:
            timed_out = True
            _kill(proc)
    t.join(timeout=10)
    if awake and awake.poll() is None:
        awake.terminate()
    return timed_out, slept


def _timeout_note(r: R.Routine, slept: float) -> str:
    limit = f"{r.timeout // 60} min" if r.timeout >= 120 else f"{r.timeout}s"
    return f"Stopped after the {limit} limit" + (f"; the Mac slept for {int(slept // 60)} min of it." if slept else ".")


def _script(r: R.Routine, env: dict, log: Path) -> dict:
    started = time.time()
    tail: list[str] = []

    def on_line(line: str) -> None:
        tail.append(line.rstrip())
        del tail[:-40]

    proc = subprocess.Popen(["/bin/sh", "-c", r.command], cwd=str(r.cwd), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                            start_new_session=True)
    timed_out, slept = _watch(proc, r.timeout, log, on_line)
    status = R.decide_status(timed_out, proc.returncode, False, "complete")
    out = "\n".join(tail).strip()
    if status == "timed_out":
        summary = _timeout_note(r, slept) + (f"\n\n{out}" if out else "")
    elif status == "errored":
        summary = f"Exit code {proc.returncode}.\n\n{out}" if out else f"Exit code {proc.returncode}, no output."
    else:
        summary = out or "Exit code 0, no output."
    return {"status": status, "exit_code": proc.returncode, "summary": summary[-1500:],
            "duration": int(time.time() - started), "slept": int(slept)}


def _wait_for_network(limit: float = 90) -> None:
    """launchd fires missed jobs the moment the Mac wakes, before Wi-Fi is back."""
    deadline = time.time() + limit
    while time.time() < deadline:
        try:
            socket.getaddrinfo("www.apple.com", 443)
            return
        except OSError:
            time.sleep(3)


def _attempt(r: R.Routine, p: Provider, exe: str, env: dict, log: Path) -> dict:
    sid = str(uuid.uuid4()) if p.takes_session_id else None
    cmd, prompt = p.headless_command(
        exe, sid, HEADLESS.format(name=r.name),
        f"Current date/time: {datetime.now().astimezone():%Y-%m-%d %H:%M %Z (%A)}\n\n{r.prompt}\n", r.meta)

    started = time.time()
    proc = subprocess.Popen(cmd, cwd=str(r.cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)
    assert proc.stdin
    results: list = []
    tail: list[str] = []

    def on_line(line: str) -> None:
        nonlocal sid
        sid = sid or p.headless_session_id(line)
        res = p.headless_result(line)
        if res:
            results.append(res)
            return
        try:
            json.loads(line)
        except ValueError:            # not structured output, so likely the CLI's own error text
            tail.append(line.strip())
            del tail[:-5]

    def feed(stdin=proc.stdin) -> None:
        try:
            stdin.write(prompt)
            stdin.close()
        except OSError:
            pass

    threading.Thread(target=feed, daemon=True).start()
    timed_out, slept = _watch(proc, r.timeout, log, on_line)

    result = results[-1] if results else None
    outcome, reason, body = R.parse_outcome(result.text if result else "")
    status = R.decide_status(timed_out, proc.returncode, bool(result and result.is_error), outcome)
    if status == "timed_out":
        summary = _timeout_note(r, slept)
    elif status == "errored":
        summary = body or "\n".join(tail) or f"{p.cli} exited with code {proc.returncode}"
    elif status == "incomplete":
        summary = (f"{reason}\n\n" if reason else "" if outcome else
                   "The run ended without an OUTCOME line, so nothing confirms the work was done.\n\n") + body
    else:
        summary = body
    return {"status": status, "exit_code": proc.returncode, "session": sid, "summary": summary[-1500:],
            "duration": int(time.time() - started), "slept": int(slept),
            "cost": result.cost if result else None, "turns": result.turns if result else None}


def run(path: str, scheduled: bool = False) -> int:
    try:
        r = R.load(Path(path))
    except OSError as e:
        print(f"nizam run: {e}")
        return 2
    if scheduled and not r.enabled:
        return 0
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{r.id}"
    base = {"run_id": run_id, "routine": r.id, "name": r.name, "agent": str(r.agent_root), "file": str(r.path),
            "trigger": "schedule" if scheduled else "manual", "started": time.time(), "pid": os.getpid()}

    def finish(status: str, summary: str, code: int, **more) -> int:
        R.record({**base, "status": status, "summary": summary, "ended": time.time(), **more})
        print(f"{r.name}: {status}" + (f" — {summary.splitlines()[0][:200]}" if summary else ""))
        return code

    if r.error:
        return finish("errored", f"Routine file is invalid: {r.error}", 2)
    own = _lock(R.LOCKS_DIR / f"{r.id}.lock", 0)
    if not own:
        return finish("skipped", "The previous run of this routine is still going.", 0)
    agent_key = hashlib.md5(str(r.agent_root).encode()).hexdigest()[:12]
    turn = _lock(R.LOCKS_DIR / f"agent-{agent_key}.lock", AGENT_WAIT)
    if not turn:
        return finish("skipped", "Another routine of this agent ran for over an hour; gave up waiting.", 0)

    env = clean_env()
    if r.meta.get("env_file"):
        env.update(R.read_env_file(r.cwd / r.meta["env_file"]))
    env["PATH"] = _login_path()
    env["NIZAM_ROUTINE"] = r.id
    p = providers.get(r.meta.get("provider"))
    exe = "" if r.command else shutil.which(p.cli, path=env["PATH"])
    if not r.command and not exe:
        return finish("errored", f"The {p.cli} command was not found on the login shell's PATH.", 2)
    if scheduled:
        _wait_for_network()

    log_dir = R.LOGS_DIR / r.id
    log_dir.mkdir(parents=True, exist_ok=True)
    for old in log_dir.iterdir():
        if time.time() - old.stat().st_mtime > LOG_KEEP_DAYS * 86400:
            old.unlink(missing_ok=True)
    log = log_dir / f"{run_id}.{'log' if r.command else 'jsonl'}"
    base.update(started=time.time(), log=str(log))
    R.record({**base, "status": "running"})

    sessions: list[str] = []
    res: dict = {}
    try:
        for attempt in (1, 2):
            if r.command:
                # No retry: a script that failed early may still have written half its rows.
                res = _script(r, env, log)
                break
            res = _attempt(r, p, exe or "", env, log)
            if ran := res.pop("session"):
                sessions.append(ran)
            R.record({**base, "status": "running", "sessions": sessions})
            if res["status"] != "errored" or res["duration"] > QUICK_FAILURE or attempt == 2:
                break
            time.sleep(RETRY_DELAY)
    except Exception as e:  # the record must never be left at "running"
        return finish("errored", f"The runner failed: {e!r}", 1, sessions=sessions)
    status = res.pop("status")
    return finish(status, res.pop("summary"), 0 if status == "completed" else 1,
                  sessions=sessions, attempts=max(1, len(sessions)), **res)
