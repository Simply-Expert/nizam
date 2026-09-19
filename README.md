# Nizam · نظام

**A manager's view of your Claude Code agents.**

Nizam treats every folder you run Claude Code in as an *agent*: a colleague with a role, standing
topics, and a queue of conversations with you. It shows, at a glance, which agent needs you,
which is working, and which has finished and is waiting for your reply. It lives in the macOS
menu bar and a floating badge, and opens the same board in a popover or a browser.

<p>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-blue.svg" /></a>
  <img alt="macOS only" src="https://img.shields.io/badge/macOS-13%2B-lightgrey?logo=apple" />
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white" />
  <img alt="Claude Code" src="https://img.shields.io/badge/Claude%20Code-2.1.158%2B-orange" />
</p>

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/Simply-Expert/nizam/main/install.sh | bash
```

That clones the code to `~/.nizam/src`, installs Claude Code hooks into `~/.claude/settings.json`
(a backup is kept beside it), builds a small PyObjC venv, links a `nizam` command, and starts the
app. Re-run the same line to upgrade. Nothing phones home and nothing needs an API key.

Manual install:

```bash
git clone https://github.com/Simply-Expert/nizam ~/.nizam/src && cd ~/.nizam/src
bin/nizam install      # hooks + venv
bin/nizam app          # menu bar + floating badge + local server
bin/nizam login on     # start at login
```

## The model

| Term | What it is | How Nizam finds it |
|---|---|---|
| **Agent** | A role you talk to, e.g. *Marketing*, *Backend*, *VA*. | The nearest folder above the session's start directory holding `CLAUDE.md`, `AGENTS.md` or a configured `.claude/`. Otherwise the start directory itself. |
| **Area** | A standing topic inside an agent, e.g. `channels/email`, `campaigns/sept-2026`. | Any sub-folder of the agent holding `INSTRUCTIONS.md`. Areas nest. Folders without the marker (`data`, `scripts`, `shared`) are not areas. |
| **Session** | One Claude Code conversation. | The transcript in `~/.claude/projects`. |
| **Follow-up** | A dated one-off the agent owes, not yet a session. | `## YYYY-MM-DD (Day) — [area] Title` sections in `FOLLOWUPS.md` at the agent root, where an optional `[area]` tag files an item under that area, or in a self-contained area's own `FOLLOWUPS.md`. The board merges them. Read-only. |

Sessions fall into five states:

| State | Meaning | Source |
|---|---|---|
| **Needs you** | Claude is blocked: a permission prompt, `AskUserQuestion`, a plan to approve, or no progress for 5 minutes while busy. | Hook events |
| **Working** | Claude is processing. | Claude's own runtime status file |
| **Your turn** | Claude finished its turn in a session that is still open. | `Stop` hook, or idle status |
| **Closed** | The session exited without being marked done. Ages into Done after two days. | Process gone |
| **Done** | You marked it done, or it went two days without activity. New activity reopens it. | You |

Done is deliberately manual. The old approach of guessing "finished" from the wording of Claude's
last message was wrong too often; you are the only one who knows a task is over.

## Using it

- **Badge and menu bar**: live counts. Click for the board, right-click for options. The badge is
  draggable and exists because a full menu bar on a notched MacBook hides new status items.
- **Board**: agents on the left (pin, rename, drag to reorder via right-click), the selected agent's
  sessions in the middle with area chips, and the full last message on the right.
- **Jump to** focuses the live session's tab (Terminal.app, iTerm, Ghostty 1.3+). **Resume** reopens
  an exited one with `claude --resume`. **New here** starts a session in that agent and area with
  your last-used settings; ⌥-click for the full dialog (prompt, permission mode, git worktree,
  paste-only).
- **Follow-ups** appear under an agent's sessions, overdue in red and today in amber; *All agents*
  shows only what is due. **Start session** opens one with the follow-up as the first prompt. Nizam
  never edits the file: the agent records the outcome and deletes its own line.
- **Clear** marks every *Closed* session in the current view as done; hovering a Closed row shows a Done button.
- Keys: `j`/`k` move, `Enter` jumps, `d` marks done, `n` starts a session, `Esc` closes.
- Notifications fire when a session newly needs you.

Claude Code sessions started from the Claude desktop app appear too, but without a live status
and with Jump to opening the app rather than the chat. Regular desktop chats are not Claude Code
sessions and are not shown.

## The `nizam` skill

`nizam install` also copies a Claude Code skill to `~/.claude/skills/nizam`. Inside any agent's
session it manages areas:

```
/nizam area list
/nizam area new email          # creates INSTRUCTIONS.md + JOURNAL-<year>.md
/nizam area promote campaigns/sept-2026
/nizam area archive referral
/nizam followup add 2026-09-18 re-run the banner conversion script
/nizam followup tag                 # add [area] tags to untagged items
/nizam followup list
/nizam followup done banner
```

A folder becomes visible on the board the moment it gets an `INSTRUCTIONS.md`.

## Commands

```
nizam app             menu bar + badge + server in one process
nizam app --quit      stop it
nizam app --detach    same, in the background; survives closing the terminal
nizam app --restart   stop it, wait for it to exit, start it again detached
nizam serve [--open]  server only, for the browser
nizam open            open the board in the browser
nizam install         install hooks + build the venv
nizam uninstall       remove hooks, skill and start-at-login
nizam login on|off    start at login (LaunchAgent)
nizam doctor          check wiring
```

## Files

| Path | Contents |
|---|---|
| `~/.nizam/state.json` | Your decisions: done flags, session titles, agent display names, pins, order, prefs |
| `~/.nizam/events.jsonl` | Hook events, including short snippets of prompts and replies. Treat as private. |
| `~/.nizam/venv` | PyObjC for the Mac shell |
| `~/.nizam/src` | The code, when installed with the one-liner |
| `~/.claude/settings.json` | Gets eight `nizam/hook.py` hook entries; `nizam uninstall` removes them |
| `~/.claude/skills/nizam` | The `/nizam` skill; removed by `nizam uninstall` |

## Requirements

macOS 13+, Python 3.10+ able to create a venv (Homebrew or Xcode command-line tools),
Claude Code 2.1.158 or newer. No third-party Python packages outside the venv.

## Lineage

Nizam started as a rewrite of [claude-sessions-status](https://github.com/blink22/claude-sessions-status)
after the folder-and-heuristics model stopped fitting how the agents were actually used.

## License

MIT. See [LICENSE](LICENSE).
