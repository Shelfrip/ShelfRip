"""DVD fingerprint recipe and compatibility; no physical disc or video reads."""
import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discs
TMP = discs.setup_env({})
import discdb
import app
import catalog
import community
import rip_all


def digest(*sizes):
    return hashlib.md5(b"".join(struct.pack("<Q", n) for n in sizes)).hexdigest().upper()


class DVD(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dvd = self.root / "VIDEO_TS"
        self.dvd.mkdir()

    def test_normal_dvd_hash_is_unchanged(self):
        for name, size in (("VIDEO_TS.BUP", 2), ("VIDEO_TS.IFO", 3), ("VTS_01_1.VOB", 7)):
            (self.dvd / name).write_bytes(b"x" * size)
        with patch.object(Path, "open", side_effect=AssertionError("must only read metadata")):
            self.assertEqual(discdb.fingerprint(str(self.root)),
                             {"content_hash": digest(2, 3, 7), "layout": "dvd", "files": 3})

    def test_extra_files_change_legacy_hash_but_not_filtered_hash(self):
        (self.dvd / "README.txt").write_bytes(b"extra")
        (self.dvd / "VIDEO_TS.IFO").write_bytes(b"abc")
        (self.dvd / "BACKUP").mkdir()
        (self.dvd / "BACKUP/VTS_01_1.VOB").write_bytes(b"nested")
        result = discdb.fingerprint(str(self.root))
        self.assertEqual(result, {"content_hash": digest(3), "legacy_hash": digest(5, 3), "layout": "dvd", "files": 1})
        self.assertNotEqual(result["content_hash"], digest(5, 3))

    def test_case_insensitive_extensions_and_no_media_files(self):
        (self.dvd / "notes.txt").write_bytes(b"ignored")
        self.assertIsNone(discdb.fingerprint(str(self.root)))
        (self.dvd / "video_ts.iFo").write_bytes(b"abc")
        self.assertEqual(discdb.fingerprint(str(self.root))["content_hash"], digest(3))

    def extra_disc(self):
        (self.dvd / "README.txt").write_bytes(b"extra")
        (self.dvd / "VIDEO_TS.IFO").write_bytes(b"abc")
        return {**discdb.fingerprint(str(self.root)), "titles": [
            {"title_id": 0, "output_file": "t00.mkv", "source_file": "01.VOB",
             "size_bytes": 7, "duration_seconds": 120}]}

    def plan(self, title="Old record"):
        return {"kind": "movie", "title": title, "tmdb_id": "123", "disc_label": title,
                "format": "dvd", "items": [{"source": "t00.mkv", "action": "move", "role": "main"}]}

    def test_old_catalog_and_prerip_identification_match_without_migration(self):
        disc = self.extra_disc()
        db = self.root / "catalog.db"
        catalog.record(self.plan(), {**disc, "content_hash": disc["legacy_hash"]}, db)
        self.assertEqual(catalog.lookup(disc, disc["titles"], db)["title"], "Old record")
        original_connect = catalog.connect
        with patch.object(catalog, "DB_PATH", db), patch.object(catalog, "connect", side_effect=lambda *args: original_connect(db)), \
                patch.object(discdb, "fingerprint", return_value=disc):
            self.assertEqual(app.identify_disc("D:", "unrelated", {"use_thediscdb": False})["title"], "Old record")
            catalog.record(self.plan("New record"), disc, db)
            self.assertEqual(app.identify_disc("D:", "unrelated", {"use_thediscdb": False})["title"], "New record")
        self.assertEqual(catalog.lookup(disc, disc["titles"], db)["title"], "New record")
        with original_connect(db) as con:
            rows = dict(con.execute("SELECT title, content_hash FROM discs"))
        con.close()
        self.assertEqual(rows, {"Old record": disc["legacy_hash"], "New record": disc["content_hash"]})

    def test_shared_lookup_order_and_new_submissions(self):
        import urllib.parse
        disc = self.extra_disc()
        answer = {"found": True, "kind": "movie", "tmdb_id": "123", "title": "Matched",
                  "titles": [{"size_bytes": 7, "duration_s": 120, "role": "main", "kept": 1}]}
        expected = [{"h": [disc["content_hash"].lower()]}, {"h": [disc["legacy_hash"].lower()]},
                    {"s": [community.signature(disc)]}]
        for misses in range(3):
            with self.subTest(misses=misses), patch.object(community, "_http", side_effect=[{"found": False}] * misses + [dict(answer)]) as http:
                self.assertEqual(community.lookup(disc, disc["titles"], {})["title"], "Matched")
                queries = [urllib.parse.parse_qs(urllib.parse.urlsplit(c.args[0]).query) for c in http.call_args_list]
                self.assertEqual(queries, expected[:misses + 1])
        with patch.object(community, "install_id", return_value="test-only"):
            body = community.submission(self.plan(), disc, {})
        self.assertEqual(body["disc"]["content_hash"], disc["content_hash"].lower())
        self.assertNotIn("legacy_hash", body["disc"])

    def test_rip_metadata_retains_legacy_lookup_hint(self):
        import json
        disc = self.extra_disc()
        cfg = {**rip_all.DEFAULT_CONFIG, "output_root": str(self.root / "rips"),
               "eject_when_done": False, "retry_failed_titles": False, "min_length_seconds": 0,
               "skip_duplicate_features": False, "use_community": False, "use_thediscdb": False}
        def run(cfg, args, *unused, **kwargs):
            if args[1] == "mkv":
                (Path(args[-1]) / "t00.mkv").write_bytes(b"ripped")
            return 0, ""
        with patch.object(rip_all, "run_makemkv", side_effect=run), \
                patch.object(rip_all, "parse_disc_info", side_effect=lambda _: {"titles": [dict(disc["titles"][0])]}), \
                patch.object(discdb, "fingerprint", return_value=disc):
            rip_all.rip_disc(cfg, {"device": "D:", "index": 0, "disc_label": "TEST", "drive_name": "Fixture"})
        saved = json.loads(next((self.root / "rips").rglob("disc_info.json")).read_text())
        self.assertEqual(saved["content_hash"], disc["content_hash"])
        self.assertEqual(saved["legacy_hash"], disc["legacy_hash"])


if __name__ == "__main__":
    unittest.main()
