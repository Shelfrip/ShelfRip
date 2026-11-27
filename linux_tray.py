"""StatusNotifier tray via the system's GTK3/AppIndicator libraries.

GTK runs in a separate process. JSON pipes carry menu snapshots and action IDs;
no commands or user-provided text are executed by a shell.
"""
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


class LinuxTray:
    def __init__(self, tip, on_click, menu, icon):
        self.tip, self.on_click, self.menu, self.icon = tip, on_click, menu, icon
        self.ok = False
        self.error = "Tray has not started."
        self.proc = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._generation = 0
        self._actions = {}

    def start(self):
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            self.error = "No graphical session. Use the web interface to open, restart or quit Shelfrip."
            return False
        try:
            self.proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--helper", self.icon],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         text=True, encoding="utf-8", start_new_session=True)
            threading.Thread(target=self._read, daemon=True, name="linux-tray-events").start()
            self.set_tip(self.tip)
            if not self._ready.wait(7):
                self.error = "No compatible tray host. Enable StatusNotifier/AppIndicator support in your desktop, then restart Shelfrip."
            if not self.ok:
                self.remove()
            return self.ok
        except (OSError, ValueError):
            self.error = "The Linux tray helper could not start. Use the web interface instead."
            self.remove()
            return False

    def _read(self):
        try:
            for line in self.proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "connected":
                    self.ok = bool(event.get("value"))
                    self.error = "" if self.ok else "The desktop tray is unavailable. Use the web interface until it returns."
                    if self.ok:
                        self._ready.set()
                elif event.get("event") == "error":
                    self.error = event.get("message", "Linux tray unavailable.")
                    self._ready.set()
                elif event.get("event") == "activate":
                    with self._lock:
                        action = self._actions.get(event.get("id"))
                    if action:
                        threading.Thread(target=action, daemon=True).start()
        finally:
            self.ok = False
            if not self.error:
                self.error = "The tray helper stopped. Restart Shelfrip to reconnect it."
            self._ready.set()
            self.proc.stdout.close()

    def set_tip(self, tip):
        self.tip = tip
        if not self.proc or self.proc.poll() is not None:
            return
        with self._lock:
            self._generation += 1
            actions, items = {}, []
            for i, item in enumerate(self.menu()):
                if item is None:
                    items.append(None)
                    continue
                label, action = item
                key = f"{self._generation}:{i}"
                if action:
                    actions[key] = action
                items.append({"id": key, "label": label, "enabled": bool(action)})
            self._actions = actions
            try:
                self.proc.stdin.write(json.dumps({"tip": tip, "items": items}) + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError):
                self.ok = False
                self.error = "The tray helper stopped. Use the web interface to restart Shelfrip."

    def notify(self, title, text):
        import desktop
        return desktop.notify(title, text)

    def remove(self):
        proc = self.proc
        if not proc:
            return
        with self._lock:
            try:
                proc.stdin.close()  # EOF also cleans up the icon when the parent crashes or execs
            except (OSError, ValueError):
                pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        self.ok = False


def _helper(icon: str):
    def emit(event, **fields):
        print(json.dumps({"event": event, **fields}), flush=True)

    def library(*names):
        for name in names:
            path = ctypes.util.find_library(name)
            if path:
                return ctypes.CDLL(path)
        raise OSError("Missing GTK3 or Ayatana AppIndicator3. Install the desktop tray libraries and restart Shelfrip.")

    def bind(lib, name, returns, *args):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = returns, list(args)
        return fn

    pointer, integer, string = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
    try:
        gtk = library("gtk-3")
        indicator = library("ayatana-appindicator3", "appindicator3")
        glib, obj = library("glib-2.0"), library("gobject-2.0")
        init = bind(gtk, "gtk_init_check", integer, pointer, pointer)
        if not init(None, None):
            raise OSError("Cannot connect to the graphical session. Use the web interface instead.")
        new = bind(indicator, "app_indicator_new", pointer, string, string, integer)
        set_menu = bind(indicator, "app_indicator_set_menu", None, pointer, pointer)
        set_status = bind(indicator, "app_indicator_set_status", None, pointer, integer)
        set_title = bind(indicator, "app_indicator_set_title", None, pointer, string)
        menu_new = bind(gtk, "gtk_menu_new", pointer)
        item_new = bind(gtk, "gtk_menu_item_new_with_label", pointer, string)
        separator = bind(gtk, "gtk_separator_menu_item_new", pointer)
        append = bind(gtk, "gtk_menu_shell_append", None, pointer, pointer)
        sensitive = bind(gtk, "gtk_widget_set_sensitive", None, pointer, integer)
        show = bind(gtk, "gtk_widget_show_all", None, pointer)
        destroy = bind(gtk, "gtk_widget_destroy", None, pointer)
        main = bind(gtk, "gtk_main", None)
        quit_main = bind(gtk, "gtk_main_quit", None)
        connect = bind(obj, "g_signal_connect_data", ctypes.c_ulong, pointer, string, pointer, pointer, pointer, integer)
        timeout = bind(glib, "g_timeout_add", ctypes.c_uint, ctypes.c_uint, pointer, pointer)
        unref = bind(obj, "g_object_unref", None, pointer)
        ref_sink = bind(obj, "g_object_ref_sink", pointer, pointer)
        app = new(b"shelfrip", icon.encode(), 0)  # APPLICATION_STATUS
        if not app:
            raise OSError("The desktop could not create a tray indicator.")
    except (OSError, AttributeError) as exc:
        emit("error", message=str(exc))
        return 1

    incoming = queue.Queue()

    def read():
        try:
            for line in sys.stdin:
                try:
                    incoming.put(json.loads(line))
                except ValueError:
                    pass
        finally:
            incoming.put(None)

    threading.Thread(target=read, daemon=True).start()
    connected_type = ctypes.CFUNCTYPE(None, pointer, integer, pointer)
    click_type = ctypes.CFUNCTYPE(None, pointer, pointer)
    tick_type = ctypes.CFUNCTYPE(integer, pointer)

    @connected_type
    def connected(_app, value, _data):
        emit("connected", value=bool(value))

    connect(app, b"connection-changed", connected, None, None, 0)
    current_menu, callbacks = None, []

    @tick_type
    def tick(_data):
        nonlocal current_menu, callbacks
        try:
            snapshot = incoming.get_nowait()
        except queue.Empty:
            return 1
        if snapshot is None:
            set_status(app, 0)  # PASSIVE
            quit_main()
            return 0
        new_menu, new_callbacks = menu_new(), []
        ref_sink(new_menu)
        for item in snapshot.get("items", []):
            widget = item_new(item["label"].encode()) if item else separator()
            if item:
                sensitive(widget, int(item["enabled"]))
                if item["enabled"]:
                    key = item["id"]
                    callback = click_type(lambda _w, _d, action=key: emit("activate", id=action))
                    new_callbacks.append(callback)
                    connect(widget, b"activate", callback, None, None, 0)
            append(new_menu, widget)
        show(new_menu)
        set_menu(app, new_menu)
        if current_menu:
            destroy(current_menu)
            unref(current_menu)
        current_menu, callbacks = new_menu, new_callbacks
        set_title(app, snapshot.get("tip", "Shelfrip").encode())
        set_status(app, 1)  # ACTIVE
        return 1

    timeout(100, tick, None)
    main()
    unref(app)
    if current_menu:
        destroy(current_menu)
        unref(current_menu)
    return 0


if __name__ == "__main__":
    raise SystemExit(_helper(sys.argv[2]))
