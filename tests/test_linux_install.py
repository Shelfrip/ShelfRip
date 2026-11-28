"""Build/install/update lifecycle in temporary homes, without touching the user's install."""
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import build_linux
import linux_install as install
import linux_update


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux package lifecycle")
class Installation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build_dir = tempfile.TemporaryDirectory()
        cls.archive = build_linux.build(ROOT, Path(cls.build_dir.name))
        cls.version = cls.archive.name.removeprefix("Shelfrip-").removesuffix("-linux.tar.gz")

    @classmethod
    def tearDownClass(cls):
        cls.build_dir.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfrip install $% ")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = {"HOME": str(self.home), "XDG_DATA_HOME": str(self.home / "data"),
                    "XDG_CONFIG_HOME": str(self.home / "config"), "SHELFRIP_DATA": str(self.home / "settings")}
        self.patch = patch.dict(os.environ, self.env)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def installed(self):
        return install.install_archive(self.archive, self.version)

    def test_package_contains_only_program_files_and_installs_launcher(self):
        target = self.installed()
        self.assertTrue(install.installed(target))
        self.assertTrue((target / "review.html").is_file())
        self.assertFalse((target / "config.json").exists())
        self.assertFalse((target / ".git").exists())
        self.assertFalse(install.login_enabled())
        launcher = install.executable()
        self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
        entry = (install.data_home() / "applications" / install.DESKTOP_ID).read_text()
        self.assertIn("Type=Application", entry)
        self.assertIn("%%", entry)
        self.assertIn('\\\\$', entry)
        self.assertEqual(self.installed(), target, "same package install should be idempotent")

    def test_login_opt_in_disable_and_uninstall_keep_user_data(self):
        self.installed()
        settings = Path(self.env["SHELFRIP_DATA"])
        settings.mkdir()
        (settings / "config.json").write_text('{"keep":true}')
        install.set_login(True)
        self.assertTrue(install.login_enabled())
        self.assertIn("--background", (install.config_home() / "autostart" / install.DESKTOP_ID).read_text())
        install.set_login(False)
        self.assertFalse(install.login_enabled())
        install.set_login(True)
        install.uninstall()
        self.assertFalse(install.executable().exists())
        self.assertFalse(install.login_enabled())
        self.assertEqual((settings / "config.json").read_text(), '{"keep":true}')

    def test_wrong_version_and_damaged_package_keep_current_install(self):
        old = self.installed()
        with self.assertRaisesRegex(ValueError, "wrong version"):
            install.install_archive(self.archive, "999.0.0")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            with tarfile.open(self.archive) as tar:
                for member in tar.getmembers():
                    dst = source / member.name.removeprefix("shelfrip/")
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(tar.extractfile(member).read())
            (source / "app.py").write_text("tampered")
            with self.assertRaisesRegex(ValueError, "integrity"):
                install.install(source)
        self.assertEqual((install.install_root() / "current").resolve(), old)

    def test_archive_rejects_traversal_and_links(self):
        for name, kind in (("shelfrip/../../escaped", tarfile.REGTYPE),
                           ("/tmp/escaped", tarfile.REGTYPE), ("shelfrip/link", tarfile.SYMTYPE)):
            archive = self.home / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                member = tarfile.TarInfo(name)
                member.type, member.linkname = kind, "/tmp"
                tar.addfile(member, io.BytesIO(b""))
            with self.assertRaisesRegex(ValueError, "Unsafe"):
                install.install_archive(archive, self.version)

    def update_info(self):
        data = self.archive.read_bytes()
        return {"latest": self.version, "installer": f"https://github.com/Shelfrip/ShelfRip/releases/download/v{self.version}/{self.archive.name}",
                "digest": "sha256:" + hashlib.sha256(data).hexdigest(), "size": len(data)}

    def test_verified_download_installs_and_reports_progress(self):
        progress = []
        with patch.object(linux_update.urllib.request, "urlopen", return_value=io.BytesIO(self.archive.read_bytes())):
            entry = linux_update.install(self.update_info(), "Shelfrip/ShelfRip", progress.append)
        self.assertTrue(entry.is_file())
        self.assertEqual(progress[-1], self.archive.stat().st_size)

    def test_failed_or_busy_update_preserves_active_version(self):
        old = self.installed()
        for data, before in ((b"corrupt", lambda: None),
                             (self.archive.read_bytes(), lambda: (_ for _ in ()).throw(ValueError("busy")))):
            with patch.object(linux_update.urllib.request, "urlopen", return_value=io.BytesIO(data)):
                with self.assertRaises(ValueError):
                    linux_update.install(self.update_info(), "Shelfrip/ShelfRip", lambda _: None, before)
            self.assertEqual((install.install_root() / "current").resolve(), old)

    def test_real_update_restarts_installed_app_on_same_port(self):
        old = self.installed()
        source = self.home / "new-source"
        shutil.copytree(old, source)
        version_file = source / "paths.py"
        version_file.write_text(version_file.read_text().replace(f'APP_VERSION = "{self.version}"', 'APP_VERSION = "99.0.0"'))
        archive = build_linux.build(source, self.home / "release")
        update = {"latest": "99.0.0", "installer": "https://github.com/Shelfrip/ShelfRip/releases/download/v99.0.0/Shelfrip-99.0.0-linux.tar.gz",
                  "digest": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(), "size": archive.stat().st_size,
                  "can_install": True}
        settings = Path(self.env["SHELFRIP_DATA"])
        settings.mkdir()
        (settings / "config.json").write_text(json.dumps({"check_updates": False, "tray_icon": False,
            "auto_rip": False, "shared_tmdb": False, "share_discs": False, "use_community": False, "job_total_discs": 123}))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        code = ("import app,linux_update; original=linux_update.urllib.request.urlopen; "
                f"linux_update.urllib.request.urlopen=lambda req,**kw: open({str(archive)!r},'rb') if getattr(req,'full_url','')=={update['installer']!r} else original(req,**kw); "
                f"app.UPDATE.update({update!r}); app.main()")
        def request(path, body=None):
            req = urllib.request.Request(f"http://127.0.0.1:{port}" + path,
                data=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=2) as response:
                return json.load(response)
        with (self.home / "update.log").open("w") as log:
            proc = subprocess.Popen([sys.executable, "-c", code, "--background", f"--port={port}"], cwd=old, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        if request("/api/version")["current"] == self.version:
                            break
                    except OSError:
                        pass
                    time.sleep(0.05)
                else:
                    self.fail("installed app did not start")
                request("/api/update", {})
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    try:
                        if request("/api/version")["current"] == "99.0.0":
                            break
                    except OSError:
                        pass
                    time.sleep(0.05)
                else:
                    self.fail((self.home / "update.log").read_text())
                self.assertIsNone(proc.poll())
                self.assertTrue(old.is_dir(), "running-version files must be retained")
                self.assertNotEqual((install.install_root() / "current").resolve(), old)
                self.assertEqual(json.loads((settings / "config.json").read_text())["job_total_discs"], 123)
                request("/api/quit", {})
                self.assertEqual(proc.wait(timeout=8), 0)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=8)

    def test_asset_selection_rejects_foreign_or_unverified_downloads(self):
        info = self.update_info()
        asset = {"name": self.archive.name, "browser_download_url": info["installer"], "digest": info["digest"]}
        self.assertTrue(linux_update.release_asset(asset, self.version, "Shelfrip/ShelfRip"))
        self.assertFalse(linux_update.release_asset({**asset, "digest": ""}, self.version, "Shelfrip/ShelfRip"))
        self.assertFalse(linux_update.release_asset({**asset, "browser_download_url": "https://example.com/app"}, self.version, "Shelfrip/ShelfRip"))

    def test_installed_launcher_runs_reuses_and_quits_real_application(self):
        self.installed()
        settings = Path(self.env["SHELFRIP_DATA"])
        settings.mkdir()
        (settings / "config.json").write_text(json.dumps({"check_updates": False, "tray_icon": False,
            "auto_rip": False, "shared_tmdb": False, "share_discs": False, "use_community": False}))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        command = [str(install.executable()), "--background", f"--port={port}"]
        base = f"http://127.0.0.1:{port}"
        def request(path, body=None):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.load(response)
        try:
            launched = subprocess.run(command, capture_output=True, text=True, timeout=20)
            self.assertEqual(launched.returncode, 0, launched.stderr)
            self.assertEqual(request("/api/version")["current"], self.version)
            boot = request("/api/drives")["boot"]
            again = subprocess.run(command, capture_output=True, text=True, timeout=20)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertIn("already running", again.stdout)
            self.assertEqual(request("/api/drives")["boot"], boot)
            self.assertTrue(request("/api/settings")["desktop"]["installed"])
            with self.assertRaisesRegex(ValueError, "Quit Shelfrip"):
                install.uninstall()
            self.assertTrue(request("/api/linux/login", {"enabled": True})["desktop"]["start_at_login"])
            self.assertFalse(request("/api/linux/login", {"enabled": False})["desktop"]["start_at_login"])
        finally:
            try:
                request("/api/quit", {})
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    try:
                        request("/api/version")
                    except OSError:
                        break
                    time.sleep(0.1)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
