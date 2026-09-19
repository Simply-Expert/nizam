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
| **Routine** | Recurring work an agent does unattended, on a schedule: a headless Claude run, or a plain script. | `routines/<name>.md` at the agent root or inside an area: a `schedule:` header and the prompt. Read-only. |

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
- **Routines** appear under an agent's follow-ups with their next run and last result. A run that
  errored, timed out, or ended without confirming its work counts under *Needs you*, notifies, and
  shows in *All agents* until it succeeds or you dismiss it. **Run now** starts one by hand,
  **Open last run** resumes that run as a normal session, **Sync schedule** installs a new schedule.
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

## Routines

A routine replaces the usual trio of prompt file, shell wrapper and hand-written LaunchAgent with one file:

```markdown
---
schedule: sat-thu 09:30, 16:00, 22:00; fri 09:30
timeout: 30m
model: sonnet
allowed_tools: mcp__slack__*, Read, Write, Edit
---
You are the daily scan. 1. Read tasks/backlog.md …
```

`nizam routines sync` turns each `schedule:` into a LaunchAgent, so launchd is the clock: routines fire
whether or not the Nizam app is running, and a slot missed while the Mac slept runs once on wake.
`nizam run` is the single wrapper they all share:

- one run per agent at a time; a second routine queues instead of killing the first
- the login shell's `PATH`, so `node`-based MCP servers work under launchd
- `env_file: .env.local` in the header, for what a wrapper used to `source`
- a wall-clock timeout that kills the whole process group, and `caffeinate` so idle sleep does not cut a run short
- one retry when a run fails within 90 seconds, which is what a run fired on wake before the network is up looks like
- every run must end with `OUTCOME: COMPLETE`. A headless run that stops to ask "shall I proceed?"
  exits 0 having done nothing; here it is reported as *incomplete*
- the full stream of each run in `~/.nizam/routines/logs`, kept 30 days

**Script routines.** A `command:` line in the header runs a script instead of Claude, for collectors and
checks that need no judgement. Same schedule, PATH, timeout, log and board; success is exit code 0.

```markdown
---
schedule: monthly 6 09:07
command: node scripts/collect-monthly-snapshot.mjs
---
Collects last month's numbers into data/snapshots/. Feeds the monthly report.
```

When one fails, **Start session to fix** opens the agent with the command, the end of the output and
the log path as the first prompt. It is meant for scripts an agent owns, not as a general cron.

Already have routines as hand-made LaunchAgents with shell wrappers? Or as cron lines? Ask the agent to
`/nizam routine migrate <name>`: it translates the plist or crontab line and the wrapper, asks before the (real) test run,
and retires the old job in the same step it installs the new one.

Schedules: `manual`, `daily 09:30, 16:00`, `mon, wed 08:00`, `sat-thu 09:30` (ranges wrap),
`monthly 1 09:00`, `hourly :15`, joined with `;`. Local time. A slot missed while the Mac was powered
off or logged out is not made up.

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
nizam routines list   this agent's routines: state, next run, last result
nizam routines sync   install this agent's schedules into launchd; remove those of deleted routines
nizam run <file>      run one routine now, headless
nizam login on|off    start at login (LaunchAgent)
nizam doctor          check wiring
```

## Files

| Path | Contents |
|---|---|
| `~/.nizam/state.json` | Your decisions: done flags, session titles, agent display names, pins, order, prefs |
| `~/.nizam/events.jsonl` | Hook events, including short snippets of prompts and replies. Treat as private. |
| `~/.nizam/routines` | Routine run records (`runs.jsonl`, with the tail of each run's final reply), per-run logs, locks. Treat as private. |
| `~/Library/LaunchAgents/co.nizam.routine.*` | One per scheduled routine; written by `nizam routines sync`, removed by `nizam uninstall` |
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
