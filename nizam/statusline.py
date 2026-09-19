"""Status line entrypoint. Records the plan limits the tool reports to
~/.nizam/usage-<provider>.json, then prints what the user's own status line
command prints, if they had one.

`nizam install` wires it in as: statusline.py <provider> [<the user's command>]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def record(provider: str, data: dict) -> None:
    limits = data.get("rate_limits")
    if not isinstance(limits, dict) or not limits:
        return
    path = os.path.join(os.path.expanduser("~"), ".nizam", f"usage-{provider}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time(), "rate_limits": limits}, f)
    os.replace(tmp, path)


def main() -> int:
    raw = sys.stdin.read()
    try:
        record(sys.argv[1], json.loads(raw))
    except Exception:
        pass
    if len(sys.argv) > 2 and sys.argv[2]:
        try:
            sys.stdout.write(subprocess.run(sys.argv[2], shell=True, input=raw, text=True,
                                            stdout=subprocess.PIPE).stdout)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
