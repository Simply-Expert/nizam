"""Jump to a session's terminal, resume a dead one, or start a new one."""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import uuid
from pathlib import Path

from . import providers

_KNOWN_TERMINALS = (("Terminal.app", "Terminal"), ("iTerm", "iTerm"), ("Ghostty", "Ghostty"),
                    ("Alacritty", "Alacritty"), ("WezTerm", "WezTerm"), ("kitty", "kitty"),
                    ("Hyper", "Hyper"), ("Tabby", "Tabby"), ("Warp", "Warp"))
_DETACHED = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
             "stderr": subprocess.DEVNULL, "start_new_session": True, "close_fds": True}


def clean_env() -> dict[str, str]:
    """Environment without the markers of an agent session Nizam itself may
    have been started from."""
    return providers.clean_env()
_SAFE_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def _ps(pid: int, fmt: str) -> str:
    try:
        return subprocess.run(["ps", "-p", str(pid), "-o", fmt], capture_output=True,
                              text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def session_host(pid: int) -> dict:
    """{'kind': 'terminal', 'tty': 'ttys001', 'app': 'Terminal'} or {'kind': 'desktop'}.
    The controlling tty is the reliable signal; a session's own record of
    its entrypoint can say desktop even for terminal launches."""
    tty = _ps(pid, "tty=")
    if tty in ("", "?", "??"):
        return {"kind": "desktop", "pid": pid}
    app = None
    cur = pid
    for _ in range(12):
        line = _ps(cur, "ppid=,command=")   # comm= truncates app paths
        if not line:
            break
        parts = line.split(None, 1)
        ppid = int(parts[0]) if parts and parts[0].isdigit() else 0
        exe = (parts[1].split()[0] if len(parts) > 1 and parts[1] else "")
        for needle, name in _KNOWN_TERMINALS:
            if needle.lower() in exe.lower():
                app = name
                break
        if app or ppid <= 1:
            break
        cur = ppid
    return {"kind": "terminal", "pid": pid, "tty": tty, "app": app}


def _osascript(*lines: str) -> str:
    args = ["osascript"]
    for l in lines:
        args += ["-e", l]
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10,
                              env=clean_env()).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def focus_terminal_tab(tty: str) -> bool:
    dev = tty if tty.startswith("/dev/") else "/dev/" + tty
    script = f'''
tell application "Terminal"
    activate
    repeat with w in windows
        try
            repeat with t in tabs of w
                if (tty of t as string) is "{dev}" then
                    set selected of t to true
                    set frontmost of w to true
                    return "found"
                end if
            end repeat
        end try
    end repeat
end tell
return "missing"'''
    return "found" in _osascript(script)


def focus_iterm_tab(tty: str) -> bool:
    dev = tty if tty.startswith("/dev/") else "/dev/" + tty
    script = f'''
tell application "iTerm"
    activate
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if (tty of s) is "{dev}" then
                    select t
                    tell s to select
                    return "found"
                end if
            end repeat
        end repeat
    end repeat
end tell
return "missing"'''
    return "found" in _osascript(script)


def focus_ghostty(cwd: str, titles: list[str]) -> bool:
    """Ghostty 1.3+ scripting: terminals expose title and working directory
    but not the tty. Title is the primary key (the agent sets it to the session
    title); the working directory only breaks ties, since tabs launched via
    `-e` report it empty."""
    def q(x: str) -> str:
        return json.dumps(x, ensure_ascii=False)
    wanted = [t.strip() for t in titles if t and t.strip()]
    conds = " or ".join(f"(nm contains {q(t)})" for t in wanted) or "false"
    script = f"""
tell application "Ghostty"
    set titleHits to {{}}
    set titleCwdHit to missing value
    set cwdOnly to missing value
    set cwdHits to 0
    repeat with w in windows
        repeat with tb in tabs of w
            repeat with s in terminals of tb
                set nm to name of s
                set wd to working directory of s
                if {conds} then
                    set end of titleHits to s
                    if wd is {q(cwd)} then set titleCwdHit to s
                end if
                if wd is {q(cwd)} then
                    set cwdHits to cwdHits + 1
                    set cwdOnly to s
                end if
            end repeat
        end repeat
    end repeat
    set target to missing value
    if titleCwdHit is not missing value then
        set target to titleCwdHit
    else if (count of titleHits) is 1 then
        set target to item 1 of titleHits
    else if (count of titleHits) is 0 and cwdHits is 1 then
        set target to cwdOnly
    end if
    if target is missing value then return "missing"
    focus target
    activate
    return "found"
end tell"""
    return "found" in _osascript(script)


