"""Jump to a session's terminal, resume a dead one, or start a new one."""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import uuid
from pathlib import Path

PERMISSION_MODES = ("acceptEdits", "plan", "auto", "default")
_KNOWN_TERMINALS = (("Terminal.app", "Terminal"), ("iTerm", "iTerm"), ("Ghostty", "Ghostty"),
                    ("Alacritty", "Alacritty"), ("WezTerm", "WezTerm"), ("kitty", "kitty"),
                    ("Hyper", "Hyper"), ("Tabby", "Tabby"), ("Warp", "Warp"))
_DETACHED = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
             "stderr": subprocess.DEVNULL, "start_new_session": True, "close_fds": True}
_SAFE_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def _ps(pid: int, fmt: str) -> str:
    try:
        return subprocess.run(["ps", "-p", str(pid), "-o", fmt], capture_output=True,
                              text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def session_host(pid: int) -> dict:
    """{'kind': 'terminal', 'tty': 'ttys001', 'app': 'Terminal'} or {'kind': 'desktop'}.
    The controlling tty is the reliable signal; Claude's own `entrypoint`
    field says claude-desktop even for terminal launches."""
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
        return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout
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


def focus(pid: int) -> bool:
    host = session_host(pid)
    if host["kind"] == "desktop":
        subprocess.Popen(["open", "-a", "Claude"], **_DETACHED)
        return True
    app = host.get("app")
    if app == "Terminal" and focus_terminal_tab(host["tty"]):
        return True
    if app == "iTerm" and focus_iterm_tab(host["tty"]):
        return True
    if app:
        subprocess.Popen(["open", "-a", app], **_DETACHED)
        return True
    return False


LAUNCHERS = ("Terminal", "Ghostty")


def run_in_new_terminal(shell_cmd: str, paste_only: bool = False, launcher: str = "Terminal") -> None:
    """Open a new terminal window running shell_cmd. With paste_only the
    command is placed in the zsh line buffer for the user to review."""
    if paste_only:
        shell_cmd = "print -z -- " + "'" + shell_cmd.replace("'", "'\\''") + "'"
    if launcher == "Ghostty":
        # `-e` hands the rest of argv to Ghostty as the command; an
        # interactive zsh after it keeps the window open once claude exits.
        subprocess.Popen(["open", "-na", "Ghostty", "--args", "-e", "zsh", "-ic",
                          shell_cmd + "; exec zsh -i"], **_DETACHED)
        return
    lit = json.dumps(shell_cmd, ensure_ascii=False)   # AppleScript shares JSON's escapes
    _osascript('tell application "Terminal" to activate',
               f'tell application "Terminal" to do script {lit}')


def resume(session_id: str, cwd: str, paste_only: bool = False, launcher: str = "Terminal") -> bool:
    if not _SAFE_ID.match(session_id):
        return False
    run_in_new_terminal(f"cd {shlex.quote(cwd)} && claude --resume {shlex.quote(session_id)}",
                        paste_only, launcher)
    return True


def start(cwd: str, prompt: str = "", permission_mode: str = "acceptEdits",
          worktree: bool = False, paste_only: bool = False, launcher: str = "Terminal") -> str:
    """Start a new session in a new Terminal window. The session id is
    minted here and passed via --session-id, so the caller knows which
    transcript belongs to this launch before it even appears."""
    sid = str(uuid.uuid4())
    flags = [f"--session-id {sid}"]
    if permission_mode in PERMISSION_MODES and permission_mode != "default":
        flags.append(f"--permission-mode {permission_mode}")
    cmd = "claude " + " ".join(flags)
    if prompt.strip():
        cmd += " " + shlex.quote(prompt.strip())
    if worktree:
        root = _git_root(cwd)
        if root:
            import time
            stamp = time.strftime("%Y%m%d-%H%M%S")
            wt = Path(root).parent / f"{Path(root).name}-{stamp}"
            shell = (f"git -C {shlex.quote(root)} worktree add -b claude/{stamp} {shlex.quote(str(wt))} "
                     f"&& cd {shlex.quote(str(wt))} && {cmd}")
            run_in_new_terminal(shell, paste_only, launcher)
            return sid
    run_in_new_terminal(f"cd {shlex.quote(cwd)} && {cmd}", paste_only, launcher)
    return sid


def _git_root(cwd: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None
