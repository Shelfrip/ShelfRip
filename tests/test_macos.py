"""macOS platform contracts; native checks use an isolated Keychain and no media writes."""
import contextlib
import ctypes as C
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = tempfile.TemporaryDirectory(prefix="shelfrip-macos-tests-")
os.environ["SHELFRIP_DATA"] = DATA.name
import app
import desktop
import discdb
import macos_desktop
import macos_keyring as keyring
import macos_support as mac
import music
import paths
import refsubs
import rip_all

INFO = {"DRDeviceIORegistryEntryPathKey": "IOService:/USB/DVD@1", "DRDeviceVendorNameKey": "Test",
        "DRDeviceProductNameKey": "Optical drive"}
STATUS = {"DRDeviceMediaStateKey": "DRDeviceMediaStateMediaPresent",
          "DRDeviceMediaInfoKey": {"DRDeviceMediaBSDNameKey": "disk8", "DRDeviceMediaTypeKey": "DRDeviceMediaTypeDVDROM"}}
DEVICE = mac.device_id(INFO)
OLD, NEW = "keychain:" + "a" * 32, "keychain:" + "b" * 32


def snapshot(status=STATUS, info=INFO, dr=None):
    return contextlib.nullcontext((dr, [(123, info, status)]))


def toc_fixture():
    def entry(point, msf, session=1, control=0):
        return bytes([session, 0x10 | control, 0, point, 0, 0, 0, 0, *msf])
    entries = (entry(0xA0, (1, 0, 0)) + entry(0xA1, (2, 0, 0)) + entry(0xA2, (10, 0, 0))
               + entry(1, (0, 2, 0)) + entry(2, (5, 0, 0))
               + entry(0xA2, (15, 0, 0), 2) + entry(3, (12, 32, 0), 2, 4))
    return (len(entries) + 2).to_bytes(2, "big") + bytes([1, 2]) + entries


