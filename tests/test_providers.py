"""A second provider that names nothing of Claude Code, to keep the seam honest."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fixtures as fx
from nizam import providers, runner, state, terminal
from nizam.providers.base import (ACTIVITY, NEEDS, TURN_DONE, HeadlessResult, Provider, Runtime, Signal,
                                  Transcript)

SID = "33333333-3333-4333-8333-333333333333"


FAKE_CLI = """#!{python}
import json, sys
json.dump({{"argv": sys.argv[1:], "stdin": sys.stdin.read()}}, open({out!r}, "w"))
print(json.dumps({{"started": "{sid}"}}))
print(json.dumps({{"final": "Done.\\nOUTCOME: COMPLETE"}}))
"""


class Fake(Provider):
    name = "fake"
    cli = "fakecli"
    takes_session_id = False

    def __init__(self, cwd: str, now: float):
        self.cwd, self.now = cwd, now

    def transcripts(self, max_age_secs):
        return [Transcript(session_id=SID, path=Path("/nowhere"), cwd=self.cwd, mtime=self.now - 10, provider="fake",
                           first_user_prompt="hello", last_role="assistant", last_assistant_text="hi", entries=2)]

    def runtimes(self):
        return {SID: Runtime(os.getpid(), SID, self.cwd, "idle", None, self.now - 10, self.now - 60)}

    def signal(self, event):
        return {"blocked": Signal(NEEDS, "Approve?", event.get("why", "")),
                "finished": Signal(TURN_DONE)}.get(event.get("what"), Signal(ACTIVITY))

    def start_command(self, session_id, prompt, permission_mode):
        return f"fakecli new {session_id}"

    def headless_command(self, exe, session_id, system_prompt, prompt, meta):
        return [exe, "run", str(session_id)], f"{system_prompt}\n\n{prompt}"

    def headless_session_id(self, line):
        return json.loads(line).get("started")

    def headless_result(self, line):
        final = json.loads(line).get("final")
        return HeadlessResult(text=final) if final else None

    def resume_command(self, session_id):
        return f"fakecli continue {session_id}"

    def clean_env(self, env):
        return {k: v for k, v in env.items() if k != "FAKECLI_SESSION"}

    def session_id(self, env):
        return env.get("FAKECLI_SESSION")


class SecondProvider(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp()).resolve()
        agent = self.home / "work" / "ops"
        agent.mkdir(parents=True)
        (agent / "AGENTS.md").write_text("ops")
        self.now = time.time()
        fake = Fake(str(agent), self.now)
        for p in (*fx.point_at(self.home), mock.patch.dict(providers._REGISTRY, {"fake": fake})):
            p.start()
            self.addCleanup(p.stop)

    def row(self, board):
        return next(s for s in board._build(self.now)["sessions"] if s["id"] == SID)

    def test_on_the_board_with_its_own_signals(self):
        board = state.Board()
        s = self.row(board)
        self.assertEqual((s["provider"], s["agent_name"], s["bucket"]), ("fake", "ops", "inbox"))
        fx.append_event(self.home, SID, "anything", self.now - 5, provider="fake", what="blocked", why="rm -rf build")
        s = self.row(board)
        self.assertEqual((s["bucket"], s["label"], s["detail"]), ("needs", "Approve?", "rm -rf build"))
        fx.append_event(self.home, SID, "anything", self.now - 2, provider="fake", what="finished")
        self.assertEqual(self.row(board)["bucket"], "inbox")

    def test_launches_with_its_own_commands(self):
        with mock.patch.object(terminal, "run_in_new_terminal") as run:
            terminal.resume(SID, "/w", provider="fake")
            sid = terminal.start("/w", provider="fake")
        self.assertIsNone(sid)
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [f"cd /w && fakecli continue {SID}", "cd /w && fakecli new None"])

    def test_headless_run_reports_the_id_the_tool_minted(self):
        exe, out = self.home / "fakecli", self.home / "call.json"
        exe.write_text(FAKE_CLI.format(python=sys.executable, out=str(out), sid=SID))
        exe.chmod(0o755)
        r = SimpleNamespace(name="daily", meta={}, prompt="Close the books.", cwd=self.home, timeout=30)
        with mock.patch.object(runner, "POLL", 0.05):
            res = runner._attempt(r, providers.get("fake"), str(exe), dict(os.environ), self.home / "run.log")
        call = json.loads(out.read_text())
        self.assertEqual((res["status"], res["session"], res["summary"]), ("completed", SID, "Done."))
        self.assertEqual(call["argv"], ["run", "None"])
        self.assertTrue(call["stdin"].startswith(runner.HEADLESS.format(name="daily") + "\n\nCurrent date/time: "))
        self.assertTrue(call["stdin"].endswith("Close the books.\n"))

    def test_env_and_fallback(self):
        with mock.patch.dict(os.environ, {"FAKECLI_SESSION": "f-1", "CLAUDECODE": "1"}):
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            self.assertEqual(providers.current_session_id(), "f-1")
            env = providers.clean_env()
        self.assertFalse({"FAKECLI_SESSION", "CLAUDECODE"} & set(env))
        self.assertEqual(providers.get("no such tool").name, "claude")
        self.assertEqual(providers.get(None).name, "claude")


if __name__ == "__main__":
    unittest.main()
