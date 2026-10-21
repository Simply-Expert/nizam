import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import fixtures  # noqa: F401  (puts the repo on sys.path)
from nizam import providers, remote
from nizam.providers.claude import Claude

SERVER = """
import sys, time
print("Connecting", flush=True)
time.sleep(0.2)
print("Connected \\x1b[1mhttps://claude.ai/code?environment=env_PAGE1\\x1b[0m", flush=True)
time.sleep(0.3)
print("\\x1b]8;;https://claude.ai/code/session_FIRST?from=cli\\x07Attached\\x1b]8;;\\x07", flush=True)
while True:
    print("." * 200, flush=True)
    time.sleep(0.01)
"""
BROKEN = """
print("Not logged in. Run claude and sign in first.", flush=True)
raise SystemExit(1)
"""


class FakeRemote(Claude):
    name = "fake"
    cli = Path(sys.executable).name

    def __init__(self, script: Path) -> None:
        self.script = script

    def remote_command(self, exe, name, permission_mode):
        return [exe, str(self.script), "--name", name]


class Remote(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.agent = self.tmp / "marketing"
        self.agent.mkdir()
        self.server = self.tmp / "server.py"
        self.server.write_text(SERVER)
        store = self.tmp / "remote"
        self.patches = [mock.patch.object(remote, "REMOTE_DIR", store),
                        mock.patch.object(remote, "AWAKE_FILE", store / "awake.json"),
                        mock.patch.object(remote, "login_path", lambda: str(Path(sys.executable).parent)),
                        mock.patch.dict(providers._REGISTRY, {"fake": FakeRemote(self.server)})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for rec in remote.awake().values():
            remote.stop(Path(rec["root"]))
        for p in self.patches:
            p.stop()

    def test_wake_finds_links_then_stop_ends_it(self):
        rec = remote.wake(self.agent, "Marketing", provider="fake")
        self.assertEqual(rec["session_url"], "https://claude.ai/code/session_FIRST")
        self.assertEqual(rec["page_url"], "https://claude.ai/code?environment=env_PAGE1")
        self.assertEqual(list(remote.awake()), [str(self.agent)])
        again = remote.wake(self.agent, "Marketing", provider="fake")
        self.assertTrue(again["already"])
        self.assertEqual(again["pid"], rec["pid"])
        self.assertTrue(remote.stop(self.agent))
        self.assertEqual(remote.awake(), {})
        self.assertFalse(remote.stop(self.agent))
        self.assertRaises(ProcessLookupError, os.kill, rec["pid"], 0)

    def test_a_server_that_exits_says_why(self):
        self.server.write_text(BROKEN)
        with self.assertRaises(remote.WakeError) as e:
            remote.wake(self.agent, "Marketing", provider="fake")
        self.assertIn("Not logged in", str(e.exception))
        self.assertEqual(remote.awake(), {})

    def test_a_missing_folder_is_refused(self):
        with self.assertRaises(remote.WakeError):
            remote.wake(self.tmp / "gone", "Gone", provider="fake")

    def test_trim_empties_a_long_log_and_keeps_the_links(self):
        rec = remote.wake(self.agent, "Marketing", provider="fake")
        log = Path(rec["log"])
        with mock.patch.object(remote, "LOG_CAP", 4096):
            deadline = time.time() + 10
            while log.stat().st_size <= 4096 and time.time() < deadline:
                time.sleep(0.05)
            remote.trim()
        self.assertLess(log.stat().st_size, 4096)
        kept = remote.awake()[str(self.agent)]
        self.assertEqual(kept["session_url"], rec["session_url"])

    def test_a_reused_pid_is_not_taken_for_the_server(self):
        remote._write({str(self.agent): {"root": str(self.agent), "pid": os.getpid(), "argv": ["no-such-server"],
                                         "log": str(self.tmp / "x.log"), "started": time.time()}})
        self.assertEqual(remote.awake(), {})

    def test_tail_reads_through_redraws(self):
        text = "\x1b[1A\x1b[J· Connecting\n\x1b[7A\x1b[J· Connected\n· Connected\nOpen \x1b]8;;https://x\x07here\x1b]8;;\x07\n"
        self.assertEqual(remote.tail(text), "· Connecting\n· Connected\nOpen here")


class ClaudeRemote(unittest.TestCase):
    def test_remote_command(self):
        c = Claude()
        self.assertEqual(c.remote_command("/bin/claude", "Marketing", "default"),
                         ["/bin/claude", "remote-control", "--name", "Marketing"])
        self.assertEqual(c.remote_command("/bin/claude", "M", "acceptEdits")[-2:], ["--permission-mode", "acceptEdits"])
        self.assertNotIn("--permission-mode", c.remote_command("/bin/claude", "M", "yolo"))

    def test_remote_links_take_the_newest(self):
        out = ("https://claude.ai/code?environment=env_A1\n"
               "\x1b]8;;https://claude.ai/code/session_01Old?from=cli\x07Attached\x1b]8;;\x07\n"
               "\x1b]8;;https://claude.ai/code/session_02New?from=cli\x07Attached\x1b]8;;\x07\n")
        self.assertEqual(Claude().remote_links(out),
                         ("https://claude.ai/code/session_02New", "https://claude.ai/code?environment=env_A1"))
        self.assertEqual(Claude().remote_links("Connecting"), (None, None))


if __name__ == "__main__":
    unittest.main()
