"""What Nizam reads from, writes to and runs of Claude Code."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import fixtures as fx
from nizam import cli, routines, runner, terminal

SID = "22222222-2222-4222-8222-222222222222"
REPO = Path(__file__).resolve().parents[1]


class Case(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp()).resolve()
        self.cwd = str(self.home / "work")
        self.now = time.time()
        for p in fx.point_at(self.home):
            p.start()
            self.addCleanup(p.stop)


class Transcripts(Case):
    def read(self, entries):
        return fx.read_transcript(fx.write_transcript(self.home, SID, self.cwd, entries))

    def test_fields(self):
        t0 = self.now - 100
        t = self.read([
            fx.user("first", t0, self.cwd),
            fx.assistant("thinking aloud", t0 + 1, self.cwd, tools=("Bash",)),
            fx.user([{"type": "tool_result", "content": "ok"}], t0 + 2, self.cwd),
            fx.assistant("answer", t0 + 3, self.cwd),
            fx.user([{"type": "text", "text": "second"}, {"type": "text", "text": "line"}], t0 + 4, self.cwd),
            fx.assistant("", t0 + 5, self.cwd, tools=("Read", "AskUserQuestion")),
            {"type": "ai-title", "aiTitle": "Titled"},
        ])
        self.assertEqual((t.session_id, t.cwd, t.entries), (SID, self.cwd, 6))
        self.assertEqual((t.first_user_prompt, t.last_user_prompt), ("first", "second\nline"))
        self.assertEqual((t.last_role, t.last_assistant_text, t.last_assistant_tools),
                         ("assistant", "answer", ["Read", "AskUserQuestion"]))
        self.assertEqual(t.custom_title, "Titled")
        self.assertAlmostEqual(t.first_ts, t0, places=3)
        self.assertAlmostEqual(t.last_ts, t0 + 5, places=3)

    def test_system_noise_is_not_a_prompt(self):
        t0 = self.now - 100
        t = self.read([
            fx.user("real", t0, self.cwd),
            fx.assistant("ok", t0 + 1, self.cwd),
            fx.user("<command-name>/clear</command-name>", t0 + 2, self.cwd),
            fx.user("<system-reminder>x</system-reminder>", t0 + 3, self.cwd),
            fx.user("<local-command-stdout></local-command-stdout>", t0 + 4, self.cwd),
            fx.user("<task-notification>x</task-notification>", t0 + 5, self.cwd),
            fx.user("[Image: source: /tmp/a.png]", t0 + 6, self.cwd),
        ])
        self.assertEqual((t.last_user_prompt, t.last_role), ("real", "assistant"))

    def test_sidechains_are_skipped(self):
        t0 = self.now - 100
        t = self.read([fx.user("main", t0, self.cwd), fx.assistant("main reply", t0 + 1, self.cwd),
                       fx.user("sub", t0 + 2, self.cwd, isSidechain=True),
                       fx.assistant("sub reply", t0 + 3, self.cwd, isSidechain=True)])
        self.assertEqual((t.entries, t.last_user_prompt, t.last_assistant_text), (2, "main", "main reply"))

    def test_no_cwd_means_no_transcript(self):
        self.assertIsNone(self.read([{"type": "summary", "summary": "s"}, {"not": "an entry"}]))

    def test_large_files_are_read_head_and_tail(self):
        t0 = self.now - 5000
        filler = "x" * 2000
        entries = [fx.user("the first prompt", t0, self.cwd)]
        entries += [fx.assistant(f"{i} {filler}", t0 + i, self.cwd) for i in range(1, 400)]
        entries += [fx.user("the last prompt", t0 + 400, self.cwd), fx.assistant("the end", t0 + 401, self.cwd)]
        t = self.read(entries)
        self.assertLess(t.entries, len(entries))
        self.assertEqual((t.first_user_prompt, t.last_user_prompt, t.last_assistant_text),
                         ("the first prompt", "the last prompt", "the end"))


class Runtimes(Case):
    def test_status_and_liveness(self):
        fx.write_runtime(self.home, "live", self.cwd, os.getpid(), "busy", updated=self.now - 3)
        fx.write_runtime(self.home, "gone", self.cwd, fx.dead_pid(), "busy")
        rts = fx.read_runtimes()
        live, gone = rts["live"], rts["gone"]
        self.assertEqual((live.pid, live.cwd, live.status, live.alive), (os.getpid(), self.cwd, "busy", True))
        self.assertAlmostEqual(live.updated_at, self.now - 3, places=2)
        self.assertEqual((gone.status, gone.alive), ("exited", False))

    def test_newest_record_wins_and_junk_is_skipped(self):
        fx.write_runtime(self.home, "dup", self.cwd, fx.dead_pid(), updated=self.now - 50)
        fx.write_runtime(self.home, "dup", self.cwd, os.getpid(), updated=self.now - 5)
        d = self.home / ".claude" / "sessions"
        (d / "1.json").write_text("{not json")
        (d / "2.json").write_text(json.dumps({"pid": 2, "cwd": self.cwd}))
        rts = fx.read_runtimes()
        self.assertEqual(list(rts), ["dup"])
        self.assertEqual(rts["dup"].pid, os.getpid())


class Hooks(Case):
    def settings(self):
        return json.loads((self.home / ".claude" / "settings.json").read_text())

    def ours(self, d):
        return {ev: [g for g in groups if any("nizam/hook.py" in h["command"] for h in g["hooks"])]
                for ev, groups in d["hooks"].items()}

    def test_install_keeps_the_users_own_settings(self):
        f = self.home / ".claude" / "settings.json"
        f.parent.mkdir(parents=True)
        mine = {"type": "command", "command": "say done"}
        original = {"model": "opus", "hooks": {"Stop": [{"hooks": [mine]}]}}
        f.write_text(json.dumps(original))
        with contextlib.redirect_stdout(io.StringIO()):
            cli.install_hooks()
            cli.install_hooks()
        d = self.settings()
        self.assertEqual(d["model"], "opus")
        self.assertEqual(d["hooks"]["Stop"][0], {"hooks": [mine]})
        ours = self.ours(d)
        self.assertEqual({ev: [g.get("matcher") for g in gs] for ev, gs in ours.items()}, {
            "SessionStart": [None], "UserPromptSubmit": [None], "Stop": [None], "SessionEnd": [None],
            "ElicitationResult": [None],
            "Notification": ["permission_prompt|idle_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input"],
            "PreToolUse": ["AskUserQuestion|ExitPlanMode"], "PostToolUse": ["AskUserQuestion|ExitPlanMode"]})
        hook = ours["Stop"][0]["hooks"][0]
        self.assertEqual((hook["type"], hook["timeout"], hook["async"]), ("command", 5, True))
        script, provider = hook["command"].split()[-2:]
        self.assertTrue(Path(script).is_file())
        self.assertEqual(provider, "claude")
        self.assertEqual(json.loads(f.with_suffix(".json.bak-nizam").read_text()), original)

        with contextlib.redirect_stdout(io.StringIO()):
            fx.uninstall_hooks()
        self.assertEqual(self.settings(), original)

    def test_install_from_nothing_and_uninstall_to_nothing(self):
        (self.home / ".claude").mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            cli.install_hooks()
            self.assertEqual(len(self.settings()["hooks"]), 8)
            fx.uninstall_hooks()
        self.assertEqual(self.settings(), {})


class HookScript(Case):
    def fire(self, payload):
        subprocess.run([sys.executable, str(REPO / "nizam" / "hook.py")], input=json.dumps(payload), text=True,
                       env={**os.environ, "HOME": str(self.home)}, check=True)
        f = self.home / ".nizam" / "events.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def test_event_line(self):
        evs = self.fire({"session_id": SID, "cwd": self.cwd, "hook_event_name": "Notification",
                         "notification_type": "permission_prompt", "message": "needs Bash",
                         "transcript_path": "/x.jsonl", "tool_response": {"big": "blob"},
                         "prompt": "p" * 900})
        self.assertEqual(len(evs), 1)
        ev = evs[0]
        self.assertAlmostEqual(ev.pop("ts"), time.time(), delta=30)
        self.assertEqual(ev, {"session_id": SID, "cwd": self.cwd, "hook_event_name": "Notification",
                              "notification_type": "permission_prompt", "message": "needs Bash",
                              "transcript_path": "/x.jsonl", "prompt": "p" * 600})

    def test_stamps_the_provider_it_was_installed_for(self):
        subprocess.run([sys.executable, str(REPO / "nizam" / "hook.py"), "fake"], text=True,
                       input=json.dumps({"session_id": SID, "hook_event_name": "Stop"}),
                       env={**os.environ, "HOME": str(self.home)}, check=True)
        ev, = self.fire({"session_id": SID, "hook_event_name": "Stop", "agent_id": "sub-1"})
        self.assertEqual(ev["provider"], "fake")

    def test_question_text(self):
        ev = self.fire({"session_id": SID, "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                        "tool_input": {"questions": [{"question": "Which ledger?"}, {"question": "second"}]}})[0]
        self.assertEqual((ev["tool_name"], ev["question"]), ("AskUserQuestion", "Which ledger?"))

    def test_sub_agents_and_garbage_are_silent(self):
        self.assertEqual(self.fire({"session_id": SID, "hook_event_name": "Stop", "agent_id": "sub-1"}), [])
        subprocess.run([sys.executable, str(REPO / "nizam" / "hook.py")], input="{not json", text=True,
                       env={**os.environ, "HOME": str(self.home)}, check=True)


class Launch(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(terminal, "run_in_new_terminal")
        self.run = p.start()
        self.addCleanup(p.stop)

    def test_start(self):
        sid = terminal.start("/w/my agent", prompt=" it's time ", permission_mode="plan", paste_only=True,
                             launcher="Ghostty")
        self.run.assert_called_once_with(
            f"cd '/w/my agent' && claude --session-id {sid} --permission-mode plan 'it'\"'\"'s time'",
            True, "Ghostty", "/w/my agent")

    def test_start_modes(self):
        for mode, flag in (("acceptEdits", " --permission-mode acceptEdits"), ("auto", " --permission-mode auto"),
                           ("default", ""), ("bogus; rm -rf", "")):
            with self.subTest(mode):
                self.run.reset_mock()
                sid = terminal.start("/w", permission_mode=mode)
                self.assertEqual(self.run.call_args.args[0], f"cd /w && claude --session-id {sid}{flag}")

    def test_start_in_a_worktree(self):
        with mock.patch.object(terminal, "git_root", lambda cwd: "/w/repo"):
            sid = terminal.start("/w/repo/sub", worktree=True)
        shell, _, _, where = self.run.call_args.args
        self.assertRegex(shell, r"^git -C /w/repo worktree add -b claude/(\d{8}-\d{6}) /w/repo-\1 "
                                rf"&& cd /w/repo-\1 && claude --session-id {sid} --permission-mode acceptEdits$")
        self.assertEqual(where, "/w")

    def test_resume(self):
        self.assertTrue(terminal.resume(SID, "/w/my agent"))
        self.run.assert_called_once_with(f"cd '/w/my agent' && claude --resume {SID}", False, "Terminal", "/w/my agent")
        self.assertFalse(terminal.resume("x; rm -rf ~", "/w"))

    def test_desktop_session_opens_by_the_apps_own_id(self):
        from nizam.providers import claude
        store = Path(tempfile.mkdtemp())
        org = store / "acct" / "org"
        org.mkdir(parents=True)
        (org / "local_abc-1.json").write_text(json.dumps({"sessionId": "local_abc-1", "cliSessionId": SID}))
        (org / "local_bad.json").write_text("{")
        s = {"id": SID, "live": False, "pid": None, "cwd": "/w", "title": "t", "provider": "claude"}
        with mock.patch.object(claude, "DESKTOP_SESSIONS", store), \
             mock.patch.object(terminal.subprocess, "Popen") as popen:
            self.assertTrue(terminal.open_session(s))
            self.assertEqual(popen.call_args.args[0],
                             ["open", "claude://code/continue?session=local_abc-1&source=nizam"])
            self.run.assert_not_called()
            self.assertTrue(terminal.open_session({**s, "id": SID.replace("2", "3")}))
            self.assertEqual(popen.call_count, 1)
        self.run.assert_called_once()

    def test_clean_env(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "s", "CLAUDECODE": "1", "AI_AGENT": "a",
                                          "ENABLE_CLAUDEAI_MCP_SERVERS": "1", "NIZAM_KEEP": "1"}):
            env = terminal.clean_env()
        self.assertEqual(env.get("NIZAM_KEEP"), "1")
        self.assertFalse([k for k in env if k.startswith("CLAUDE") or k in ("AI_AGENT", "ENABLE_CLAUDEAI_MCP_SERVERS")])


FAKE_CLAUDE = """#!{python}
import json, os, sys
out = {out!r}
n = len([f for f in os.listdir(out) if f.startswith("call")])
json.dump({{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd(),
           "env": dict(os.environ)}}, open(os.path.join(out, f"call{{n}}.json"), "w"))
