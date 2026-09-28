import re
import unittest
from pathlib import Path

import fixtures  # noqa: F401  (puts the repo on sys.path)
import nizam

WHATSNEW = Path(__file__).resolve().parents[1] / "WHATSNEW.md"


class WhatsNew(unittest.TestCase):
    def test_every_release_says_what_changed(self):
        text = WHATSNEW.read_text(encoding="utf-8")
        versions = re.findall(r"^## (\d+\.\d+\.\d+)\b", text, flags=re.M)
        self.assertTrue(versions, "WHATSNEW.md has no release sections")
        self.assertEqual(versions[0], nizam.__version__,
                         f"add a '## {nizam.__version__}' section to the top of WHATSNEW.md")


if __name__ == "__main__":
    unittest.main()
