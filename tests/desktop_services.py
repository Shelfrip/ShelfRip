"""Optional native integration suite: python3 tests/desktop_services.py.

Needs dbus-run-session, Xvfb/xauth, GTK3, Ayatana AppIndicator3, secret-tool,
gnome-keyring and PyGObject (test harness only). Creates a private D-Bus session
and temporary home; never reads or modifies the login session's keyring.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def isolated_session():
    with tempfile.TemporaryDirectory(prefix="shelfrip-desktop-") as tmp:
        env = {**os.environ, "HOME": tmp, "XDG_CONFIG_HOME": tmp + "/config",
               "XDG_DATA_HOME": tmp + "/data", "XDG_CACHE_HOME": tmp + "/cache",
               "XDG_RUNTIME_DIR": tmp + "/run", "SHELFRIP_DATA": tmp + "/shelfrip",
               "PYTHONDONTWRITEBYTECODE": "1", "GTK_MODULES": "", "NO_AT_BRIDGE": "1"}
        for key in ("DBUS_SESSION_BUS_ADDRESS", "GNOME_KEYRING_CONTROL", "WAYLAND_DISPLAY"):
            env.pop(key, None)
        Path(env["XDG_RUNTIME_DIR"]).mkdir(mode=0o700)
        return subprocess.run(["dbus-run-session", "--", "xvfb-run", "-a", sys.executable,
                               str(Path(__file__).resolve()), "--session"], env=env).returncode


if __name__ == "__main__" and "--session" not in sys.argv:
    raise SystemExit(isolated_session())

if "--session" in sys.argv:
    sys.argv.remove("--session")
    from gi.repository import Gio, GLib
    import desktop
    import linux_keyring
    import linux_tray
    import app
    import paths
    import linux_window
    import linux_install
    import build_linux
    import socket
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    WATCHER = "org.kde.StatusNotifierWatcher"
    XML = '''<node><interface name="org.kde.StatusNotifierWatcher">
      <method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
      <method name="RegisterStatusNotifierHost"><arg type="s" direction="in"/></method>
      <property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
      <property name="RegisteredStatusNotifierItems" type="as" access="read"/>
      <property name="ProtocolVersion" type="i" access="read"/>
      <signal name="StatusNotifierHostRegistered"/>
      <signal name="StatusNotifierItemRegistered"><arg type="s"/></signal>
    </interface></node>'''

    def eventually(predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError("Desktop service did not reach expected state")

    class Services(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            subprocess.run(["gnome-keyring-daemon", "--unlock", "--components=secrets"],
                           input=b"isolated-test-keyring", check=True, capture_output=True, timeout=10)
            cls.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            cls.loop = GLib.MainLoop()
            cls.thread = threading.Thread(target=cls.loop.run, daemon=True)
            cls.thread.start()

        @classmethod
        def tearDownClass(cls):
            cls.loop.quit()
            cls.thread.join(timeout=3)

        def test_real_keyring_save_replacement_and_removal(self):
            password = " test-only ü\n"
            paths.write_config({})
            app._save_settings({"opensubtitles_password": password})
            first = json.loads(paths.CONFIG_PATH.read_text())["opensubtitles_password"]
            self.assertTrue(linux_keyring.is_reference(first))
            self.assertEqual(linux_keyring.lookup(first), password)
            self.assertNotIn(password, paths.CONFIG_PATH.read_text())
            try:
                app._save_settings({"opensubtitles_password": "replacement test-only"})
                second = json.loads(paths.CONFIG_PATH.read_text())["opensubtitles_password"]
                self.assertNotEqual(first, second)
                self.assertEqual(linux_keyring.lookup(second), "replacement test-only")
                with self.assertRaises(linux_keyring.KeyringError):
                    linux_keyring.lookup(first)
                app._save_settings({"opensubtitles_password": None})
                with self.assertRaises(linux_keyring.KeyringError):
                    linux_keyring.lookup(second)
            finally:
                linux_keyring.cleanup(first)
                linux_keyring.cleanup(json.loads(paths.CONFIG_PATH.read_text()).get("opensubtitles_password", ""))

        def test_native_window_reuses_closes_and_reopens(self):
            class Page(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<!doctype html><title>Shelfrip</title><h1>Local window test</h1>")
                def log_message(self, *_):
                    pass
            server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            manager = subprocess.Popen(["openbox"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            url = f"http://127.0.0.1:{server.server_port}/"
            def windows():
                return [line for line in subprocess.check_output(["wmctrl", "-lp"], text=True).splitlines()
                        if line.endswith(" Shelfrip")]
            try:
                self.assertTrue(linux_window.open_window(url))
                first = linux_window._window.proc.pid
                eventually(lambda: len(windows()) == 1)
                self.assertTrue(linux_window.open_window(url + "#review"))
                self.assertEqual(linux_window._window.proc.pid, first)
                self.assertEqual(len(windows()), 1)
                subprocess.run(["wmctrl", "-ic", windows()[0].split()[0]], check=True)
                eventually(lambda: linux_window._window.proc.poll() is not None)
                self.assertTrue(linux_window.open_window(url))
                self.assertNotEqual(linux_window._window.proc.pid, first)
                eventually(lambda: len(windows()) == 1)
                linux_window.close()
                eventually(lambda: not windows())
            finally:
                linux_window.close()
                manager.terminate()
                manager.wait(timeout=5)
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

        def test_desktop_entry_escaping_with_native_parser(self):
            command = ["/bin/echo", 'space ü $dollar `tick` "quote" \\slash %percent']
            filename = Path(os.environ["HOME"]) / "test.desktop"
            filename.write_text(linux_install.entry(command, Path("/tmp/icon.svg")))
            subprocess.run(["desktop-file-validate", str(filename)], check=True)
            appinfo = Gio.DesktopAppInfo.new_from_filename(str(filename))
            self.assertIsNotNone(appinfo)
            # Launch an executable recorder to test the actual argument vector.
            record = Path(os.environ["HOME"]) / "args.json"
            script = Path(os.environ["HOME"]) / "record.py"
            script.write_text("import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))")
            filename.write_text(linux_install.entry([sys.executable, str(script), str(record), command[1]], Path("/tmp/icon.svg")))
            appinfo = Gio.DesktopAppInfo.new_from_filename(str(filename))
            self.assertTrue(appinfo.launch([], None))
            eventually(record.exists)
            self.assertEqual(json.loads(record.read_text()), [command[1]])

        def test_installed_app_menu_launch_reuses_window_and_keeps_server_after_close(self):
            package = build_linux.build(ROOT, Path(os.environ["HOME"]) / "package")
            version = package.name.removeprefix("Shelfrip-").removesuffix("-linux.tar.gz")
            linux_install.install_archive(package, version)
            data = Path(os.environ["HOME"]) / "launcher-data"
            data.mkdir()
            (data / "config.json").write_text(json.dumps({"check_updates": False, "auto_rip": False,
                "tray_icon": False, "shared_tmdb": False, "share_discs": False, "use_community": False}))
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            entry = linux_install.data_home() / "applications" / linux_install.DESKTOP_ID
            entry.write_text(linux_install.entry([str(linux_install.executable()), f"--port={port}"],
                                                 linux_install.install_root() / "icon.svg"))
            appinfo = Gio.DesktopAppInfo.new_from_filename(str(entry))
            context = Gio.AppLaunchContext()
            context.setenv("SHELFRIP_DATA", str(data))
            manager = subprocess.Popen(["openbox"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            def request(route, post=False):
                req = urllib.request.Request(f"http://127.0.0.1:{port}" + route,
                    data=b"{}" if post else None, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=2) as response:
                    return json.load(response)
            def ready():
                try:
                    return request("/api/version")["app"] == "Shelfrip"
                except OSError:
                    return False
            def windows():
                return [line for line in subprocess.check_output(["wmctrl", "-lp"], text=True).splitlines()
                        if line.endswith(" Shelfrip")]
            try:
                self.assertTrue(appinfo.launch([], context))
                eventually(ready, 15)
                eventually(lambda: len(windows()) == 1, 15)
                first = windows()[0].split()[0]
                boot = request("/api/drives")["boot"]
                self.assertTrue(appinfo.launch([], context))
                time.sleep(1)
                self.assertEqual([w.split()[0] for w in windows()], [first])
                self.assertEqual(request("/api/drives")["boot"], boot)
                subprocess.run(["wmctrl", "-ic", first], check=True)
                eventually(lambda: not windows())
                self.assertTrue(ready(), "closing the window stopped the server")
                self.assertTrue(appinfo.launch([], context))
                eventually(lambda: len(windows()) == 1, 15)
                self.assertEqual(request("/api/drives")["boot"], boot)
            finally:
                if ready():
                    request("/api/quit", True)
                    eventually(lambda: not ready(), 10)
                manager.terminate()
                manager.wait(timeout=5)
                linux_install.uninstall()

        def test_real_indicator_menu_reconnection_and_cleanup(self):
            registered = []

            def method(conn, sender, path, interface, name, parameters, invocation):
                value = parameters.unpack()[0]
                if name == "RegisterStatusNotifierItem":
                    registered.append((sender, value) if value.startswith("/") else (value, "/StatusNotifierItem"))
                invocation.return_value(None)

            def prop(*args):
                name = args[-1]
                return {"IsStatusNotifierHostRegistered": GLib.Variant("b", True),
                        "RegisteredStatusNotifierItems": GLib.Variant("as", []),
                        "ProtocolVersion": GLib.Variant("i", 0)}[name]

            object_id = self.bus.register_object("/StatusNotifierWatcher",
                Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], method, prop, None)
            acquired = threading.Event()
            def own():
                acquired.clear()
                owner = Gio.bus_own_name_on_connection(self.bus, WATCHER, Gio.BusNameOwnerFlags.NONE,
                    lambda *_: acquired.set(), None)
                self.assertTrue(acquired.wait(5))
                return owner
            owner = own()
            clicked = threading.Event()
            tray = desktop.create_tray("Shelfrip test", None,
                lambda: [("Open Shelfrip", clicked.set), ("Status", None), None, ("Quit", clicked.set)])
            try:
                self.assertTrue(tray.start(), tray.error)
                eventually(lambda: registered)
                destination, path = registered[-1]
                def call(interface, name, signature, args, target=path):
                    return self.bus.call_sync(destination, target, interface, name,
                        GLib.Variant(signature, args), None, Gio.DBusCallFlags.NONE, 5000, None).unpack()
                menu_path = call("org.freedesktop.DBus.Properties", "Get", "(ss)",
                    ("org.kde.StatusNotifierItem", "Menu"))[0]
                def layout():
                    return call("com.canonical.dbusmenu", "GetLayout", "(iias)", (0, -1, []), menu_path)[1]
                eventually(lambda: len(layout()[2]) == 4)
                items = layout()[2]
                self.assertEqual(items[0][1]["label"], "Open Shelfrip")
                self.assertFalse(items[1][1]["enabled"])
                self.assertEqual(items[2][1]["type"], "separator")
                call("com.canonical.dbusmenu", "Event", "(isvu)",
                     (items[0][0], "clicked", GLib.Variant("i", 0), 0), menu_path)
                self.assertTrue(clicked.wait(5), "Native menu activation did not reach Python callback")
                for n in range(10):
                    tray.set_tip(f"Updated {n}")
                eventually(lambda: call("org.freedesktop.DBus.Properties", "Get", "(ss)",
                    ("org.kde.StatusNotifierItem", "Title"))[0] == "Updated 9")
                Gio.bus_unown_name(owner)
                owner = None
                eventually(lambda: not tray.ok)
                owner = own()
                eventually(lambda: tray.ok)
                # Abrupt app death must also close the helper's input and remove its icon.
                parent = subprocess.Popen([sys.executable, "-c",
                    "import desktop,time; t=desktop.create_tray('Crash test',None,lambda: [('Open',lambda:None)]); "
                    "assert t.start(),t.error; print(t.proc.pid,flush=True); time.sleep(60)"],
                    cwd=ROOT, stdout=subprocess.PIPE, text=True)
                try:
                    import select
                    self.assertTrue(select.select([parent.stdout], [], [], 12)[0])
                    helper_pid = int(parent.stdout.readline())
                    parent.kill()
                    parent.wait(timeout=3)
                    def helper_stopped():
                        stat = Path(f"/proc/{helper_pid}/stat")
                        return not stat.exists() or stat.read_text().split(")", 1)[1].split()[0] == "Z"
                    eventually(helper_stopped)
                finally:
                    if parent.poll() is None:
                        parent.kill()
                    parent.wait(timeout=3)
                    parent.stdout.close()
            finally:
                tray.remove()
                if owner is not None:
                    Gio.bus_unown_name(owner)
                self.bus.unregister_object(object_id)
            self.assertEqual(tray.proc.returncode, 0)
            self.assertFalse(tray.ok)

        def test_missing_host_exits_helper_and_keeps_app_usable(self):
            tray = desktop.create_tray("Shelfrip test", None, lambda: [("Open", lambda: None)])
            try:
                self.assertFalse(tray.start())
                self.assertIn("tray host", tray.error)
                self.assertIsNotNone(tray.proc.poll())
            finally:
                tray.remove()

    unittest.main()
