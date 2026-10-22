import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fixtures  # noqa: F401  (puts the repo on sys.path)
from nizam import launch as L


class Login(unittest.TestCase):
    def setUp(self):
        self.plist = Path(tempfile.mkdtemp()) / "co.nizam.app.plist"
        self.patch = mock.patch.object(L, "LAUNCH_AGENT", str(self.plist))
        self.patch.start()
        L.set_login(True)

    def tearDown(self):
        self.patch.stop()

    def test_a_crash_brings_the_app_back_but_quit_does_not(self):
        with open(self.plist, "rb") as f:
            self.assertEqual(plistlib.load(f)["KeepAlive"], {"SuccessfulExit": False})

    def test_launchd_is_left_alone_while_an_app_runs(self):
        with mock.patch.object(L, "app_running", lambda: True), mock.patch.object(L.subprocess, "run") as run:
            self.assertFalse(L.start_with_launchd())
        run.assert_not_called()

    def test_the_login_item_is_reloaded_then_started(self):
        ok = mock.Mock(returncode=0)
        with mock.patch.object(L, "app_running", lambda: False), \
             mock.patch.object(L.subprocess, "run", return_value=ok) as run:
            self.assertTrue(L.start_with_launchd())
        self.assertEqual([c.args[0][1] for c in run.call_args_list], ["bootout", "bootstrap"])

    def test_turning_login_off_does_not_quit_the_running_app(self):
        with mock.patch.object(L.subprocess, "run") as run:
            L.set_login(False)
        run.assert_not_called()
        self.assertFalse(L.login_enabled())


if __name__ == "__main__":
    unittest.main()
