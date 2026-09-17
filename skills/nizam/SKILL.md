---
name: nizam
description: Nizam (نظام) — manage this agent's Areas and Follow-ups as seen on the Nizam board. Use when the user says "nizam area new X", "create an area for X", "list areas", "archive the X area", "promote this folder to an area", "add a follow-up", "remind us to X on <date>", "what follow-ups are due", "that follow-up is done", or asks where a topic's instructions and journal should live. An Area is a sub-folder carrying INSTRUCTIONS.md (durable rules) and JOURNAL-<year>.md (dated log). Folders without INSTRUCTIONS.md (data, scripts, shared) are not areas.
---

# Nizam

An **Agent** is the folder you launched Claude in (the one with CLAUDE.md, AGENTS.md or `.claude/`).
An **Area** is a sub-folder of that agent that holds `INSTRUCTIONS.md`.
Areas nest: `campaigns/sept-2026` inside `campaigns` is a child area if both carry the marker.
The Nizam board groups sessions by agent and area using exactly this rule, so a folder becomes
visible on the board the moment it gets an INSTRUCTIONS.md.

## `area new <name>` (optionally under a shelf, e.g. `channels/snapchat`)
1. Create the folder relative to the agent root. If the user gave a bare name and the agent already
   keeps areas under a shelf folder (`channels/`, `campaigns/`, `areas/`), use the one that fits the
   topic when it is obvious, otherwise ask which shelf.
2. Write `INSTRUCTIONS.md`, filled from what the user said:

   ```markdown
   # <Name> — Instructions

   Settled, durable rules for this area, written so a new person could follow them blindly.
   Dated events go in the journal, not here.

   ## What this area covers
   ## How we work here
   ## Definitions / thresholds
   ```
3. Write `JOURNAL-<current year>.md`:

   ```markdown
   # <Name> — Journal <year>

   Newest entries on top. One entry per send, decision, or notable event.

   ---
   ```
4. If the agent's `CLAUDE.md` has a workspace tree, add one line for the new area.
5. Reply with the path and the one-line rule: instructions hold settled practice, journal holds dated events.

## `area list`
Walk the agent root (skip `node_modules`, `.git`, `data`, `archive`, dot-folders; depth ≤ 4) and print
every folder containing `INSTRUCTIONS.md`, indented by nesting. Note any folder that looks like an area
but lacks the marker (has a JOURNAL or PLAN.md) and offer to promote it.

## `area archive <name>`
Move the folder to `archive/<name>` at the agent root (create `archive/` if missing) and append a dated
line to the area's journal saying it was archived and why. Never delete.

## `area promote <folder>`
Turn an existing folder into an area by adding `INSTRUCTIONS.md` (seeded from any README, PLAN.md or
notes already there) and a journal if missing.

## `followup add <date> <what>`
Follow-ups are dated, short-lived, one-off items that would otherwise be forgotten. The Nizam board
reads every `FOLLOWUPS.md` it finds (agent root and inside areas), merges them, flags overdue items,
filters by area, and can start a session from one. It never writes these files; you do.

**Where an item goes — you decide, using this rule of thumb:**
- **Default: the agent root's `FOLLOWUPS.md`, with an `[area]` tag.** One queue the agent can scan
  in ten seconds.
- **An area's own `FOLLOWUPS.md`** only when that area is self-contained: it has its own plan or
  status log, and its follow-ups would never be acted on from a root session (a campaign with a
  dozen dated checks is the typical case). Items in an area file need no tag.
- **Never both.** Once an area has its own file, all of that area's items live there; move any
  tagged ones out of the root file when you create it, and say that you did.

1. Create the chosen `FOLLOWUPS.md` if missing, with this header:

   ```markdown
   # Follow-ups — dated, short-lived, one-off

   Nothing recurring (routines live elsewhere). Nothing further out than ~14 days.
   Delete on completion; delete when more than 7 days stale or move it to a real plan.

   ---
   ```
2. Add the item in date order, using exactly this heading shape so the board can parse it:

   ```markdown
   ## 2026-09-18 (Fri) — [email] Short imperative title
   One paragraph: the exact action, and why it matters, written so it can be picked up cold.
   Name the script, file or query to run. End with "Delete once <condition>."
   ```

   The `[tag]` is the area the item belongs to: the area's folder name (`[email]`), or its path
   from the agent root when the name alone is ambiguous (`[campaigns/sept-2026]`). Run `area list`
   if unsure. Omit the tag for items that belong to the agent as a whole. A tagged item shows under
   that area's chip on the board, and "Start session" opens in that area's folder.

## `followup tag`
Go through the untagged items in `FOLLOWUPS.md` and add the `[area]` tag to each one that clearly
belongs to a single area, judging from its text. Leave cross-area items untagged. Change nothing
else in the file and show the list of changes.

## `followup list`
Aggregate first: read the root `FOLLOWUPS.md` **and** every `FOLLOWUPS.md` inside an area (run
`area list` to find them), so nothing due in an area is missed from a root session. Print the
items grouped as overdue, today, upcoming, each with its area. Point out any item more than 7 days late and
ask whether to delete it or move it into a plan.

## `followup done <title or date>`
Find the item in whichever file holds it. Write the outcome where it belongs (the area's journal, a plan's status log), then delete the item
from `FOLLOWUPS.md`. The file is a queue, not a record.

## Rules
- Never put dated events in INSTRUCTIONS.md, and never leave settled rules only in the journal.
- Keep INSTRUCTIONS.md short enough to read in a minute; promote rules from the journal when they settle.
- Do not create an area for a one-off task; those belong in the agent root's FOLLOWUPS.md or TASKS.md.
