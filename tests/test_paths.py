"""
test_paths.py - old copies of your settings next to the program are merged and moved aside.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = """CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE discs (id INTEGER PRIMARY KEY, content_hash TEXT, disc_label TEXT, title TEXT, tmdb_id TEXT);
CREATE TABLE titles (disc_id INTEGER, title_index INTEGER, name TEXT);"""


def db(path: Path, discs: list[tuple]) -> None:
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    for i, (label, title, tid) in enumerate(discs, 1):
        c.execute("INSERT INTO discs VALUES (?, '', ?, ?, ?)", (i, label, title, tid))
        c.execute("INSERT INTO titles VALUES (?, 0, ?)", (i, title))
    c.commit()
    c.close()


class OldCopies(unittest.TestCase):
    def test_merge_and_move(self):
        t = Path(tempfile.mkdtemp())
        app, data = t / "app", t / "data"
        (app / ".cache").mkdir(parents=True)
        (data / ".cache").mkdir(parents=True)
        shutil.copy2(ROOT / "paths.py", app / "paths.py")
        (app / "config.json").write_text("{}")
        (data / "config.json").write_text('{"a": 1}')
        db(app / "disc_catalog.db", [("ONE", "One", "1"), ("OLD_ONLY", "Old", "2")])
        db(data / "disc_catalog.db", [("ONE", "One", "1"), ("NEW", "New", "3")])
        (app / "learned_labels.json").write_text(json.dumps({"ONE": {"tmdb_id": "1"}, "OLD ONLY": {"tmdb_id": "2"}}))
        (data / "learned_labels.json").write_text(json.dumps({"ONE": {"tmdb_id": "1"}, "NEW": {"tmdb_id": "3"}}))
        env = {**os.environ, "SHELFRIP_DATA": str(data)}
        out = subprocess.run([sys.executable, "-c", "import paths; print(paths.MIGRATION_NOTE)"], cwd=app, env=env,
                             capture_output=True, text=True, check=True).stdout
        self.assertIn("Old settings (not used)", out)
        old = app / "Old settings (not used)"
        self.assertTrue((old / "config.json").exists() and (old / "disc_catalog.db").exists())
        self.assertFalse((app / "config.json").exists())
        labels = json.loads((data / "learned_labels.json").read_text())
        self.assertEqual(set(labels), {"ONE", "NEW", "OLD ONLY"})
        c = sqlite3.connect(data / "disc_catalog.db")
        self.assertEqual(sorted(r[0] for r in c.execute("SELECT disc_label FROM discs")), ["NEW", "OLD_ONLY", "ONE"])
        self.assertEqual(c.execute("SELECT COUNT(*) FROM titles").fetchone()[0], 3)
        # a second start does nothing
        out2 = subprocess.run([sys.executable, "-c", "import paths; print(repr(paths.MIGRATION_NOTE))"], cwd=app,
                              env=env, capture_output=True, text=True, check=True).stdout
        self.assertEqual(out2.strip(), "''")


class PortableCopy(unittest.TestCase):
    """A portable copy with no settings of its own starts from the installed app's, once."""

    def start(self, app: Path, home: Path, **extra) -> str:
        env = {k: v for k, v in os.environ.items() if k not in ("SHELFRIP_DATA", "DISC_RIPPER_DATA", "APPDATA")}
        env.update(XDG_CONFIG_HOME=str(home), APPDATA=str(home), HOME=str(home), **extra)
        return subprocess.run([sys.executable, "-c", "import paths; print(repr(paths.MIGRATION_NOTE))"], cwd=app,
                              env=env, capture_output=True, text=True, check=True).stdout.strip()

    def setUp(self):
        t = Path(tempfile.mkdtemp())
        self.app, self.home = t / "app", t / "home"
        self.app.mkdir()
        shutil.copy2(ROOT / "paths.py", self.app / "paths.py")
        (self.app / "portable.txt").write_text("")
        self.inst = self.home / ("Shelfrip" if os.name == "nt" else "shelfrip")
        if sys.platform == "darwin":
            self.inst = self.home / "Library/Application Support/Shelfrip"
        (self.inst / ".cache").mkdir(parents=True)
        (self.inst / "backups").mkdir()
        (self.inst / "config.json").write_text('{"rip_dir": "E:/Ripped"}')
        (self.inst / "shared.json").write_text("{}")
        (self.inst / ".cache" / "posters.json").write_text("{}")
        (self.inst / "shelfrip.log").write_text("log")
        (self.inst / "backups" / "config.json").write_text("{}")

    def test_copies_once(self):
        self.assertIn("portable now", self.start(self.app, self.home))
        self.assertEqual(json.loads((self.app / "config.json").read_text()), {"rip_dir": "E:/Ripped"})
        self.assertTrue((self.app / "shared.json").exists() and (self.app / ".cache" / "posters.json").exists())
        self.assertFalse((self.app / "shelfrip.log").exists() or (self.app / "backups").exists())
        self.assertTrue((self.inst / "config.json").exists())  # the installed app keeps its own
        (self.inst / "config.json").write_text('{"rip_dir": "F:/Other"}')
        self.assertEqual(self.start(self.app, self.home), "''")
        self.assertEqual(json.loads((self.app / "config.json").read_text()), {"rip_dir": "E:/Ripped"})

    def test_left_alone(self):
        (self.app / "config.json").write_text('{"mine": 1}')
        self.assertEqual(self.start(self.app, self.home), "''")
        self.assertEqual(json.loads((self.app / "config.json").read_text()), {"mine": 1})
        (self.app / "config.json").unlink()
        fresh = self.app.parent / "fresh"
        self.start(self.app, self.home, SHELFRIP_DATA=str(fresh))  # a chosen data folder is never filled in
        self.assertFalse((self.app / "config.json").exists() or (fresh / "config.json").exists())

    def test_not_portable(self):
        (self.app / "portable.txt").unlink()
        self.assertEqual(self.start(self.app, self.home), "''")
        self.assertFalse((self.app / "config.json").exists())


if __name__ == "__main__":
    unittest.main()
