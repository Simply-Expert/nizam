import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nizam import update as U


def git(where: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(where), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class Update(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.origin = self.tmp / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        self.release("v0.1.0")
        self.src = self.tmp / "src"
        self.patches = [mock.patch.object(U, "SRC", self.src), mock.patch.object(U, "NIZAM_DIR", self.tmp),
                        mock.patch.object(U, "CACHE_FILE", self.tmp / "update.json"),
                        mock.patch.object(U, "_info", None), mock.patch.object(U, "_next_check", 0.0)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def release(self, tag: str | None):
        (self.origin / "f").write_text(tag or "untagged")
        git(self.origin, "add", "f")
        git(self.origin, "commit", "-q", "-m", tag or "untagged")
        if tag:
            git(self.origin, "tag", tag)

    def clone(self, ref: str):
        git(self.tmp, "clone", "-q", "--branch", ref, str(self.origin), str(self.src))

    def test_newest_tag_orders_by_number(self):
        out = "a\trefs/tags/v0.9.0\nb\trefs/tags/v0.10.0\nc\trefs/tags/vnext\nd\trefs/tags/v0.2"
        self.assertEqual(U.newest_tag(out), "v0.10.0")
        self.assertIsNone(U.newest_tag(""))

    def test_release_install_moves_to_the_newest_tag(self):
        self.clone("v0.1.0")
        self.assertIsNone(U.check()["latest"])
        self.release("v0.2.0")
        self.release(None)
        info = U.check()
        self.assertEqual((info["current"], info["latest"]), ("v0.1.0", "v0.2.0"))
        self.assertEqual(U.installed()["ref"], "v0.1.0", "checking must not move the checkout")
        U.apply(info)
        self.assertEqual(U.installed()["ref"], "v0.2.0")
        self.assertFalse(U.CACHE_FILE.exists())
        self.assertIsNone(U.check()["latest"])

    def test_branch_checkout_fast_forwards(self):
        self.clone("main")
        self.assertIsNone(U.check()["latest"])
        self.release(None)
        info = U.check()
        self.assertTrue(info["latest"].startswith("main@"))
        U.apply(info)
        self.assertEqual(git(self.src, "rev-parse", "HEAD"), git(self.origin, "rev-parse", "HEAD"))

    def test_a_checkout_ahead_of_origin_has_nothing_to_fetch(self):
        self.clone("main")
        (self.src / "g").write_text("local")
        git(self.src, "add", "g")
        git(self.src, "commit", "-q", "-m", "local")
        self.assertIsNone(U.check()["latest"])

    def test_local_changes_stop_the_update(self):
        self.clone("v0.1.0")
        self.release("v0.2.0")
        (self.src / "f").write_text("edited")
        with self.assertRaisesRegex(U.UpdateError, "local changes"):
            U.apply(U.check())
        self.assertEqual(U.installed()["ref"], "v0.1.0")

    def test_a_bare_commit_is_left_alone(self):
        self.clone("main")
        git(self.src, "checkout", "-q", "--detach")
        git(self.src, "tag", "-d", "v0.1.0")
        with self.assertRaises(U.UpdateError):
            U.check()

    def test_poll_reuses_a_fresh_cache_for_this_install_only(self):
        self.clone("v0.1.0")
        self.release("v0.2.0")
        U.check()
        for stale, expect in ((False, "v0.2.0"), (True, None)):
            with mock.patch.object(U, "_info", None), mock.patch.object(U, "_next_check", 0.0), \
                    mock.patch.object(U, "check", side_effect=U.UpdateError("offline")) as check:
                if stale:
                    git(self.src, "fetch", "-q", "--tags")
                    git(self.src, "checkout", "-q", "v0.2.0")
                U.poll()
                U._thread.join()
                self.assertEqual((U.poll() or {}).get("latest"), expect)
                self.assertEqual(check.called, stale)


if __name__ == "__main__":
    unittest.main()
