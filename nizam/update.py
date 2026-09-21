"""Self-update. A checkout on a v* tag moves to the newest one; one on a branch fast-forwards."""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from .paths import NIZAM_DIR

SRC = Path(__file__).resolve().parents[1]
CACHE_FILE = NIZAM_DIR / "update.json"
CHECK_EVERY_SECS = 24 * 3600
RETRY_SECS = 3600
GIT_TIMEOUT = 20


class UpdateError(Exception):
    pass


def _run(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(SRC), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UpdateError(f"git {args[0]}: {e}") from e


def _git(*args: str) -> str:
    r = _run(*args)
    if r.returncode:
        raise UpdateError((r.stderr.strip() or f"git {args[0]} failed").splitlines()[-1])
    return r.stdout.strip()


def _version(tag: str) -> tuple[int, ...]:
    """Empty for anything that is not a release tag."""
    m = re.fullmatch(r"v(\d+(?:\.\d+)*)", tag)
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def newest_tag(ls_remote: str) -> str | None:
    tags = [line.rpartition("refs/tags/")[2] for line in ls_remote.splitlines()]
    return max((t for t in tags if _version(t)), key=_version, default=None)


def installed() -> dict:
    branch = _run("symbolic-ref", "-q", "--short", "HEAD").stdout.strip()
    if branch:
        return {"mode": "branch", "ref": branch, "label": f"{branch}@{_git('rev-parse', '--short', 'HEAD')}"}
    tag = _run("describe", "--tags", "--exact-match", "HEAD").stdout.strip()
    if not _version(tag):
        raise UpdateError(f"{SRC} is neither on a release tag nor on a branch; update it by hand")
    return {"mode": "tag", "ref": tag, "label": tag}


def check() -> dict:
    """Reads only: nothing is fetched."""
    here = installed()
    latest = None
    if here["mode"] == "tag":
        newest = newest_tag(_git("ls-remote", "--tags", "--refs", "origin", "v*"))
        if newest and _version(newest) > _version(here["ref"]):
            latest = newest
    else:
        sha = _git("ls-remote", "origin", f"refs/heads/{here['ref']}").partition("\t")[0]
        # A sha we do not have fails this too, as it should.
        if sha and _run("merge-base", "--is-ancestor", sha, "HEAD").returncode:
            latest = f"{here['ref']}@{sha[:7]}"
    info = {"current": here["label"], "latest": latest, "checked_at": time.time()}
    _remember(info)
    return info


def apply(info: dict) -> None:
    here = installed()
    if _git("status", "--porcelain", "--untracked-files=no"):
        raise UpdateError(f"{SRC} has local changes; commit or stash them first")
    _git("fetch", "-q", "--tags", "origin")
    if here["mode"] == "tag":
        _git("checkout", "-q", info["latest"])
    else:
        _git("merge", "-q", "--ff-only", f"origin/{here['ref']}")
    CACHE_FILE.unlink(missing_ok=True)


_info: dict | None = None
_next_check = 0.0
_thread: threading.Thread | None = None


def _remember(info: dict) -> None:
    global _info, _next_check
    _info = info
    _next_check = info["checked_at"] + CHECK_EVERY_SECS
    try:
        NIZAM_DIR.mkdir(exist_ok=True)
        CACHE_FILE.write_text(json.dumps(info))
    except OSError:
        pass


def _background() -> None:
    global _info, _next_check
    try:
        if _info is None and CACHE_FILE.exists():
            cached = json.loads(CACHE_FILE.read_text())
            # A cache written before the checkout last moved describes another install.
            if cached.get("current") == installed()["label"]:
                _info, _next_check = cached, cached["checked_at"] + CHECK_EVERY_SECS
        if time.time() >= _next_check:
            check()
    except (UpdateError, ValueError, KeyError, OSError):
        _next_check = time.time() + RETRY_SECS


def poll() -> dict | None:
    """The last known result, never blocking; a daily check runs behind it."""
    global _thread
    if time.time() >= _next_check and not (_thread and _thread.is_alive()):
        _thread = threading.Thread(target=_background, daemon=True)
        _thread.start()
    return _info
