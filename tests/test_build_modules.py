"""
test_build_modules.py - every Shelfrip module is listed in build_installer.MODULES, so the
installed app has it even when no other module imports it at the top (handbrake.py, 2026-11-27).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOT_MODULES = {"build_installer", "build_linux", "launcher"}  # the build script itself, and the exe's entry point


class BuildModules(unittest.TestCase):
    def test_every_module_is_bundled(self):
        text = (ROOT / "build_installer.py").read_text(encoding="utf-8")
        listed = set(re.findall(r'"(\w+)"', re.search(r"MODULES\s*=\s*\[(.*?)\]", text, re.S).group(1)))
        here = {p.stem for p in ROOT.glob("*.py")} - NOT_MODULES
        self.assertEqual(sorted(here - listed), [])

    def test_linux_package_includes_runtime_modules(self):
        import runpy
        listed = set(runpy.run_path(str(ROOT / "build_linux.py"))["MODULES"])
        here = {p.stem for p in ROOT.glob("*.py")} - NOT_MODULES
        self.assertEqual(sorted(here - listed), [])


if __name__ == "__main__":
    unittest.main()
