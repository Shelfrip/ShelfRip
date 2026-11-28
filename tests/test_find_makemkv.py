"""
test_find_makemkv.py - the MakeMKV setting pointing at makemkv.exe (MakeMKV's window) or its
folder still finds makemkvcon, instead of opening MakeMKV's window on every rip (issue #39).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rip_all  # noqa: E402


class FindMakeMKV(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "MakeMKV"
        self.dir.mkdir()
        for name in ("makemkv.exe", "makemkvcon64.exe", "makemkvcon.exe"):
            (self.dir / name).write_bytes(b"")

    def tearDown(self):
        self.tmp.cleanup()

    def find(self, setting):
        return Path(rip_all.find_makemkv({"makemkvcon_path": str(setting)}))

    def test_window_program_uses_makemkvcon(self):
        self.assertEqual(self.find(self.dir / "makemkv.exe").name, "makemkvcon64.exe")

    def test_folder_uses_makemkvcon(self):
        self.assertEqual(self.find(self.dir).name, "makemkvcon64.exe")

    def test_quoted_path(self):
        self.assertEqual(self.find(f'"{self.dir / "makemkv.exe"}"').name, "makemkvcon64.exe")

    def test_chosen_makemkvcon_is_kept(self):
        self.assertEqual(self.find(self.dir / "makemkvcon.exe").name, "makemkvcon.exe")

    def test_32_bit_only(self):
        (self.dir / "makemkvcon64.exe").unlink()
        self.assertEqual(self.find(self.dir / "makemkv.exe").name, "makemkvcon.exe")


if __name__ == "__main__":
    unittest.main()
