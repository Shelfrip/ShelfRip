"""Linux integrations: fake sysfs/udev devices, kernel ABI and desktop processes.

No real disc is read or ejected by these tests. See docs/linux.md for hardware checks.
"""
from __future__ import annotations

import os
import json
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discs

TMP = discs.setup_env({})
import desktop
import health
import linux_support as linux
import music
import paths
import rip_all


def result(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class Discovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = patch.object(linux, "SYS_BLOCK", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def device(self, name, kind="5"):
        p = self.root / name / "device"
        p.mkdir(parents=True)
        (p / "type").write_text(kind)

    def test_no_drive_does_not_launch_any_process(self):
        self.device("sda", "0")
        with patch.object(linux.subprocess, "run") as run:
            self.assertEqual(linux.scan_drives(), [])
            run.assert_not_called()

    def test_loaded_empty_and_unplugged_drives(self):
        self.device("sr0")
        self.device("sr2")
        self.device("sda", "0")
        with patch.object(linux.shutil, "which", return_value="/bin/udevadm"), \
                patch.object(linux.subprocess, "run", side_effect=[
                    result("ID_CDROM_MEDIA=1\nID_MODEL=USB_DVD\nID_FS_LABEL_ENC=Am\\xc3\\xa9lie\\x20DVD\n"),
                    result("ID_CDROM=1\nID_MODEL=Empty_drive\n")]) as run:
            drives = linux.scan_drives()
        self.assertEqual([d["device"] for d in drives], ["/dev/sr0", "/dev/sr2"])
        self.assertEqual(drives[0]["disc_label"], "Amélie DVD")
        self.assertTrue(drives[0]["loaded"])
        self.assertFalse(drives[1]["loaded"])
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(c.args[0][1:3] == ["info", "--query=property"] for c in run.call_args_list))

    def test_audio_cd_is_detected_without_opening_device(self):
        self.device("sr0")
        with patch.object(linux.shutil, "which", return_value="udevadm"), \
                patch.object(linux.subprocess, "run", return_value=result(
                    "ID_CDROM_MEDIA=1\nID_CDROM_MEDIA_TRACK_COUNT_AUDIO=12\n")), \
                patch.object(linux.os, "open") as opened:
            self.assertTrue(linux.scan_drives()[0]["audio"])
            opened.assert_not_called()

    def test_missing_udev_requests_fallback(self):
        self.device("sr0")
        with patch.object(linux.shutil, "which", return_value=None):
            self.assertIsNone(linux.scan_drives())

    def test_udev_failure_is_not_reported_as_no_disc(self):
        self.device("sr0")
        with patch.object(linux.shutil, "which", return_value="udevadm"), \
                patch.object(linux.subprocess, "run", return_value=result(returncode=1, stderr="permission denied")):
            with self.assertRaisesRegex(OSError, "sr0.*permission denied"):
                linux.scan_drives()

    def test_device_permissions_and_missing_sg_driver(self):
        self.device("sr0")
        with patch.object(linux.os, "access", return_value=False):
            self.assertIn("No read/write access to /dev/sr0", linux.access_problem("/dev/sr0"))
        with patch.object(linux.os, "access", return_value=True):
            self.assertIn("Enable the Linux sg driver", linux.access_problem("/dev/sr0"))
        (self.root / "sr0/device/scsi_generic/sg7").mkdir(parents=True)
        with patch.object(linux.os, "access", side_effect=lambda p, _: p == "/dev/sr0"):
            self.assertIn("No read/write access to /dev/sg7", linux.access_problem("/dev/sr0"))
        with patch.object(linux.os, "access", return_value=True):
            self.assertEqual(linux.access_problem("/dev/sr0"), "")


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux runtime integration")
class Routing(unittest.TestCase):
    def tearDown(self):
        rip_all._LINUX_AUDIO_DRIVES.clear()

    def test_device_identity_not_enumeration_index(self):
        self.assertEqual(rip_all.makemkv_source({"device": "/dev/sr2", "index": 0}), "dev:/dev/sr2")
        self.assertEqual(rip_all.makemkv_source({"device": "E:", "index": 0}), "dev:E:")
        self.assertEqual(rip_all.makemkv_source({"device": "", "index": 2}), "disc:2")

    def test_no_drives_does_not_fall_back_to_makemkv(self):
        with patch.object(linux, "scan_drives", return_value=[]), patch.object(rip_all, "run_makemkv") as run:
            self.assertEqual(rip_all.scan_drives({}), [])
            run.assert_not_called()

    def test_fallback_and_device_filter(self):
        output = '\n'.join(['DRV:0,2,999,1,"USB","DVD","/dev/sr0"',
                            'DRV:1,2,999,1,"USB","DVD2","/dev/sr2"'])
        with patch.object(linux, "scan_drives", return_value=None), \
                patch.object(rip_all, "run_makemkv", return_value=(0, output)):
            self.assertEqual([d["device"] for d in rip_all.scan_drives({"drives": ["/dev/sr2"]})], ["/dev/sr2"])

    def test_busy_audio_drive_is_not_read_again(self):
        drive = {"device": "/dev/sr0", "audio": True, "loaded": True, "disc_label": ""}
        rip_all._LINUX_AUDIO_DRIVES["/dev/sr0"] = {"disc_label": "Audio CD ABC", "toc": {"first": 1}}
        with patch.object(linux, "scan_drives", return_value=[drive]), \
                patch.dict(rip_all.PROCS, {"/dev/sr0": Mock()}), patch.object(music, "read_toc") as read:
            found = rip_all.scan_drives({})
        read.assert_not_called()
        self.assertEqual(found[0]["disc_label"], "Audio CD ABC")
        with patch.object(linux, "scan_drives", return_value=[]):
            rip_all.scan_drives({})
        self.assertEqual(rip_all._LINUX_AUDIO_DRIVES, {})

    def test_linux_offsets(self):
        cfg = {"cd_offsets": "/dev/sr0=6, /dev/sr2:-667"}
        self.assertEqual(music.drive_offset(cfg, "/dev/sr0"), 6)
        self.assertEqual(music.drive_offset(cfg, "/dev/sr2"), -667)
        self.assertEqual(music.drive_offset(cfg, "/dev/sr1"), 0)
        self.assertEqual(music.drive_offset({"cd_offsets": "12"}, "/dev/sr0"), 12)
        self.assertEqual(music.drive_offset({"cd_offsets": "D:6, E:667"}, "E:"), 667)

    def test_eject_dispatch(self):
        with patch.object(linux, "eject") as eject:
            rip_all.eject("/dev/sr2")
            eject.assert_called_once_with("/dev/sr2")

    def test_blocked_drive_never_starts_rip(self):
        drive = {"device": "/dev/sr0", "loaded": True, "disc_label": "DVD", "access_error": "Missing sg"}
        with patch.object(rip_all, "scan_drives", return_value=[drive]), patch.object(rip_all, "acquire_lock") as lock:
            self.assertEqual(rip_all.rip_loaded({}, set(), {}), [])
        lock.assert_not_called()

    def test_config_is_private(self):
        paths.write_config({"opensubtitles_password": "test-only"})
        self.assertEqual(stat.S_IMODE(paths.CONFIG_PATH.stat().st_mode), 0o600)


class Ejection(unittest.TestCase):
    def test_rejects_regular_file(self):
        with self.assertRaisesRegex(ValueError, "not an optical drive"):
            linux.optical_device(__file__)

    @unittest.skipUnless(hasattr(os, "makedev"), "POSIX device identifiers")
    def test_rejects_non_optical_block_device(self):
        with patch.object(Path, "resolve", return_value=Path("/dev/sda")), \
                patch.object(Path, "stat", return_value=Mock(st_mode=stat.S_IFBLK, st_rdev=os.makedev(8, 0))), \
                patch.object(Path, "read_text", return_value="0\n"):
            with self.assertRaisesRegex(ValueError, "not an optical drive"):
                linux.optical_device("/dev/sda")

    def test_error_is_propagated(self):
        with patch.object(linux, "optical_device", return_value="/dev/sr0"), \
                patch.object(linux.shutil, "which", return_value="/bin/eject"), \
                patch.object(linux.subprocess, "run", return_value=result(returncode=1, stderr="device busy")) as run:
            with self.assertRaisesRegex(OSError, "device busy"):
                linux.eject("/dev/cdrom")
            self.assertEqual(run.call_args.args[0], ["/bin/eject", "--", "/dev/sr0"])

    def test_missing_eject_has_actionable_error(self):
        with patch.object(linux, "optical_device", return_value="/dev/sr0"), \
                patch.object(linux.shutil, "which", return_value=None):
            with self.assertRaisesRegex(OSError, "Install eject"):
                linux.eject("/dev/sr0")


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux kernel ABI")
class AudioTOC(unittest.TestCase):
    def test_toc_matches_existing_musicbrainz_input(self):
        def ioctl(fd, request, buf, mutate):
            if request == 0x5305:
                buf[:] = bytes([1, 2])
            else:
                self.assertEqual((request, len(buf), buf[2]), (0x5306, 12, 2))
                buf[4:7] = {1: bytes([0, 2, 0]), 2: bytes([3, 2, 0]), 0xAA: bytes([6, 2, 0])}[buf[0]]
            return 0
        with patch.object(linux, "optical_device", return_value="/dev/sr0"), \
                patch.object(linux.os, "open", return_value=123) as opened, \
                patch.object(linux.os, "close") as closed, patch("fcntl.ioctl", side_effect=ioctl):
            toc = music.read_toc("/dev/sr0")
        self.assertEqual(toc, {"first": 1, "last": 2, "leadout": 27150, "offsets": [150, 13650]})
        opened.assert_called_once_with("/dev/sr0", os.O_RDONLY | os.O_NONBLOCK)
        closed.assert_called_once_with(123)

    def test_ioctl_error_closes_device(self):
        with patch.object(linux, "optical_device", return_value="/dev/sr0"), \
                patch.object(linux.os, "open", return_value=123), \
                patch.object(linux.os, "close") as closed, patch("fcntl.ioctl", side_effect=OSError("disc removed")):
            with self.assertRaisesRegex(OSError, "disc removed"):
                linux.read_toc("/dev/sr0")
        closed.assert_called_once_with(123)

    def test_permission_error_explains_device_access(self):
        with patch.object(linux, "optical_device", return_value="/dev/sr0"), \
                patch.object(linux.os, "open", side_effect=PermissionError()):
            with self.assertRaisesRegex(PermissionError, "Grant your user access"):
                linux.read_toc("/dev/sr0")

    def test_enhanced_cd_data_track_is_excluded(self):
        def ioctl(fd, request, buf, mutate):
            if request == 0x5305:
                buf[:] = bytes([1, 2])
            else:
                if buf[0] == 2:
                    buf[1] = 0x40 if sys.byteorder == "little" else 4
                buf[4:7] = {1: bytes([0, 2, 0]), 2: bytes([5, 2, 0]), 0xAA: bytes([6, 2, 0])}[buf[0]]
            return 0
        with patch.object(linux, "optical_device", return_value="/dev/sr0"), \
                patch.object(linux.os, "open", return_value=123), patch.object(linux.os, "close"), \
                patch("fcntl.ioctl", side_effect=ioctl):
            self.assertEqual(music.read_toc("/dev/sr0"),
                             {"first": 1, "last": 1, "leadout": 11250, "offsets": [150]})


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux desktop")
class Desktop(unittest.TestCase):
    def test_open_passes_file_as_one_absolute_argument(self):
        with patch.object(desktop.shutil, "which", return_value="/bin/xdg-open"), \
                patch.object(desktop.subprocess, "Popen") as run:
            path = TMP / "a file; $(echo unsafe).mkv"
            desktop.open_file(path)
        self.assertEqual(run.call_args.args[0], ["/bin/xdg-open", str(path)])
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_missing_opener_is_not_silent_success(self):
        with patch.object(desktop.shutil, "which", return_value=None):
            with self.assertRaisesRegex(OSError, "xdg-utils"):
                desktop.open_file(TMP)

    def test_picker_selected_cancelled_and_failed(self):
        with patch.object(desktop.shutil, "which", side_effect=lambda n: "/bin/zenity" if n == "zenity" else None), \
                patch.object(desktop.subprocess, "run", side_effect=[result("/tmp/My DVDs\n"),
                    result(returncode=1), result(returncode=2, stderr="no display")]) as run:
            self.assertEqual(desktop.pick("folder"), ("/tmp/My DVDs", ""))
            self.assertIn("--directory", run.call_args.args[0])
            self.assertEqual(desktop.pick("file"), ("", ""))
            self.assertEqual(desktop.pick("folder"), (None, "no display"))

    def test_kde_picker(self):
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}), \
                patch.object(desktop.shutil, "which", side_effect=lambda n: "/bin/" + n), \
                patch.object(desktop.subprocess, "run", return_value=result("/tmp/rips\n")) as run:
            self.assertEqual(desktop.pick("folder"), ("/tmp/rips", ""))
        self.assertEqual(run.call_args.args[0][0], "/bin/kdialog")
        self.assertIn("--getexistingdirectory", run.call_args.args[0])

    def test_missing_picker_allows_manual_path(self):
        with patch.object(desktop.shutil, "which", return_value=None):
            selected, error = desktop.pick()
        self.assertIsNone(selected)
        self.assertIn("type the path", error)

    def test_notifications_are_optional(self):
        with patch.object(desktop.shutil, "which", return_value=None):
            self.assertFalse(desktop.notify("Done", "Disc filed"))
        with patch.object(desktop.shutil, "which", return_value="/bin/notify-send"), \
                patch.object(desktop.subprocess, "run", return_value=result()) as run:
            self.assertTrue(desktop.notify("--a-title", "Disc filed"))
        self.assertEqual(run.call_args.args[0][-3:], ["--", "--a-title", "Disc filed"])

    def test_browser_fallback_and_app_window(self):
        import linux_window
        with patch.object(desktop, "_find_browser", return_value=None), \
                patch.object(linux_window, "open_window", return_value=False), \
                patch.object(desktop.webbrowser, "open") as browser:
            desktop.open_window("http://127.0.0.1:8765/")
        browser.assert_called_once()
        with patch.object(desktop, "_find_browser", return_value="/bin/chromium"), \
                patch.object(linux_window, "open_window", return_value=False), \
                patch.object(desktop.subprocess, "Popen") as proc:
            desktop.open_window("http://127.0.0.1:8765/")
        self.assertIn("--app=http://127.0.0.1:8765/", proc.call_args.args[0])


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux processes")
class Processes(unittest.TestCase):
    def tearDown(self):
        health.LEFTOVER.clear()

    def test_only_stop_live_makemkv_processes(self):
        health.LEFTOVER[:] = [123, 456]
        with patch.object(health, "_makemkv_pids", return_value=[123]), patch.object(health.os, "kill") as kill:
            self.assertEqual(health.stop_leftovers(), 1)
        kill.assert_called_once_with(123, signal.SIGTERM)

    def test_process_identity_and_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "makemkvcon"
            exe.touch()
            for pid in ("123", "456"):
                (root / pid).mkdir()
                (root / pid / "exe").symlink_to(exe if pid == "123" else sys.executable)
            with patch.object(health, "PROC", root):
                self.assertEqual(health._makemkv_pids(), [123])
                with patch.object(health.os, "getuid", return_value=os.getuid() + 1):
                    self.assertEqual(health._makemkv_pids(), [])


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux application smoke test")
class Application(unittest.TestCase):
    def test_desktop_actions_reject_cross_origin_requests(self):
        import app
        for route in ("/api/open", "/api/linux/login"):
            handler = object.__new__(app.Handler)
            handler.path, handler.headers = route, {"Origin": "https://example.com"}
            handler.body = Mock(side_effect=AssertionError("must reject before reading body"))
            handler.send = Mock()
            handler.do_POST()
            handler.send.assert_called_once_with(403, {"error": "forbidden"})

    def test_update_does_not_interrupt_an_import(self):
        import app
        import importer
        import linux_update
        with patch.object(app, "RIPPER", None), patch.object(app, "UPDATE", {}), \
                patch.dict(importer.JOBS, {"source": {"state": "running"}}, clear=True), \
                patch.object(linux_update, "install") as install, patch.object(app, "restart_app") as restart:
            app.install_update()
            self.assertEqual(app.UPDATE["step"], "error")
            self.assertIn("Work started", app.UPDATE["error"])
            self.assertFalse(app.review.UPDATING)
        install.assert_not_called()
        restart.assert_not_called()

    def test_busy_tray_quit_opens_page_for_confirmation(self):
        import app
        tray = Mock()
        tray.start.return_value = False  # inspect callbacks without starting a polling thread
        with patch.object(app.desktop, "create_tray", return_value=tray) as create, \
                patch.object(app, "tray_status", return_value="Ripping"), \
                patch.object(app.desktop, "open_window") as opened, \
                patch.object(app.desktop, "notify") as notify, \
                patch.object(app, "RIPPER", Mock(busy={"/dev/sr0"})), \
                patch.object(app, "TRAY"), patch.object(app, "quit_app") as quit_app:
            app.start_tray("http://127.0.0.1:8765/", True)
            menu = create.call_args.args[2]()
            next(item[1] for item in menu if item and item[0] == "Quit Shelfrip")()
        quit_app.assert_not_called()
        opened.assert_called_once_with("http://127.0.0.1:8765/", True)
        notify.assert_called_once()

    def test_restart_and_quit_require_confirmation_when_busy(self):
        import app
        for route in ("/api/restart", "/api/quit"):
            handler = object.__new__(app.Handler)
            handler.path = route
            handler.headers = {}
            handler.body = lambda: {}
            handler.send = Mock()
            with patch.object(app, "RIPPER", Mock(busy={"/dev/sr0"})), \
                    patch.object(app.threading, "Timer") as timer:
                handler.do_POST()
            handler.send.assert_called_once_with(200, {"busy": ["/dev/sr0"]})
            timer.assert_not_called()

    def test_real_app_serves_page_settings_and_drive_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "config.json").write_text(json.dumps({
                "check_updates": False, "shared_tmdb": False, "share_discs": False,
                "use_community": False, "tray_icon": False, "auto_rip": False,
                "output_root": "", "plex_library_root": str(data / "library"),
            }))
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            env = {**os.environ, "SHELFRIP_DATA": str(data), "PYTHONUNBUFFERED": "1"}
            with open(data / "app.log", "w") as log:
                proc = subprocess.Popen([sys.executable, "-c",
                    f"import app; app.review.PORT = {port}; app.main()", "--background"],
                    cwd=Path(__file__).resolve().parent.parent, env=env, stdout=log, stderr=log)
                try:
                    base = f"http://127.0.0.1:{port}"
                    deadline = time.monotonic() + 15
                    while True:
                        try:
                            with urllib.request.urlopen(base + "/api/version", timeout=1) as response:
                                version = json.load(response)
                            break
                        except OSError:
                            if proc.poll() is not None or time.monotonic() > deadline:
                                self.fail((data / "app.log").read_text())
                            time.sleep(0.05)
                    self.assertEqual(version["app"], "Shelfrip")
                    with urllib.request.urlopen(base + "/", timeout=5) as response:
                        self.assertIn(b"<html", response.read().lower())
                    with urllib.request.urlopen(base + "/api/settings", timeout=10) as response:
                        settings = json.load(response)
                    self.assertEqual(settings["platform"], "linux")
                    self.assertEqual(settings["path"], str(data / "config.json"))
                    with urllib.request.urlopen(base + "/api/drives", timeout=5) as response:
                        drives = json.load(response)
                    self.assertIn("drives", drives)
                    old_boot = drives["boot"]
                    request = urllib.request.Request(base + "/api/restart", data=b"{}",
                                                     headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertTrue(json.load(response)["ok"])
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        try:
                            with urllib.request.urlopen(base + "/api/drives", timeout=1) as response:
                                if json.load(response)["boot"] != old_boot:
                                    break
                        except OSError:
                            pass
                        time.sleep(0.05)
                    else:
                        self.fail("restart did not produce a new boot on the same port")
                    self.assertIsNone(proc.poll(), "Linux restart should replace the existing process")
                    with urllib.request.urlopen(base + "/api/settings", timeout=5) as response:
                        self.assertEqual(json.load(response)["path"], str(data / "config.json"))
                    request = urllib.request.Request(base + "/api/quit", data=b"{}",
                                                     headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertTrue(json.load(response)["ok"])
                    self.assertEqual(proc.wait(timeout=8), 0)
                finally:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)


@unittest.skipUnless(shutil.which("node"), "Node is needed to check the interface")
class Interface(unittest.TestCase):
    def test_interface_javascript_parses(self):
        html = (Path(__file__).resolve().parent.parent / "review.html").read_text(encoding="utf-8")
        for script in re.findall(r"<script[^>]*>(.*?)</script>", html, re.S):
            checked = subprocess.run(["node", "--check"], input=script, text=True, encoding="utf-8",
                                     capture_output=True, timeout=20)
            self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_password_help_matches_server_platform(self):
        html = (Path(__file__).resolve().parent.parent / "review.html").read_text(encoding="utf-8")
        renderer = html[html.index("function setField("):html.index("const FMTS =")]
        script = '''
const assert = require('node:assert/strict');
const SETS = {platform: 'linux'}, SET_STATUS = {}, esc = x => String(x ?? '');
''' + renderer + '''
const meta = ['opensubtitles_password', 'Password', 'Encrypted for your Windows account.'];
const linux = setField({value: false, kind: 'secret'}, meta);
assert.ok(linux.includes('Secret Service keyring'));
assert.ok(linux.includes('Install secret-tool'));
assert.ok(!linux.includes('Encrypted for your Windows account'));
SETS.password_storage = {helper_available: true};
const saved = setField({value: true, kind: 'secret'}, meta);
assert.ok(saved.includes('Remove saved password'));
SETS.desktop = {tray_active: true};
assert.ok(setField({value: true, kind: 'bool'}, ['tray_icon', '', '']).includes('Tray connected'));
SETS.platform = 'win32';
const windows = setField({value: false, kind: 'secret'}, meta);
assert.ok(windows.includes('Encrypted for your Windows account'));
assert.ok(!windows.includes('Secret Service keyring'));
'''
        checked = subprocess.run(["node"], input=script, text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(checked.returncode, 0, checked.stderr)


if __name__ == "__main__":
    unittest.main()
