import tempfile
import unittest
from pathlib import Path

import fixtures  # noqa: F401  (puts the repo on sys.path)
from nizam import agents as A


class Areas(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def area(self, rel: str):
        (self.root / rel).mkdir(parents=True, exist_ok=True)
        (self.root / rel / A.AREA_MARKER).write_text("rules")

    def test_areas_nest(self):
        self.area("channels")
        self.area("channels/email")
        areas = A._scan_areas(self.root)
        self.assertEqual(areas["channels/email"].parent, "channels")
        self.assertEqual(areas["channels"].children, ["channels/email"])

    def test_a_folder_of_hundreds_of_folders_is_data_and_not_searched(self):
        for i in range(A.MAX_FANOUT + 1):
            (self.root / "uploads" / str(i)).mkdir(parents=True)
        self.area("uploads/7")
        self.area("channels/email")
        self.assertEqual(sorted(A._scan_areas(self.root)), ["channels/email"])

    def test_a_session_under_an_old_symlinked_path_belongs_to_the_moved_agent(self):
        real = self.root.resolve() / "Agents" / "growth"
        real.mkdir(parents=True)
        (real / "AGENTS.md").write_text("role")
        self.area("Agents/growth/channels/email")
        old = self.root / "MarketingReport"
        old.symlink_to(real)
        agent, area = A.resolve(old / "channels" / "email")
        self.assertEqual(agent.root, real)
        self.assertEqual(area and area.rel, "channels/email")


if __name__ == "__main__":
    unittest.main()