class Optical(unittest.TestCase):
    def test_discovery_empty_and_filter_do_not_scan_make_mkv(self):
        other = mac.device_id({**INFO, "DRDeviceIORegistryEntryPathKey": "IOService:/USB/DVD@2"})
        self.assertNotEqual(other, DEVICE)
        with patch.object(sys, "platform", "darwin"), patch.object(mac, "scan_drives", return_value=[]) as scan, \
                patch.object(rip_all, "run_makemkv") as run:
            self.assertEqual(rip_all.scan_drives({}), [])
            run.assert_not_called()
            scan.return_value = [{"device": DEVICE}, {"device": other}]
            self.assertEqual(rip_all.scan_drives({"drives": [other]}), [{"device": other}])

    def test_stable_identity_across_eject_and_bsd_changes(self):
        empty = {"DRDeviceMediaStateKey": "DRDeviceMediaStateNone"}
        with patch.object(mac, "devices", side_effect=lambda: snapshot(empty)), patch.object(mac, "_plist") as command:
            drive = mac.scan_drives()[0]
            self.assertEqual(drive["device"], DEVICE)
            self.assertFalse(drive["loaded"])
            command.assert_not_called()
        changed = {**STATUS, "DRDeviceMediaInfoKey": {"DRDeviceMediaBSDNameKey": "disk19"}}
        with patch.object(mac, "devices", side_effect=lambda: snapshot(changed)), \
                patch.object(mac, "_volumes", return_value=[{"VolumeName": "A film ü"}]):
            drive = mac.scan_drives()[0]
            self.assertEqual((drive["device"], drive["disc_label"]), (DEVICE, "A film ü"))
            self.assertEqual(mac.media_device(DEVICE), "/dev/disk19")

    def test_make_mkv_uses_fresh_device_not_enumeration_index(self):
        with patch.object(sys, "platform", "darwin"), patch.object(mac, "devices", side_effect=snapshot):
            self.assertEqual(rip_all.makemkv_source({"index": 0, "device": DEVICE}), "dev:/dev/disk8")
            self.assertEqual(mac.media_device("/dev/rdisk8"), "/dev/disk8")
            for bad in ("/dev/disk0", "/dev/disk8s1", "disk8", "/dev/disk8;touch x", "macos:missing"):
                with self.assertRaises(ValueError):
                    mac.media_device(bad)

    def test_eject_only_a_current_optical_device(self):
        fn = Mock(return_value=0)
        with patch.object(mac, "devices", side_effect=snapshot), patch.object(mac, "bind", return_value=fn):
            with self.assertRaises(ValueError):
                mac.eject("/dev/disk0")
            fn.assert_not_called()
            mac.eject(DEVICE)
            fn.assert_called_once_with(123)
            fn.return_value = -47
            with self.assertRaisesRegex(OSError, "busy"):
                mac.eject(DEVICE)

    def test_unmounted_and_blank_media(self):
        with patch.object(mac, "devices", side_effect=snapshot), patch.object(mac, "_volumes", side_effect=OSError()):
            self.assertIsNone(mac.mounted_root(DEVICE))
            self.assertTrue(mac.scan_drives()[0]["loaded"])
        blank = {**STATUS, "DRDeviceMediaInfoKey": {**STATUS["DRDeviceMediaInfoKey"], "DRDeviceMediaIsBlankKey": True}}
        with patch.object(mac, "devices", side_effect=lambda: snapshot(blank)), patch.object(mac, "_volumes") as volumes:
            self.assertFalse(mac.scan_drives()[0]["loaded"])
            volumes.assert_not_called()

    def test_partitioned_disc_mount_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "A disc ü"
            (root / "VIDEO_TS").mkdir(parents=True)
            (root / "VIDEO_TS/VIDEO_TS.IFO").write_bytes(b"test")
            def command(args):
                if args[1] == "list":
                    return {"AllDisksAndPartitions": [{"DeviceIdentifier": "disk8", "Partitions": [
                        {"DeviceIdentifier": "disk8s1"}, {"DeviceIdentifier": "disk0s1"}]}]}
                self.assertNotEqual(args[-1], "/dev/disk0s1")
                return {"MountPoint": str(root)} if args[-1] == "/dev/disk8s1" else {}
            with patch.object(mac, "devices", side_effect=snapshot), patch.object(mac, "_plist", side_effect=command), \
                    patch.object(sys, "platform", "darwin"):
                self.assertEqual(discdb.mounted_root(DEVICE), str(root))
                self.assertEqual(discdb.fingerprint(discdb.mounted_root(DEVICE))["layout"], "dvd")

    def test_multisession_audio_toc_and_musicbrainz_identity(self):
        raw = toc_fixture()
        with patch.object(mac, "devices", side_effect=snapshot), patch.object(mac, "_cd_tocs", return_value={"/dev/disk8": raw}), \
                patch.object(sys, "platform", "darwin"):
            toc = music.read_toc(DEVICE)
            self.assertEqual(toc, {"first": 1, "last": 2, "leadout": 45000, "offsets": [150, 22500]})
            drive = {"device": DEVICE, "audio": True}
            self.assertTrue(music.label_drive(drive))
            self.assertEqual(drive["disc_label"], "CD_" + music.disc_id(toc))
            self.assertEqual(music.read_cdtext(DEVICE), {})
        for bad in (b"", raw[:-1], b"\0\2\1\1", raw[:4] + raw[15:]):
            with self.assertRaises(ValueError):
                mac.parse_toc(bad)

    def test_audio_scan_uses_cached_toc(self):
        status = {**STATUS, "DRDeviceMediaInfoKey": {**STATUS["DRDeviceMediaInfoKey"], "DRDeviceMediaTypeKey": "DRDeviceMediaTypeCDROM"}}
        with patch.object(mac, "devices", side_effect=lambda: snapshot(status)), patch.object(mac, "_volumes", return_value=[]), \
                patch.object(mac, "_cd_tocs", return_value={"/dev/disk8": toc_fixture()}):
            self.assertTrue(mac.scan_drives()[0]["audio"])
        self.assertEqual(music.drive_offset({"cd_offsets": DEVICE + "=-6"}, DEVICE), -6)