def focus(pid: int, cwd: str = "", titles: list[str] | None = None, provider: str | None = None) -> bool:
    host = session_host(pid)
    if host["kind"] == "desktop":
        app = providers.get(provider).desktop_app
        if not app:
            return False
        subprocess.Popen(["open", "-a", app], env=clean_env(), **_DETACHED)
        return True
    app = host.get("app")
    if app == "Terminal" and focus_terminal_tab(host["tty"]):
        return True
    if app == "iTerm" and focus_iterm_tab(host["tty"]):
        return True
    if app == "Ghostty" and cwd and focus_ghostty(cwd, titles or []):
        return True
    if app:
        subprocess.Popen(["open", "-a", app], env=clean_env(), **_DETACHED)
        return True
    return False


def _ghostty_new_window(shell_cmd: str, run: bool) -> bool:
    """Open a window in the running Ghostty (never a second instance) and
    type the command into its shell, so shell integration and the working
    directory stay intact and the tab is scriptable afterwards."""
    text = json.dumps(shell_cmd, ensure_ascii=False)
    enter = 'send key "enter" to t' if run else ""
    script = f"""
tell application "Ghostty"
    activate
    set w to new window
    delay 0.5
    set t to focused terminal of selected tab of w
    input text {text} to t
    {enter}
    return "ok"
end tell"""
    return "ok" in _osascript(script)


LAUNCHERS = ("Terminal", "Ghostty")


def run_in_new_terminal(shell_cmd: str, paste_only: bool = False, launcher: str = "Terminal",
                        cwd: str | None = None) -> None:
    """Open a new terminal window running shell_cmd. With paste_only the
    command is placed in the zsh line buffer for the user to review."""
    if launcher == "Ghostty":
        if _ghostty_new_window(shell_cmd, run=not paste_only):
            return
        subprocess.Popen(["open", "-a", "Ghostty"], env=clean_env(), **_DETACHED)
        return
    if paste_only:
        shell_cmd = "print -z -- " + "'" + shell_cmd.replace("'", "'\\''") + "'"
    lit = json.dumps(shell_cmd, ensure_ascii=False)   # AppleScript shares JSON's escapes
    _osascript('tell application "Terminal" to activate',
               f'tell application "Terminal" to do script {lit}')


def resume(session_id: str, cwd: str, paste_only: bool = False, launcher: str = "Terminal",
           provider: str | None = None) -> bool:
    if not _SAFE_ID.match(session_id):
        return False
    run_in_new_terminal(f"cd {shlex.quote(cwd)} && {providers.get(provider).resume_command(session_id)}",
                        paste_only, launcher, cwd)
    return True


def start(cwd: str, prompt: str = "", permission_mode: str = "acceptEdits",
          worktree: bool = False, paste_only: bool = False, launcher: str = "Terminal",
          provider: str | None = None) -> str:
    """Start a new session in a new Terminal window. The session id is
    minted here and handed to the CLI, so the caller knows which
    transcript belongs to this launch before it even appears."""
    sid = str(uuid.uuid4())
    p = providers.get(provider)
    cmd = p.start_command(sid, prompt, permission_mode)
    if worktree:
        root = git_root(cwd)
        if root:
            import time
            stamp = time.strftime("%Y%m%d-%H%M%S")
            wt = Path(root).parent / f"{Path(root).name}-{stamp}"
            shell = (f"git -C {shlex.quote(root)} worktree add -b {p.name}/{stamp} {shlex.quote(str(wt))} "
                     f"&& cd {shlex.quote(str(wt))} && {cmd}")
            run_in_new_terminal(shell, paste_only, launcher, str(wt.parent))
            return sid
    run_in_new_terminal(f"cd {shlex.quote(cwd)} && {cmd}", paste_only, launcher, cwd)
    return sid


def git_root(cwd: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None
