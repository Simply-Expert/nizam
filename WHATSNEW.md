# What's new in Nizam

The newest release is at the top.

## 0.3.1 · 22 Oct 2026

- **Start a follow-up from your phone.** `/followups` numbers the ones due now, with a button
  each. Tap one and its agent wakes on that follow-up, the way *Start session* does on the
  board. Open the session in the Claude app and it is already working on it.
- **Claude in Chrome on the phone.** Sessions you open on an awake agent can use Chrome when
  `/chrome` is on for your Mac. Before, they started without it.
- **A much quicker bot.** On networks without working IPv6, every message to and from the bot
  could wait up to 15 seconds. Replies now come back in well under a second.

## 0.3.0 · 22 Oct 2026

- **Wake an agent from your phone.** Connect a Telegram bot of your own, send `/wake marketing`,
  and talk to that agent in the Claude app on your phone. `/sleep`, `/agents`, `/status` and
  `/followups` work too, and so does plain text like "wake the one that does invoices".
- **Your phone tells you when something needs you.** Send the bot `/notify on`: it messages you
  once you have been away from the Mac for 5 minutes, once for each thing.
- **Routines say what they found.** A run that checked and found nothing stays quiet. A run that
  found something you should see goes under Needs you, and stays there until you dismiss it.
- **A Settings page.** Click the gear next to the Nizam name, or press ⌘,, to see and change
  every setting in one place, including setting up the Telegram bot.
- **New sessions in the Claude app.** Choose *Claude app* under *New sessions open in*: new
  sessions open there with the folder and the task filled in, ready for you to send.
- **Pin and gear match.** Both buttons at the top of the board are now plain grey icons.

## 0.2.5 · 20 Oct 2026

- **Notifications are opt-in.** They are off until you turn them on: right-click the badge
  and choose *Notifications*, then allow Nizam when macOS asks.
- **Clicking a notification takes you there.** It opens the session's terminal tab, or the
  Claude app for a session started there, instead of Script Editor. Anything else opens the
  board on that item.
- **A lower, softer sound.** Notifications use Bottle instead of Tink.
- **Closed and Done fold away.** Click either header on the board to collapse it to its count.

## 0.2.4 · 7 Oct 2026

- **Follow-ups at a time of day.** Give a follow-up a time (`## 2026-10-01 (Thu) 21:00 — Title`)
  and it comes up then, with a notification. Items without a time come up at 10:00, or at the
  hour you set with `nizam followups hour 8pm`.
- **Follow-ups any date out.** Agents no longer refuse dates more than two weeks away.
- **Resumed routines settle the run.** Resume a failed routine's session and finish it: the
  board takes that result instead of leaving the run Incomplete.
- **Hover works on every desktop.** The floating badge now shows its lists on hover on every
  Space, not just one.

## 0.2.3 · 28 Sep 2026

- **Come back to a session later.** Click ⏰ Remind me on a session (or press z) and
  pick a time. It waits in a new "Later" list, then moves to "Needs you" with a
  notification when the time comes.
- **Or just ask.** Inside a session, say "remind me about this at 1:30".