print("plain noise")
print(json.dumps({{"type": "system", "subtype": "init"}}))
print(json.dumps({result}))
sys.exit({code})
"""


class Headless(Case):
    def setUp(self):
        super().setUp()
        self.agent = self.home / "work" / "finance"
        (self.agent / "routines").mkdir(parents=True)
        (self.agent / "CLAUDE.md").write_text("finance")
        self.bin = self.home / "bin"
        self.out = self.home / "out"
        self.bin.mkdir(); self.out.mkdir()
        store = self.home / ".nizam" / "routines"
        self.records = []
        for p in (mock.patch.object(routines, "LOCKS_DIR", store / "locks"),
                  mock.patch.object(routines, "LOGS_DIR", store / "logs"),
                  mock.patch.object(routines, "record", self.records.append),
                  mock.patch.object(runner, "_login_path", lambda: str(self.bin)),
                  mock.patch.object(runner, "POLL", 0.05), mock.patch.object(runner, "RETRY_DELAY", 0),
                  mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "outer", "AI_AGENT": "x"})):
            p.start()
            self.addCleanup(p.stop)

    def fake_claude(self, result, code=0):
        f = self.bin / "claude"
        f.write_text(FAKE_CLAUDE.format(python=sys.executable, out=str(self.out), result=result, code=code))
        f.chmod(0o755)

    def routine(self, front="schedule: daily 09:30\n"):
        f = self.agent / "routines" / "daily.md"
        f.write_text(f"---\n{front}---\nClose the books.\n")
        return str(f)

    def go(self, path):
        with contextlib.redirect_stdout(io.StringIO()):
            return runner.run(path)

    def calls(self):
        return [json.loads(f.read_text()) for f in sorted(self.out.glob("call*.json"))]

    def test_completed_run(self):
        self.fake_claude({"type": "result", "is_error": False, "total_cost_usd": 0.12, "num_turns": 3,
                          "result": "Books closed.\nOUTCOME: COMPLETE"})
        code = self.go(self.routine("schedule: daily 09:30\nmodel: sonnet\npermission_mode: acceptEdits\n"
                                    "allowed_tools: Read, Bash(git log, -n 5)\ndisallowed_tools: Write\n"))
        call, = self.calls()
        sid = call["argv"][5]
        self.assertEqual(call["argv"], [
            "-p", "--output-format", "stream-json", "--verbose", "--session-id", sid,
            "--append-system-prompt", runner.HEADLESS.format(name="daily"), "--model", "sonnet",
            "--permission-mode", "acceptEdits", "--allowedTools", "Read,Bash(git log, -n 5)",
            "--disallowedTools", "Write"])
        self.assertRegex(call["stdin"], r"^Current date/time: \d{4}-\d\d-\d\d \d\d:\d\d .*\n\nClose the books\.\n$")
        self.assertEqual(Path(call["cwd"]).resolve(), self.agent)
        self.assertEqual(call["env"]["NIZAM_ROUTINE"], self.records[-1]["routine"])
        self.assertFalse([k for k in call["env"] if k.startswith("CLAUDE") or k == "AI_AGENT"])

        self.assertEqual([r["status"] for r in self.records], ["running", "running", "completed"])
        last = self.records[-1]
        self.assertEqual(code, 0)
        self.assertEqual((last["summary"], last["sessions"], last["attempts"], last["cost"], last["turns"],
                          last["exit_code"]), ("Books closed.", [sid], 1, 0.12, 3, 0))
        log = Path(last["log"])
        self.assertEqual(log.suffix, ".jsonl")
        self.assertIn('"type": "result"', log.read_text())

    def test_bare_routine_passes_no_optional_flags(self):
        self.fake_claude({"type": "result", "result": "OUTCOME: COMPLETE"})
        self.go(self.routine())
        self.assertEqual(len(self.calls()[0]["argv"]), 8)

    def test_no_outcome_line_is_incomplete(self):
        self.fake_claude({"type": "result", "result": "I think it went fine."})
        self.assertEqual(self.go(self.routine()), 1)
        last = self.records[-1]
        self.assertEqual(last["status"], "incomplete")
        self.assertTrue(last["summary"].endswith("I think it went fine."))
        self.assertIn("without an OUTCOME line", last["summary"])

    def test_quick_error_is_retried_once(self):
        self.fake_claude({"type": "result", "is_error": True, "result": "Credit balance is too low"}, code=1)
        self.assertEqual(self.go(self.routine()), 1)
        last = self.records[-1]
        self.assertEqual((last["status"], last["summary"], last["attempts"], len(self.calls())),
                         ("errored", "Credit balance is too low", 2, 2))
        self.assertEqual(len(set(last["sessions"])), 2)

    def test_crash_without_a_result(self):
        self.fake_claude(None, code=3)
        self.go(self.routine())
        last = self.records[-1]
        self.assertEqual(last["status"], "errored")
        self.assertIn("plain noise", last["summary"])

    def test_cli_not_found(self):
        self.go(self.routine())
        last = self.records[-1]
        self.assertEqual((last["status"], last["summary"]),
                         ("errored", "The claude command was not found on the login shell's PATH."))

    def test_script_routines_never_touch_the_cli(self):
        self.go(self.routine("schedule: daily 09:30\ncommand: echo hello\n"))
        last = self.records[-1]
        self.assertEqual((last["status"], last["summary"], self.calls()), ("completed", "hello", []))


if __name__ == "__main__":
    unittest.main()