class Desktop(unittest.TestCase):
    def test_picker_values_cannot_become_applescript(self):
        title = 'A "quoted" title; do shell script "touch BAD"'
        with patch.object(macos_desktop.subprocess, "run", return_value=Mock(returncode=0, stdout="/Volumes/A disc ü/\n")) as run:
            self.assertEqual(macos_desktop.pick_path("folder", "/tmp", title), ("/Volumes/A disc ü/", ""))
            args = run.call_args.args[0]
            self.assertEqual(args[:2], ["/usr/bin/osascript", "-"])
            self.assertIn(title, args)
            self.assertNotIn(title, run.call_args.kwargs["input"])
        with patch.object(macos_desktop, "_script", return_value=Mock(returncode=0, stdout="\n")):
            self.assertEqual(macos_desktop.pick_path("file", "", ""), ("", ""))
        self.assertIsNone(macos_desktop.pick_path("other", "", "")[0])

    def test_native_open_error_and_notifications(self):
        with patch.object(sys, "platform", "darwin"), patch.object(desktop.subprocess, "run", return_value=Mock(returncode=1)) as run:
            with self.assertRaises(OSError):
                desktop.open_file(Path("/tmp/a movie.mkv"))
            self.assertEqual(run.call_args.args[0], ["/usr/bin/open", str(Path("/tmp/a movie.mkv").resolve())])
        with patch.object(macos_desktop, "_script", side_effect=subprocess.TimeoutExpired("osascript", 10)):
            self.assertFalse(macos_desktop.notify("title", "text"))

    @unittest.skipIf(os.name == "nt", "POSIX application paths")
    def test_application_support_path_and_makemkv_bundle(self):
        with patch.object(sys, "platform", "darwin"), patch.object(paths.os, "name", "posix"), patch.object(paths.Path, "home", return_value=Path("/Users/test")):
            self.assertEqual(paths._installed_data_dir(), Path("/Users/test/Library/Application Support/Shelfrip"))
        with patch.object(sys, "platform", "darwin"), patch.object(Path, "is_file", lambda p: str(p) == "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon"):
            self.assertEqual(rip_all.find_makemkv({}), "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon")

    def test_macos_does_not_offer_a_windows_installer(self):
        release = {"tag_name": "v99.0.0", "html_url": "https://github.com/Shelfrip/ShelfRip/releases/tag/v99.0.0",
                   "assets": [{"name": "Shelfrip Setup 99.0.0.exe", "browser_download_url":
                       "https://github.com/Shelfrip/ShelfRip/releases/download/v99.0.0/setup.exe", "size": 42}]}
        import io
        with patch.object(sys, "platform", "darwin"), patch.object(app.os, "name", "posix"), \
                patch.object(app, "_read_config", return_value={}), patch.dict(app.UPDATE, {}, clear=True), \
                patch.object(app.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(release).encode())):
            app.check_update()
            self.assertEqual(app.UPDATE["latest"], "99.0.0")
            self.assertNotIn("installer", app.UPDATE)


