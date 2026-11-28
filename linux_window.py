"""An optional reusable GTK3/WebKitGTK app window, isolated from the web server."""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import urllib.parse
import webbrowser

_lock = threading.Lock()
_window = None


class Window:
    def __init__(self):
        self.proc = None
        self.ready = threading.Event()
        self.ok = False

    def read(self, proc):
        try:
            for line in proc.stdout:
                event = json.loads(line)
                self.ok = event.get("event") == "ready"
                self.ready.set()
        finally:
            self.ok = False
            self.ready.set()
            proc.stdout.close()

    def show(self, url, startup_id=""):
        if not self.proc or self.proc.poll() is not None:
            self.ready.clear()
            self.proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--helper", url],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", start_new_session=True)
            threading.Thread(target=self.read, args=(self.proc,), daemon=True).start()
            if not self.ready.wait(10) or not self.ok:
                self.close()
                return False
        self.proc.stdin.write(json.dumps({"url": url, "startup_id": startup_id}) + "\n")
        self.proc.stdin.flush()
        return True

    def close(self):
        if not self.proc:
            return
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.ok = False


def open_window(url: str, startup_id="") -> bool:
    global _window
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    if not ctypes.util.find_library("gtk-3") or not any(ctypes.util.find_library(n) for n in ("webkit2gtk-4.1", "webkit2gtk-4.0")):
        return False
    with _lock:
        try:
            if _window is None or (_window.proc and _window.proc.poll() is not None):
                if _window:
                    _window.close()
                _window = Window()
            return _window.show(url, startup_id)
        except (OSError, ValueError):
            if _window:
                _window.close()
            return False


def close():
    global _window
    with _lock:
        if _window:
            _window.close()
            _window = None


def _helper(url):
    def library(*names):
        for name in names:
            path = ctypes.util.find_library(name)
            if path:
                return ctypes.CDLL(path)
        raise OSError("GTK3/WebKitGTK is unavailable")

    def bind(lib, name, returns, *args):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = returns, list(args)
        return fn

    pointer, integer, string = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
    callbacks = []
    try:
        gtk, webkit = library("gtk-3"), library("webkit2gtk-4.1", "webkit2gtk-4.0")
        glib, obj = library("glib-2.0"), library("gobject-2.0")
        bind(glib, "g_set_prgname", None, string)(b"org.shelfrip.Shelfrip")
        bind(glib, "g_set_application_name", None, string)(b"Shelfrip")
        if not bind(gtk, "gtk_init_check", integer, pointer, pointer)(None, None):
            raise OSError("No graphical session")
        window = bind(gtk, "gtk_window_new", pointer, integer)(0)
        import paths
        storage = paths.DATA_DIR / "webview"
        storage.mkdir(mode=0o700, parents=True, exist_ok=True)
        manager_new = bind(webkit, "webkit_website_data_manager_new", pointer, string)
        manager = manager_new(b"base-data-directory", string(os.fsencode(storage / "data")),
                              b"base-cache-directory", string(os.fsencode(storage / "cache")), None)
        context = bind(webkit, "webkit_web_context_new_with_website_data_manager", pointer, pointer)(manager)
        view = bind(webkit, "webkit_web_view_new_with_context", pointer, pointer)(context)
        unref = bind(obj, "g_object_unref", None, pointer)
        unref(context)
        unref(manager)
        load = bind(webkit, "webkit_web_view_load_uri", None, pointer, string)
        uri = bind(webkit, "webkit_web_view_get_uri", string, pointer)
        present = bind(gtk, "gtk_window_present", None, pointer)
        startup = bind(gtk, "gtk_window_set_startup_id", None, pointer, string)
        quit_main = bind(gtk, "gtk_main_quit", None)
        connect = bind(obj, "g_signal_connect_data", ctypes.c_ulong, pointer, string, pointer, pointer, pointer, integer)
        bind(gtk, "gtk_window_set_title", None, pointer, string)(window, b"Shelfrip")
        bind(gtk, "gtk_window_set_wmclass", None, pointer, string, string)(window, b"shelfrip", b"Shelfrip")
        bind(gtk, "gtk_window_set_default_size", None, pointer, integer, integer)(window, 1280, 760)
        bind(gtk, "gtk_window_set_icon_name", None, pointer, string)(window, b"media-optical")
        import linux_install
        icon = linux_install.install_root() / "icon.svg"
        if icon.is_file():
            bind(gtk, "gtk_window_set_icon_from_file", integer, pointer, string, pointer)(window, os.fsencode(icon), None)
        bind(gtk, "gtk_container_add", None, pointer, pointer)(window, view)
        click_type = ctypes.CFUNCTYPE(None, pointer, pointer)
        policy_type = ctypes.CFUNCTYPE(integer, pointer, pointer, integer, pointer)
        tick_type = ctypes.CFUNCTYPE(integer, pointer)
        origin = urllib.parse.urlsplit(url)[:2]
        action = bind(webkit, "webkit_navigation_policy_decision_get_navigation_action", pointer, pointer)
        request = bind(webkit, "webkit_navigation_action_get_request", pointer, pointer)
        request_uri = bind(webkit, "webkit_uri_request_get_uri", string, pointer)
        ignore = bind(webkit, "webkit_policy_decision_ignore", None, pointer)

        @policy_type
        def policy(_view, decision, kind, _data):
            if kind not in (0, 1):  # navigation/new window; response policy stays WebKit's
                return 0
            target = request_uri(request(action(decision)))
            target = target.decode() if target else ""
            parsed = urllib.parse.urlsplit(target)
            if parsed[:2] == origin and kind == 0:
                return 0
            ignore(decision)
            if parsed[:2] == origin:
                load(view, target.encode())
            elif parsed.scheme in ("https", "http"):
                threading.Thread(target=webbrowser.open, args=(target,), daemon=True).start()
            return 1

        @click_type
        def destroyed(_window, _data):
            quit_main()

        connect(window, b"destroy", destroyed, None, None, 0)
        connect(view, b"decide-policy", policy, None, None, 0)
        callbacks.extend([destroyed, policy])
        incoming = queue.Queue()

        def read():
            try:
                for line in sys.stdin:
                    incoming.put(json.loads(line))
            finally:
                incoming.put(None)

        threading.Thread(target=read, daemon=True).start()

        @tick_type
        def tick(_data):
            try:
                message = incoming.get_nowait()
            except queue.Empty:
                return 1
            if message is None:
                bind(gtk, "gtk_widget_destroy", None, pointer)(window)
                return 0
            target = message["url"]
            if urllib.parse.urlsplit(target)[:2] != origin:
                return 1
            if message.get("startup_id"):
                startup(window, message["startup_id"].encode())
            current = (uri(view) or b"").decode()
            # Opening the main page again preserves the current review/settings view.
            if current.split("#")[0] != target.split("#")[0] or "#" in target:
                load(view, target.encode())
            present(window)
            return 1

        callbacks.append(tick)
        bind(glib, "g_timeout_add", ctypes.c_uint, ctypes.c_uint, pointer, pointer)(100, tick, None)
        load(view, url.encode())
        bind(gtk, "gtk_widget_show_all", None, pointer)(window)
        print(json.dumps({"event": "ready"}), flush=True)
        bind(gtk, "gtk_main", None)()
        return 0
    except (OSError, AttributeError):
        print(json.dumps({"event": "error"}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(_helper(sys.argv[2]))
