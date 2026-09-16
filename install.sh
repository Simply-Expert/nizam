#!/bin/bash
# Nizam installer — macOS only.
#   curl -fsSL https://raw.githubusercontent.com/blink22/nizam/main/install.sh | bash
# Re-run the same line to upgrade. Env overrides:
#   NIZAM_SRC   where the code lives      (default ~/.nizam/src)
#   NIZAM_REPO  git URL to clone          (default https://github.com/blink22/nizam)
#   NIZAM_REF   branch or tag to check out (default main)
set -euo pipefail

SRC="${NIZAM_SRC:-$HOME/.nizam/src}"
REPO="${NIZAM_REPO:-https://github.com/blink22/nizam}"
REF="${NIZAM_REF:-main}"
BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; RED=$'\033[31m'; RESET=$'\033[0m'
step() { printf '%s→%s %s\n' "$BOLD" "$RESET" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
fail() { printf '  %s✗%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "Nizam is macOS only."
command -v git >/dev/null || fail "git is required (install Xcode command-line tools: xcode-select --install)."
command -v claude >/dev/null || printf '  %s!%s claude CLI not found on PATH; Nizam will start but see no sessions.\n' "$RED" "$RESET"

PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$c" ] && "$c" -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
[ -n "$PY" ] || fail "Python 3.10+ with venv is required (brew install python)."

mkdir -p "$HOME/.nizam"
if [ -d "$SRC/.git" ]; then
  step "Updating $SRC"
  git -C "$SRC" fetch -q origin "$REF" && git -C "$SRC" checkout -q "$REF" && git -C "$SRC" pull -q --ff-only origin "$REF"
else
  step "Cloning $REPO → $SRC"
  git clone -q --branch "$REF" "$REPO" "$SRC"
fi
ok "code at $SRC"

step "Installing Claude Code hooks and the PyObjC venv (one-time, ~1 min)"
( cd "$SRC" && "$PY" -m nizam install )

# Put `nizam` on PATH.
LINKED=""
for d in /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin"; do
  if [ -d "$d" ] && [ -w "$d" ]; then ln -sf "$SRC/bin/nizam" "$d/nizam" && LINKED="$d/nizam" && break; fi
done
if [ -n "$LINKED" ]; then ok "command: nizam ($LINKED)"; else
  mkdir -p "$HOME/.local/bin" && ln -sf "$SRC/bin/nizam" "$HOME/.local/bin/nizam"
  ok "command: nizam (~/.local/bin/nizam — add ~/.local/bin to your PATH)"
fi

# Ask about login only when a terminal is attached (curl | bash still has /dev/tty).
LOGIN=y
if (exec </dev/tty) 2>/dev/null; then
  printf '%sStart Nizam at login? [Y/n] %s' "$BOLD" "$RESET"; read -r LOGIN </dev/tty || LOGIN=y
fi
case "${LOGIN:-y}" in n|N|no|NO) ;; *) ( cd "$SRC" && "$PY" -m nizam login on ) ;; esac

step "Launching"
( cd "$SRC" && nohup "$PY" -m nizam app >/dev/null 2>&1 & )
sleep 2
ok "Nizam is running. Look for the floating badge near the top-right of your screen."
printf '%s  nizam open        # board in the browser\n  nizam app --quit  # stop\n  nizam doctor      # check wiring%s\n' "$DIM" "$RESET"
