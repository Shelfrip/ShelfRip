"""Real detached application lifecycle, with no drives, user data or desktop."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import background
import linux_tray

ROOT = Path(__file__).resolve().parent.parent


class TrayFallback(unittest.TestCase):
    def test_missing_native_libraries_report_a_usable_error(self):
        import contextlib
        import io
        output = io.StringIO()
        with patch.object(linux_tray.ctypes.util, "find_library", return_value=None), \
                contextlib.redirect_stdout(output):
            self.assertEqual(linux_tray._helper("icon.png"), 1)
        event = json.loads(output.getvalue())
        self.assertEqual(event["event"], "error")
        self.assertIn("Install the desktop tray libraries", event["message"])

    def test_headless_start_is_fast_and_never_spawns(self):
        tray = linux_tray.LinuxTray("Shelfrip", None, lambda: [], "icon.png")
        with patch.dict(os.environ, {"DISPLAY": "", "WAYLAND_DISPLAY": ""}), \
                patch.object(linux_tray.subprocess, "Popen") as spawn:
            self.assertFalse(tray.start())
            self.assertIn("No graphical session", tray.error)
        spawn.assert_not_called()

    def test_missing_helper_is_nonfatal(self):
        tray = linux_tray.LinuxTray("Shelfrip", None, lambda: [], "icon.png")
        with patch.dict(os.environ, {"DISPLAY": ":99"}), \
                patch.object(linux_tray.subprocess, "Popen", side_effect=OSError()):
            self.assertFalse(tray.start())
        self.assertIn("could not start", tray.error)

    def test_old_or_disabled_menu_actions_cannot_run(self):
        action = Mock()
        tray = linux_tray.LinuxTray("Shelfrip", None, lambda: [("Open", action), ("Status", None)], "icon.png")
        tray.proc = Mock()
        tray.proc.poll.return_value = None
        tray.set_tip("first")
        old = next(iter(tray._actions))
        tray.set_tip("second")
        # Use a file-like stream so reader EOF handling is exercised as well.
        import io
        tray.proc.stdout = io.StringIO("\n".join([json.dumps({"event": "activate", "id": old}),
                                                  json.dumps({"event": "activate", "id": "2:1"})]))
        tray._read()
        action.assert_not_called()
        self.assertFalse(tray.ok)


@unittest.skipUnless(sys.platform.startswith("linux") or sys.platform == "darwin", "POSIX detached lifecycle")
class Background(unittest.TestCase):
    def test_failed_start_is_reported_and_log_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "app.log"
            with self.assertRaisesRegex(OSError, "could not start"):
                background.launch([sys.executable, "-c", "raise SystemExit(3)"], log)
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_detach_survives_launcher_exit_reuses_instance_restarts_and_quits(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "config.json").write_text(json.dumps({
                "check_updates": False, "shared_tmdb": False, "share_discs": False,
                "use_community": False, "tray_icon": True, "auto_rip": False,
                "output_root": "", "plex_library_root": str(data / "library"),
            }))
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            env = {**os.environ, "SHELFRIP_DATA": tmp, "DISPLAY": "", "WAYLAND_DISPLAY": "",
                   "PYTHONUNBUFFERED": "1"}
            launcher = [sys.executable, str(ROOT / "app.py"), "--detach", f"--port={port}"]
            base = f"http://127.0.0.1:{port}"

            def request(route, post=False):
                req = urllib.request.Request(base + route, data=b"{}" if post else None,
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as response:
                    return json.load(response)

            try:
                started = subprocess.run(launcher, env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(started.returncode, 0, started.stderr)
                self.assertIn("started in the background", started.stdout)
                settings = request("/api/settings")
                self.assertTrue(settings["desktop"]["background"])
                self.assertFalse(settings["desktop"]["tray_active"])
                self.assertIn("not implemented" if sys.platform == "darwin" else "No graphical session",
                              settings["desktop"]["tray_error"])
                boot = request("/api/drives")["boot"]
                again = subprocess.run(launcher, env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(again.returncode, 0, again.stderr)
                self.assertIn("already running", again.stdout)
                self.assertEqual(request("/api/drives")["boot"], boot)
                self.assertTrue(request("/api/restart", True)["ok"])
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        if request("/api/drives")["boot"] != boot:
                            break
                    except OSError:
                        pass
                    time.sleep(0.05)
                else:
                    self.fail("Detached instance did not restart")
                self.assertTrue(request("/api/settings")["desktop"]["background"])
                self.assertTrue(request("/api/quit", True)["ok"])
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        request("/api/version")
                    except OSError:
                        break
                    time.sleep(0.05)
                else:
                    self.fail("Detached instance did not quit")
                self.assertEqual((data / "shelfrip.log").stat().st_mode & 0o777, 0o600)
            finally:
                try:
                    request("/api/quit", True)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