class Passwords(unittest.TestCase):
    def setUp(self):
        self.platform = patch.object(sys, "platform", "darwin")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        paths.write_config({"opensubtitles_password": OLD})

    def test_verified_exact_password_storage_and_no_plaintext_fallback(self):
        password = " secret ü\n\0 "
        backend = Mock(side_effect=["", password])
        with patch.object(keyring, "_run", backend):
            self.assertTrue(keyring.is_reference(refsubs.protect(password)))
            self.assertEqual(backend.call_args_list[0].args[2], password)
        with patch.object(keyring, "_run", side_effect=keyring.KeyringError("locked")):
            with self.assertRaises(keyring.KeyringError):
                refsubs.protect(password)
        for invalid in ("plaintext", "keychain:invalid", "secret-service:" + "a" * 32):
            with self.assertRaises(keyring.KeyringError):
                refsubs.reveal(invalid)

    def test_replacement_commit_and_rollback(self):
        with patch.object(keyring, "store", return_value=NEW), patch.object(keyring, "cleanup") as cleanup:
            app._save_settings({"opensubtitles_password": "new password"})
            self.assertEqual(json.loads(paths.CONFIG_PATH.read_text())["opensubtitles_password"], NEW)
            cleanup.assert_called_once_with(OLD)
        paths.write_config({"opensubtitles_password": OLD})
        with patch.object(keyring, "store", return_value=NEW), patch.object(paths, "write_config", side_effect=OSError("test")), \
                patch.object(keyring, "cleanup") as cleanup:
            with self.assertRaises(ValueError):
                app._save_settings({"opensubtitles_password": "new password"})
            cleanup.assert_called_once_with(NEW)
            self.assertEqual(json.loads(paths.CONFIG_PATH.read_text())["opensubtitles_password"], OLD)

    def test_settings_poll_does_not_lookup_and_removal(self):
        with patch.object(keyring, "lookup") as lookup, patch.object(app, "setup_check", return_value=[]):
            view = app.settings_view()
            self.assertTrue(view["password_storage"]["helper_available"])
            lookup.assert_not_called()
        with patch.object(keyring, "cleanup") as cleanup:
            app._save_settings({"opensubtitles_password": None})
            cleanup.assert_called_once_with(OLD)
        self.assertEqual(json.loads(paths.CONFIG_PATH.read_text())["opensubtitles_password"], "")


@unittest.skipUnless(sys.platform == "darwin", "native macOS APIs")
class Native(unittest.TestCase):
    def test_real_discovery_and_process_identity(self):
        drives = mac.scan_drives()  # cached queries only: never rip, eject or mount
        self.assertEqual(len({d["device"] for d in drives}), len(drives))
        for drive in drives:
            self.assertRegex(drive["device"], r"^macos:[0-9a-f]{24}$")
        self.assertIn(os.getpid(), mac.process_ids({Path(sys.executable).resolve().name}))

    def test_real_private_keychain_round_trip_and_lock(self):
        from macos_native import CoreFoundation, bind, framework
        sec, cf = framework("Security"), CoreFoundation()
        search = C.c_void_p()
        self.assertEqual(bind(sec, "SecKeychainCopySearchList", C.c_int32, C.c_void_p)(C.byref(search)), 0)
        ref = C.c_void_p()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shelfrip-test.keychain"
            password = b"ephemeral-test-keychain"
            try:
                code = bind(sec, "SecKeychainCreate", C.c_int32, C.c_char_p, C.c_uint32, C.c_char_p,
                            C.c_ubyte, C.c_void_p, C.c_void_p)(os.fsencode(path), len(password), password, False, None, C.byref(ref))
                self.assertEqual(code, 0)
                # Restore the user's search list immediately; every test operation names its private keychain.
                self.assertEqual(bind(sec, "SecKeychainSetSearchList", C.c_int32, C.c_void_p)(search), 0)
                store = keyring.Keychain(path)
                secret = " exact ü\n\0 password "
                store.run("store", OLD, secret)
                self.assertEqual(store.run("lookup", OLD), secret)
                self.assertEqual(bind(sec, "SecKeychainLock", C.c_int32, C.c_void_p)(ref), 0)
                with self.assertRaises(keyring.KeyringError):
                    store.run("lookup", OLD)
                self.assertEqual(bind(sec, "SecKeychainUnlock", C.c_int32, C.c_void_p, C.c_uint32, C.c_char_p,
                                      C.c_ubyte)(ref, len(password), password, True), 0)
                store.run("clear", OLD)
                store.run("clear", OLD)  # idempotent cleanup
                with self.assertRaises(keyring.KeyringError):
                    store.run("lookup", OLD)
            finally:
                bind(sec, "SecKeychainSetSearchList", C.c_int32, C.c_void_p)(search)
                if ref:
                    bind(sec, "SecKeychainDelete", C.c_int32, C.c_void_p)(ref)
                    cf.release(ref)
                cf.release(search)


if __name__ == "__main__":
    unittest.main()
