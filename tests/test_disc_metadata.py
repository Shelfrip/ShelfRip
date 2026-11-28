"""Mounted-disc identity and optional scanner integration; no physical drive needed."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discs
TMP = discs.setup_env({})
import app
import discdb
import linux_support as linux
import rip_all


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux mount discovery")
class Mounts(unittest.TestCase):
    def resolve(self, rows, mounts, device="/dev/cdrom", unreadable=()):
        number = os.makedev(11, 0)
        def info(path, **kwargs):
            if str(path) == "/dev/sr0":
                return SimpleNamespace(st_rdev=number)
            value = mounts.get(str(path))
            if isinstance(value, Exception):
                raise value
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=value)
        with patch.object(linux, "optical_device", return_value="/dev/sr0") as optical, \
                patch.object(linux.os, "access", side_effect=lambda path, mode: str(path) not in unreadable), \
                patch.object(Path, "stat", info), patch.object(Path, "read_text", return_value=rows):
            result = linux.mounted_root(device)
        optical.assert_called_once_with(device)
        return result

    def test_alias_and_escaped_mountpoint_match_device_number_not_source(self):
        root = "/media/Film ü\twith space\\040"
        rows = r"40 1 11:0 / /media/Film\040ü\011with\040space\134040 ro shared:3 - udf /dev/disk/by-label/Film ro"
        self.assertEqual(self.resolve(rows, {root: os.makedev(11, 0)}), root)

    def test_rejects_subtree_wrong_device_hidden_and_inaccessible_mounts(self):
        rows = "\n".join([
            "broken", "40 1 11:0 /VIDEO_TS /subtree ro - udf /dev/sr0 ro",
            "41 1 11:1 / /other-drive ro - udf /dev/sr0 ro",
            "42 1 11:0 / /hidden ro - udf /dev/sr0 ro",
            "43 1 11:0 / /private ro - udf /dev/sr0 ro",
            "45 1 11:0 / /no-listing ro - udf /dev/sr0 ro",
            "44 1 11:0 / /full-bind ro - udf /dev/cdrom ro"])
        self.assertEqual(self.resolve(rows, {"/hidden": 55, "/private": PermissionError(),
                                            "/no-listing": os.makedev(11, 0),
                                            "/full-bind": os.makedev(11, 0)}, unreadable=("/no-listing",)), "/full-bind")

    def test_unmounted_invalid_and_unavailable_procfs_return_no_root(self):
        self.assertIsNone(self.resolve("", {}))
        with patch.object(linux, "optical_device", side_effect=ValueError("not optical")):
            self.assertIsNone(linux.mounted_root("/dev/sda"))
        with patch.object(linux, "optical_device", side_effect=FileNotFoundError()):
            self.assertIsNone(linux.mounted_root("/dev/sr9"))
        with patch.object(linux, "optical_device", return_value="/dev/sr0"), \
                patch.object(Path, "stat", return_value=SimpleNamespace(st_rdev=os.makedev(11, 0))), \
                patch.object(Path, "read_text", side_effect=PermissionError()):
            self.assertIsNone(linux.mounted_root("/dev/sr0"))


class Fingerprints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_matches_published_discdb_vectors_without_reading_video(self):
        # TheDiscDb/optical-disc-manifest examples/v1/{dvd,blu-ray}/basic.json
        # Normative recipe: specification/v1/README.md, thediscdb-content-hash.
        vectors = [({"VIDEO_TS/VIDEO_TS.IFO": 16384, "VIDEO_TS/VTS_01_0.IFO": 65536,
                     "VIDEO_TS/VTS_01_1.VOB": 1073709056}, "6EA135E733DB603765376D13F02A2D9D", "dvd"),
                   ({"BDMV/STREAM/01000.m2ts": 27027738624}, "0633802636ACA37258C27FBDAEB527A6", "bluray")]
        original_stat = Path.stat
        for i, (files, expected, layout) in enumerate(vectors):
            root = self.root / str(i)
            sizes = {}
            for name, size in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                sizes[path] = size
            # Non-media files and nested backups must not change the hash.
            next(iter(sizes)).parent.joinpath("README.txt").write_text("ignore")
            backup = next(iter(sizes)).parent / "BACKUP"
            backup.mkdir()
            (backup / next(iter(sizes)).name).write_bytes(b"not part of the hash")
            def metadata(path, **kwargs):
                values = list(original_stat(path, **kwargs))
                if path in sizes:
                    values[6] = sizes[path]
                return os.stat_result(values)
            with patch.object(Path, "stat", metadata), patch.object(Path, "open", side_effect=AssertionError("data read")):
                self.assertEqual(discdb.fingerprint(str(root)),
                                 {"content_hash": expected, "layout": layout, "files": len(files)})

    def test_lowercase_disc_directories_and_missing_or_unreadable_disc(self):
        dvd = self.root / "video_ts"
        dvd.mkdir()
        (dvd / "VIDEO_TS.IFO").write_bytes(b"abc")
        self.assertEqual(discdb.fingerprint(str(self.root))["layout"], "dvd")
        self.assertEqual(app.disc_kind(str(self.root)), "dvd")
        self.assertIsNone(discdb.fingerprint(str(self.root / "missing")))
        with patch.object(Path, "iterdir", side_effect=PermissionError()):
            self.assertIsNone(discdb.fingerprint(str(self.root)))

    def test_uhd_index_and_windows_drive_roots(self):
        bdmv = self.root / "bdmv"
        bdmv.mkdir()
        (bdmv / "INDEX.BDMV").write_bytes(b"INDX0300")
        self.assertEqual(app.disc_kind(str(self.root)), "4k")
        self.assertEqual(discdb.mounted_root("D:"), "D:\\")

    def test_linux_prerip_identification_uses_mounted_hash(self):
        dvd = self.root / "VIDEO_TS"
        dvd.mkdir()
        (dvd / "VIDEO_TS.IFO").write_bytes(b"example")
        fingerprint = discdb.fingerprint(str(self.root))
        match = {"title": "Example", "year": "2020", "type": "movie", "externalids": {"tmdb": "123"},
                 "releases": [{"discs": [{"contentHash": fingerprint["content_hash"], "titles": []}]}]}
        with patch.object(discdb, "mounted_root", return_value=str(self.root)) as root, \
                patch.object(discdb, "_query", return_value=[match]) as query:
            identified = app.identify_disc("/dev/sr0", "EXAMPLE", {"use_thediscdb": True})
        root.assert_called_once_with("/dev/sr0")
        self.assertEqual(identified["title"], "Example")
        self.assertEqual(query.call_args.args[1], {"h": fingerprint["content_hash"]})


@unittest.skipUnless(sys.platform.startswith("linux"), "Native Linux scanner invocation")
class Scanner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mount = self.root / "disc with spaces ü"
        self.mount.mkdir()
        (self.mount / "VIDEO_TS").mkdir()
        (self.mount / "VIDEO_TS/VIDEO_TS.IFO").write_bytes(b"example")
        self.out = self.root / "rip"
        self.out.mkdir()
        self.device = "/dev/sr0"
        rip_all.STATUS[self.device] = {}
        self.addCleanup(lambda: rip_all.STATUS.pop(self.device, None))

    def executable(self, body):
        script = self.root / "scanner with spaces"
        script.write_text(f"#!{sys.executable}\nimport sys,json,time\nfrom pathlib import Path\n" + body)
        script.chmod(0o755)
        return str(script)

    def test_scanner_receives_mounted_root_and_publishes_manifest_with_metadata(self):
        scanner = self.executable(
            "assert sys.argv[2]=='--output' and sys.argv[4:]==['--force','--quiet']\n"
            "assert (Path(sys.argv[1])/'VIDEO_TS').is_dir()\n"
            "Path(sys.argv[3]).write_text(json.dumps({'schemaVersion':1,'disc':{'root':sys.argv[1]}}))\n")
        disc = {}
        with patch.object(discdb, "mounted_root", return_value=str(self.mount)):
            rip_all.record_disc_metadata({"thediscdb_scan_path": scanner}, self.device, disc, self.out)
        self.assertEqual(disc["disc_layout"], "dvd")
        self.assertEqual(disc["content_hash"], discdb.fingerprint(str(self.mount))["content_hash"])
        self.assertEqual(json.loads((self.out / "disc.odm.json").read_text())["disc"]["root"], str(self.mount))
        self.assertNotIn(self.device, rip_all.PROCS)

    def test_rip_pipeline_persists_the_mounted_disc_fingerprint(self):
        cfg = {**rip_all.DEFAULT_CONFIG, "output_root": str(self.out), "retry_failed_titles": False,
               "skip_duplicate_features": False, "eject_when_done": False}
        with patch.object(discdb, "mounted_root", return_value=str(self.mount)), \
                patch.object(rip_all, "run_makemkv", return_value=(0, "")), \
                patch.object(rip_all, "disc_scanner", return_value=None):
            info = rip_all.rip_disc(cfg, {"device": self.device, "disc_label": "TEST", "drive_name": "Test DVD"})
        saved = next(self.out.glob("*/disc_info.json"))
        expected = discdb.fingerprint(str(self.mount))["content_hash"]
        self.assertEqual(info["content_hash"], expected)
        self.assertEqual(json.loads(saved.read_text())["content_hash"], expected)

    def test_failure_invalid_output_and_timeout_do_not_publish_partial_manifests(self):
        for body in ("Path(sys.argv[3]).write_text('{}'); sys.exit(2)",
                     "Path(sys.argv[3]).write_text('invalid')", "pass", "time.sleep(30)"):
            with self.subTest(body=body), patch.object(rip_all, "DISC_SCAN_TIMEOUT", 0.3), \
                    patch.object(rip_all, "log") as log:
                rip_all.scan_manifest(self.executable(body), str(self.mount), self.out, self.device)
                self.assertTrue(log.called)
                self.assertEqual(list(self.out.iterdir()), [])
                self.assertNotIn(self.device, rip_all.PROCS)

    def test_cancel_stops_real_scanner_and_leaves_no_partial_upload(self):
        scanner = self.executable("Path(sys.argv[3]).write_text('{}'); time.sleep(30)")
        worker = threading.Thread(target=rip_all.scan_manifest,
                                  args=(scanner, str(self.mount), self.out, self.device))
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not list(self.out.glob(".disc-scan-*/disc.odm.json")) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(list(self.out.glob(".disc-scan-*/disc.odm.json")), "scanner never started")
            self.assertTrue(rip_all.cancel(self.device))
            worker.join(timeout=8)
            self.assertFalse(worker.is_alive())
            self.assertEqual(list(self.out.iterdir()), [])
            self.assertNotIn(self.device, rip_all.PROCS)
        finally:
            proc = rip_all.PROCS.get(self.device)
            if proc:
                proc.terminate()
            worker.join(timeout=8)

    def test_unmounted_disc_skips_scanner_and_reports_reason(self):
        with patch.object(discdb, "mounted_root", return_value=None), \
                patch.object(rip_all, "scan_manifest") as scan, patch.object(rip_all, "log") as log:
            rip_all.record_disc_metadata({}, self.device, {}, self.out)
        scan.assert_not_called()
        self.assertIn("no accessible mounted disc", log.call_args.args[0])

    def test_scanner_discovery_and_explicit_invalid_path(self):
        scanner = self.executable("pass")
        self.assertEqual(rip_all.disc_scanner({"thediscdb_scan_path": scanner}), scanner)
        self.assertIsNone(rip_all.disc_scanner({"thediscdb_scan_path": str(self.root / 'missing')}))
        with patch.object(rip_all.shutil, "which", side_effect=[None, "/usr/bin/thediscdb-scan"]) as which:
            self.assertEqual(rip_all.disc_scanner({}), "/usr/bin/thediscdb-scan")
        self.assertEqual(which.call_args.args, ("thediscdb-scan",))


if __name__ == "__main__":
    unittest.main()
