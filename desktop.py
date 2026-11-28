r"""
desktop.py - Shelfrip's desktop window, tray, dialogs and notifications.

  open_window(url)   opens the page in an app window (Edge's app mode: no tabs or address
                     bar; Edge comes with Windows 10 and 11), else Chrome, else the browser.
  Tray(...)          the icon in the notification area. Click it to open Shelfrip;
                     right-click for a menu. Closing the window leaves Shelfrip running there.
  write_icon(path)   writes shelfrip.ico (the tray, the exe and the installer use it).
  hide_console()     hides the black console window once the tray icon is up.

Windows integration uses ctypes. Linux uses desktop programs on PATH for opening files,
dialogs and notifications, plus GTK3/AppIndicator for the tray. Python dependencies are standard library.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import threading
import webbrowser
import zlib
from pathlib import Path

import paths

ICON_PATH = paths.DATA_DIR / "shelfrip.ico"


def create_tray(tip, on_click, menu):
    if sys.platform.startswith("linux"):
        from linux_tray import LinuxTray
        icon = paths.DATA_DIR / "shelfrip-tray.png"
        rows = b"".join(b"\0" + bytes(c for pixel in row for c in pixel) for row in _pixels(64))
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        icon.parent.mkdir(parents=True, exist_ok=True)
        icon.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 6, 0, 0, 0))
                         + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
        return LinuxTray(tip, on_click, menu, str(icon))
    tray = Tray(tip, on_click, menu)
    if sys.platform == "darwin":
        tray.error = "The macOS menu-bar icon is not implemented yet. Use the page to quit."
    return tray


# --------------------------------------------------------------------------- icon

def _pixels(size: int) -> list[list[tuple[int, int, int, int]]]:
    """The Shelfrip mark (three spines on a shelf), drawn on a size x size grid."""
    clear = (0, 0, 0, 0)
    img = [[clear] * size for _ in range(size)]
    k = size / 28.0
    shapes = [  # x, y, w, h on a 28 grid, RGBA
        (3, 4, 5, 18, (0xF2, 0xB3, 0x3D, 255)),
        (10, 7, 5, 15, (0x6C, 0x9B, 0xFF, 255)),
        (17, 5, 5, 17, (0x4C, 0xC3, 0x8A, 255)),
        (2, 23, 24, 2.5, (0xC9, 0xD1, 0xDC, 255)),
    ]
    for x, y, w, h, c in shapes:
        x0, y0 = int(round(x * k)), int(round(y * k))
        x1, y1 = max(x0 + 1, int(round((x + w) * k))), max(y0 + 1, int(round((y + h) * k)))
        for yy in range(y0, min(y1, size)):
            for xx in range(x0, min(x1, size)):
                img[yy][xx] = c
    return img


def write_icon(path: Path = ICON_PATH) -> Path:
    """An .ico with 16, 24, 32, 48 and 256 px images (32-bit, uncompressed)."""
    sizes = (16, 24, 32, 48, 256)
    images = []
    for s in sizes:
        px = _pixels(s)
        data = bytearray()
        for row in reversed(px):  # BMP rows go bottom-up, pixels as BGRA
            for r, g, b, a in row:
                data += bytes((b, g, r, a))
        mask_row = ((s + 31) // 32) * 4
        data += bytes(mask_row * s)  # AND mask: all zero (alpha does the work)
        header = struct.pack("<IiiHHIIiiII", 40, s, s * 2, 1, 32, 0, len(data), 0, 0, 0, 0)
        images.append(header + bytes(data))
    out = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    for s, img in zip(sizes, images):
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
    out += b"".join(images)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out)
    return path


# --------------------------------------------------------------------------- app window

def _find_browser() -> str | None:
    if sys.platform.startswith("linux"):
        return next((p for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
                     if (p := shutil.which(name))), None)
    if os.name != "nt":
        return None
    roots = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    names = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"]
    for name in names:  # Edge first: it's on every Windows 10/11 PC
        for root in roots:
            if root and Path(root, name).is_file():
                return str(Path(root, name))
    return shutil.which("msedge") or shutil.which("chrome")


def _window_box() -> list[str]:
    """Size and place the window to fit the screen it opens on (inside the taskbar), centred.
    Sizes are in the same scaled units Edge uses, so 125% or 150% display scaling is fine."""
    w, h, x0, y0 = 1320, 900, 0, 0
    try:
        import ctypes

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        r = RECT()
        if ctypes.WinDLL("user32").SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0):  # SPI_GETWORKAREA
            aw, ah = r.right - r.left, r.bottom - r.top
            w, h = min(w, int(aw * 0.92)), min(h, int(ah * 0.9))
            x0, y0 = r.left + (aw - w) // 2, r.top + (ah - h) // 2
    except Exception:
        return ["--window-size=1280,760"]
    return [f"--window-size={w},{h}", f"--window-position={x0},{y0}"]


APP_ID = "Shelfrip.App"  # the taskbar identity of Shelfrip's window (and of the installer's shortcuts)


def _shelfrip_windows() -> list:
    """Handles of the open Shelfrip app windows (Edge/Chrome windows titled just "Shelfrip";
    a browser tab would add " - Microsoft Edge")."""
    import ctypes
    u32 = ctypes.WinDLL("user32")
    found = []
    PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    u32.EnumWindows.argtypes = [PROC, ctypes.c_void_p]
    u32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    u32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    u32.IsWindowVisible.argtypes = [ctypes.c_void_p]

    def each(hwnd, _):
        title, cls = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(64)
        u32.GetWindowTextW(hwnd, title, 256)
        u32.GetClassNameW(hwnd, cls, 64)
        if title.value == "Shelfrip" and cls.value.startswith("Chrome_WidgetWin") and u32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True
    u32.EnumWindows(PROC(each), None)
    return found


def relaunch_command() -> str:
    """What a pinned taskbar button runs: Shelfrip.exe, or pythonw app.py when run from the scripts."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else exe}" "{Path(__file__).resolve().with_name("app.py")}"'


def brand_window(hwnd) -> bool:
    """Give an app window Shelfrip's own taskbar identity, so it shows as Shelfrip (not Edge),
    with Shelfrip's icon, and pinning it pins Shelfrip. Uses the window's property store."""
    try:
        import ctypes

        class GUID(ctypes.Structure):
            _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]

        class PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", GUID), ("pid", ctypes.c_uint32)]

        class PROPVARIANT(ctypes.Structure):
            _fields_ = [("vt", ctypes.c_uint16), ("r1", ctypes.c_uint16), ("r2", ctypes.c_uint16),
                        ("r3", ctypes.c_uint16), ("val", ctypes.c_void_p), ("pad", ctypes.c_void_p)]

        def guid(a, b, c, d):
            return GUID(a, b, c, (ctypes.c_ubyte * 8)(*d))
        fmt = guid(0x9F4C2855, 0x9F79, 0x4B39, (0xA8, 0xD0, 0xE1, 0xD4, 0x2D, 0xE1, 0xD5, 0xF3))  # PKEY_AppUserModel_*
        iid = guid(0x886D8EEB, 0x8CF2, 0x4446, (0x8D, 0x02, 0xCD, 0xBA, 0x1D, 0xBD, 0xCF, 0x99))  # IID_IPropertyStore
        ole = ctypes.WinDLL("ole32")
        ole.CoInitialize(None)
        s32 = ctypes.WinDLL("shell32")
        s32.SHGetPropertyStoreForWindow.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        store = ctypes.c_void_p()
        if s32.SHGetPropertyStoreForWindow(hwnd, ctypes.byref(iid), ctypes.byref(store)) != 0 or not store:
            return False
        vtbl = ctypes.cast(ctypes.cast(store, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        HR = ctypes.c_long
        SetValue = ctypes.WINFUNCTYPE(HR, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(vtbl[6])
        Commit = ctypes.WINFUNCTYPE(HR, ctypes.c_void_p)(vtbl[7])
        Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
        icon = f"{sys.executable},0" if getattr(sys, "frozen", False) else \
            str(ICON_PATH if ICON_PATH.exists() else write_icon())
        keep = []
        try:
            # the relaunch details go in first; the id last (Windows reads them when the id is set)
            for pid, text in ((2, relaunch_command()), (3, icon), (4, "Shelfrip"), (5, APP_ID)):
                buf = ctypes.create_unicode_buffer(text)
                keep.append(buf)
                pv = PROPVARIANT(vt=31, val=ctypes.cast(buf, ctypes.c_void_p))  # VT_LPWSTR
                SetValue(store, ctypes.byref(PROPERTYKEY(fmt, pid)), ctypes.byref(pv))
            return Commit(store) == 0
        finally:
            Release(store)
    except Exception:
        return False


def _brand_when_open(before: set, timeout: float = 20.0) -> None:
    """Wait for the new app window to appear (it takes a moment to load) and brand it."""
    import time
    end = time.time() + timeout
    while time.time() < end:
        try:
            new = [h for h in _shelfrip_windows() if h not in before]
        except Exception:
            return
        if new:
            for h in new:
                brand_window(h)
            return
        time.sleep(0.5)


def _focus_existing() -> bool:
    """If a Shelfrip app window is already open, bring it to the front instead of opening another."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        found = _shelfrip_windows()
        if not found:
            return False
        u32 = ctypes.WinDLL("user32")
        u32.IsIconic.argtypes = [ctypes.c_void_p]
        u32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        if u32.IsIconic(found[0]):
            u32.ShowWindow(found[0], 9)  # SW_RESTORE
        u32.SetForegroundWindow(found[0])
        return True
    except Exception:
        return False


def open_window(url: str, use_window: bool = True, startup_id: str = "") -> None:
    """Open Shelfrip in its own window (falls back to a browser tab). If its window is already
    open (say you clicked the tray icon again), that window comes to the front instead."""
    if sys.platform.startswith("linux") and use_window:
        import linux_window
        if linux_window.open_window(url, startup_id):
            return
    if use_window and "#" not in url and _focus_existing():
        return
    exe = _find_browser() if use_window else None
    if exe:
        try:
            before = set(_shelfrip_windows())
        except Exception:
            before = set()
        try:
            subprocess.Popen([exe, f"--app={url}", *_window_box(), "--no-first-run",
                              "--no-default-browser-check"],
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) |
                             getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True)
            if os.name == "nt":
                threading.Thread(target=_brand_when_open, args=(before,), daemon=True).start()
            return
        except OSError:
            pass
    webbrowser.open(url)


# --------------------------------------------------------------------------- console

LOG_PATH = paths.DATA_DIR / "shelfrip.log"


def use_log_file() -> bool:
    """Started without a console window (Shelfrip.exe, pythonw)? Send what would have been
    printed there to shelfrip.log in the data folder instead. Returns True if it did."""
    if sys.stdout is not None and sys.stderr is not None:
        return False
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 5_000_000:
            LOG_PATH.replace(LOG_PATH.with_suffix(".old.log"))
        f = open(LOG_PATH, "a", encoding="utf-8", buffering=1, errors="replace")
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f
        return True
    except OSError:
        return False


def message_box(title: str, text: str) -> None:
    """A plain Windows message box (for errors when there's no console to print them to)."""
    if os.name != "nt":
        print(f"{title}: {text}")
        return
    try:
        import ctypes
        u32 = ctypes.WinDLL("user32")
        u32.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        u32.MessageBoxW(None, text, title, 0x10 | 0x40000)  # MB_ICONERROR | MB_TOPMOST
    except Exception:
        pass


def open_file(path: Path) -> None:
    if sys.platform == "darwin":
        result = subprocess.run(["/usr/bin/open", str(Path(path).resolve())], capture_output=True, timeout=15)
        if result.returncode:
            raise OSError("macOS could not open this file. Choose a default application in Finder.")
        return
    if sys.platform.startswith("linux"):
        exe = shutil.which("xdg-open")
        if not exe:
            raise OSError("Install xdg-utils to open files and folders from Shelfrip.")
        proc = subprocess.Popen([exe, str(Path(path).resolve())], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        # Some desktop openers wait for the player/editor to close. Don't block the API.
        threading.Thread(target=proc.wait, daemon=True).start()
        return
    os.startfile(str(path))  # type: ignore[attr-defined]


def notify(title: str, text: str) -> bool:
    """Linux desktop notification; optional on headless systems."""
    if sys.platform == "darwin":
        import macos_desktop
        return macos_desktop.notify(title, text)
    exe = shutil.which("notify-send") if sys.platform.startswith("linux") else None
    if not exe:
        return False
    try:
        return subprocess.run([exe, "--app-name=Shelfrip", "--", title, text],
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _pick_linux(kind: str, start: str, title: str) -> tuple[str | None, str]:
    if kind not in ("folder", "file"):
        return None, "Choose a file or folder."
    title = title or ("Choose a folder" if kind == "folder" else "Choose the program")
    filename = str(Path(start).expanduser().absolute()) if start else str(Path.home()) + "/"
    if Path(filename).is_dir() and not filename.endswith("/"):
        filename += "/"
    zenity, kdialog = shutil.which("zenity"), shutil.which("kdialog")
    kde = "kde" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if kdialog and (kde or not zenity):
        cmd = [kdialog, "--title", title, "--getexistingdirectory" if kind == "folder" else "--getopenfilename", filename]
    elif zenity:
        cmd = [zenity, "--file-selection", "--title=" + title, "--filename=" + filename]
        if kind == "folder":
            cmd.append("--directory")
    else:
        return None, "Install zenity or kdialog for the folder picker, or type the path instead."
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 1:  # user cancelled
            return "", ""
        if result.returncode:
            return None, result.stderr.strip() or "Couldn't open the picker. Type the path instead."
        return result.stdout.rstrip("\r\n"), ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"Couldn't open the picker: {exc}. Type the path instead."


def _console():
    import ctypes
    k32 = ctypes.WinDLL("kernel32")
    k32.GetConsoleWindow.restype = ctypes.c_void_p
    return ctypes, k32, k32.GetConsoleWindow()


def owns_console() -> bool:
    """True if the console window is ours alone (not a Command Prompt someone typed into)."""
    if os.name != "nt":
        return False
    try:
        ctypes, k32, hwnd = _console()
        if not hwnd:
            return False
        ids = (ctypes.c_uint32 * 4)()
        # a one-file exe runs as two processes (unpacker + app) sharing the console
        return k32.GetConsoleProcessList(ids, 4) <= (2 if getattr(sys, "frozen", False) else 1)
    except Exception:
        return False


def show_console(show: bool) -> None:
    if os.name != "nt":
        return
    try:
        ctypes, _, hwnd = _console()
        if hwnd:
            u32 = ctypes.WinDLL("user32")
            u32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
            u32.ShowWindow(hwnd, 5 if show else 0)  # SW_SHOW / SW_HIDE
            if show:
                u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
                u32.SetForegroundWindow(hwnd)
    except Exception:
        pass


# --------------------------------------------------------------------------- tray icon

class Tray:
    """The notification-area icon. `menu` is a function returning [(label, callback or None), ...]
    (None = separator), built fresh each time the menu opens so it shows the current state.
    `on_click` runs on a left click. Runs its own thread; start() returns False if it can't."""

    WM_TRAY = 0x8001  # WM_APP + 1

    def __init__(self, tip: str, on_click, menu) -> None:
        self.tip, self.on_click, self.menu = tip, on_click, menu
        self.hwnd = None
        self.taskbar_created = None  # set before the window exists: messages arrive during CreateWindowExW
        self._ready = threading.Event()
        self.ok = False

    def start(self) -> bool:
        if os.name != "nt":
            return False
        threading.Thread(target=self._run, daemon=True, name="tray").start()
        self._ready.wait(5)
        return self.ok

    # -- Win32 plumbing
    def _setup(self):
        import ctypes
        self.c = ctypes
        self.u32 = ctypes.WinDLL("user32", use_last_error=True)
        self.s32 = ctypes.WinDLL("shell32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT, HWND, UINT, WPARAM, LPARAM = ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t
        self.WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", UINT), ("lpfnWndProc", self.WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                        ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                        ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint32), ("hWnd", ctypes.c_void_p), ("uID", UINT), ("uFlags", UINT),
                        ("uCallbackMessage", UINT), ("hIcon", ctypes.c_void_p), ("szTip", ctypes.c_wchar * 128),
                        ("dwState", ctypes.c_uint32), ("dwStateMask", ctypes.c_uint32),
                        ("szInfo", ctypes.c_wchar * 256), ("uVersion", UINT), ("szInfoTitle", ctypes.c_wchar * 64),
                        ("dwInfoFlags", ctypes.c_uint32), ("guidItem", ctypes.c_ubyte * 16),
                        ("hBalloonIcon", ctypes.c_void_p)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class MSG(ctypes.Structure):
            _fields_ = [("hwnd", ctypes.c_void_p), ("message", UINT), ("wParam", WPARAM), ("lParam", LPARAM),
                        ("time", ctypes.c_uint32), ("pt", POINT), ("lPrivate", ctypes.c_uint32)]

        self.WNDCLASSW, self.NID, self.POINT, self.MSG = WNDCLASSW, NOTIFYICONDATAW, POINT, MSG
        u = self.u32
        u.DefWindowProcW.argtypes = [HWND, UINT, WPARAM, LPARAM]
        u.DefWindowProcW.restype = LRESULT
        u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        u.CreateWindowExW.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      HWND, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        u.CreateWindowExW.restype = HWND
        u.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, UINT, ctypes.c_int, ctypes.c_int, UINT]
        u.LoadImageW.restype = ctypes.c_void_p
        u.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u.LoadIconW.restype = ctypes.c_void_p
        u.CreatePopupMenu.restype = ctypes.c_void_p
        u.AppendMenuW.argtypes = [ctypes.c_void_p, UINT, ctypes.c_size_t, ctypes.c_wchar_p]
        u.TrackPopupMenu.argtypes = [ctypes.c_void_p, UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, HWND, ctypes.c_void_p]
        u.DestroyMenu.argtypes = [ctypes.c_void_p]
        u.SetForegroundWindow.argtypes = [HWND]
        u.PostMessageW.argtypes = [HWND, UINT, WPARAM, LPARAM]
        u.GetMessageW.argtypes = [ctypes.POINTER(MSG), HWND, UINT, UINT]
        u.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
        u.DispatchMessageW.restype = LRESULT
        u.RegisterWindowMessageW.argtypes = [ctypes.c_wchar_p]
        u.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
        self.s32.Shell_NotifyIconW.argtypes = [ctypes.c_uint32, ctypes.POINTER(NOTIFYICONDATAW)]
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        self.hinst = k32.GetModuleHandleW(None)

    def _nid(self, flags: int) -> "object":
        nid = self.NID()
        nid.cbSize = self.c.sizeof(self.NID)
        nid.hWnd, nid.uID, nid.uFlags = self.hwnd, 1, flags
        return nid

    def _add(self) -> bool:
        nid = self._nid(0x1 | 0x2 | 0x4)  # NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage, nid.hIcon, nid.szTip = self.WM_TRAY, self.hicon, self.tip[:127]
        return bool(self.s32.Shell_NotifyIconW(0, self.c.byref(nid)))  # NIM_ADD

    def set_tip(self, tip: str) -> None:
        self.tip = tip
        if self.hwnd:
            nid = self._nid(0x4)
            nid.szTip = tip[:127]
            self.s32.Shell_NotifyIconW(1, self.c.byref(nid))  # NIM_MODIFY

    def notify(self, title: str, text: str) -> bool:
        """A Windows notification from the tray icon (shown as a toast on Windows 10 and 11)."""
        if not self.hwnd:
            return False
        nid = self._nid(0x10)  # NIF_INFO
        nid.szInfoTitle, nid.szInfo, nid.dwInfoFlags = title[:63], text[:255], 0x4  # NIIF_USER (our icon)
        return bool(self.s32.Shell_NotifyIconW(1, self.c.byref(nid)))

    def remove(self) -> None:
        if self.hwnd:
            self.s32.Shell_NotifyIconW(2, self.c.byref(self._nid(0)))  # NIM_DELETE

    def _show_menu(self) -> None:
        u = self.u32
        items = self.menu()
        h = u.CreatePopupMenu()
        actions = {}
        for n, item in enumerate(items, start=1):
            if item is None:
                u.AppendMenuW(h, 0x800, 0, None)  # MF_SEPARATOR
                continue
            label, fn = item
            u.AppendMenuW(h, 0 if fn else 0x1, n, label)  # MF_STRING, or MF_GRAYED for a heading
            actions[n] = fn
        pt = self.POINT()
        u.GetCursorPos(self.c.byref(pt))
        u.SetForegroundWindow(self.hwnd)
        cmd = u.TrackPopupMenu(h, 0x0100 | 0x0002 | 0x0020, pt.x, pt.y, 0, self.hwnd, None)  # RETURNCMD|RIGHTBUTTON|BOTTOMALIGN
        u.PostMessageW(self.hwnd, 0, 0, 0)
        u.DestroyMenu(h)
        fn = actions.get(cmd)
        if fn:
            threading.Thread(target=fn, daemon=True).start()

    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == self.WM_TRAY:
            ev = lparam & 0xFFFF
            if ev == 0x0202:  # WM_LBUTTONUP
                threading.Thread(target=self.on_click, daemon=True).start()
            elif ev == 0x0205:  # WM_RBUTTONUP
                self._show_menu()
            return 0
        if msg == self.taskbar_created:  # Explorer restarted: put the icon back
            self._add()
            return 0
        return self.u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self) -> None:
        try:
            self._setup()
            u = self.u32
            self._wndproc = self.WNDPROC(self._proc)  # keep a reference, or it's garbage-collected
            wc = self.WNDCLASSW()
            wc.lpfnWndProc, wc.hInstance, wc.lpszClassName = self._wndproc, self.hinst, "ShelfripTray"
            u.RegisterClassW(self.c.byref(wc))
            self.taskbar_created = u.RegisterWindowMessageW("TaskbarCreated")
            self.hwnd = u.CreateWindowExW(0, "ShelfripTray", "Shelfrip", 0, 0, 0, 0, 0, None, None, self.hinst, None)
            try:
                icon = ICON_PATH if ICON_PATH.exists() else write_icon()
                px = u.GetSystemMetrics(49) or 16  # SM_CXSMICON: the tray's icon size at this display scale
                self.hicon = u.LoadImageW(None, str(icon), 1, px, px, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
            except OSError:
                self.hicon = None
            if not self.hicon:
                self.hicon = u.LoadIconW(None, self.c.c_void_p(32512))  # IDI_APPLICATION
            self.ok = bool(self.hwnd) and self._add()
        except Exception as exc:
            print(f"(no tray icon: {exc})")
            self.ok = False
        finally:
            self._ready.set()
        if not self.ok:
            return
        msg = self.MSG()
        while u.GetMessageW(self.c.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(self.c.byref(msg))
            u.DispatchMessageW(self.c.byref(msg))


# --------------------------------------------------------------------------- pickers

def _raise_when_shown(thread_id: int, stop: threading.Event) -> None:
    """Bring the picker to the front once it opens. Shelfrip's own process isn't the foreground app
    (the window is Edge's), so Windows would otherwise open the dialog behind it, where it looks
    like Browse did nothing. Borrowing the foreground window's input lets us hand focus over."""
    import ctypes
    u32 = ctypes.WinDLL("user32")
    k32 = ctypes.WinDLL("kernel32")
    PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    u32.EnumThreadWindows.argtypes = [ctypes.c_uint32, PROC, ctypes.c_void_p]
    u32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    u32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    u32.GetForegroundWindow.restype = ctypes.c_void_p
    u32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    u32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    for _ in range(200):  # up to 10 seconds
        if stop.wait(0.05):
            return
        found = []

        def each(hwnd, _):
            cls = ctypes.create_unicode_buffer(32)
            u32.GetClassNameW(hwnd, cls, 32)
            if cls.value == "#32770" and u32.IsWindowVisible(hwnd):  # a dialog box
                found.append(hwnd)
            return True
        u32.EnumThreadWindows(thread_id, PROC(each), None)
        if found:
            me = k32.GetCurrentThreadId()
            fg = u32.GetForegroundWindow()
            other = u32.GetWindowThreadProcessId(fg, None) if fg else 0
            if other and other != me:
                u32.AttachThreadInput(me, other, True)
            u32.BringWindowToTop(found[0])
            u32.SetForegroundWindow(found[0])
            if other and other != me:
                u32.AttachThreadInput(me, other, False)
            return


def _pick_native(kind: str, start: str, title: str) -> str:
    """Windows' folder or .exe picker, opened from this process and owned by Shelfrip's window."""
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.WinDLL("user32")
    k32 = ctypes.WinDLL("kernel32")
    ole32 = ctypes.WinDLL("ole32")
    owners = _shelfrip_windows()
    owner = owners[0] if owners else None
    stop = threading.Event()
    threading.Thread(target=_raise_when_shown, args=(k32.GetCurrentThreadId(), stop), daemon=True).start()
    ole32.OleInitialize(None)  # the folder picker's "new style" needs OLE on this thread
    try:
        if kind == "file":
            comdlg = ctypes.WinDLL("comdlg32")

            class OFN(ctypes.Structure):
                _fields_ = [("lStructSize", wintypes.DWORD), ("hwndOwner", ctypes.c_void_p), ("hInstance", ctypes.c_void_p),
                            ("lpstrFilter", ctypes.c_void_p), ("lpstrCustomFilter", ctypes.c_void_p),
                            ("nMaxCustFilter", wintypes.DWORD), ("nFilterIndex", wintypes.DWORD),
                            ("lpstrFile", ctypes.c_void_p), ("nMaxFile", wintypes.DWORD), ("lpstrFileTitle", ctypes.c_void_p),
                            ("nMaxFileTitle", wintypes.DWORD), ("lpstrInitialDir", ctypes.c_wchar_p),
                            ("lpstrTitle", ctypes.c_wchar_p), ("Flags", wintypes.DWORD), ("nFileOffset", wintypes.WORD),
                            ("nFileExtension", wintypes.WORD), ("lpstrDefExt", ctypes.c_wchar_p), ("lCustData", ctypes.c_void_p),
                            ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", ctypes.c_void_p), ("pvReserved", ctypes.c_void_p),
                            ("dwReserved", wintypes.DWORD), ("FlagsEx", wintypes.DWORD)]
            filt = "Programs (*.exe)\0*.exe\0\0"
            filt_buf = (ctypes.c_wchar * len(filt))(*filt)
            out = ctypes.create_unicode_buffer(32768)
            folder = os.path.dirname(start) if start else ""
            o = OFN()
            o.lStructSize = ctypes.sizeof(OFN)
            o.hwndOwner = owner
            o.lpstrFilter = ctypes.cast(filt_buf, ctypes.c_void_p)
            o.nFilterIndex = 1
            o.lpstrFile = ctypes.cast(out, ctypes.c_void_p)
            o.nMaxFile = 32768
            o.lpstrInitialDir = folder if folder and os.path.isdir(folder) else None
            o.lpstrTitle = title or "Choose the program"
            o.Flags = 0x80000 | 0x1000 | 0x800 | 0x8  # OFN_EXPLORER | FILEMUSTEXIST | PATHMUSTEXIST | NOCHANGEDIR
            comdlg.GetOpenFileNameW.argtypes = [ctypes.c_void_p]
            if comdlg.GetOpenFileNameW(ctypes.byref(o)):
                return out.value
            err = comdlg.CommDlgExtendedError()
            if err:
                raise OSError(f"the file picker failed (error {err:#x})")
            return ""
        shell32 = ctypes.WinDLL("shell32")
        CALLBACK = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p)

        class BROWSEINFO(ctypes.Structure):
            _fields_ = [("hwndOwner", ctypes.c_void_p), ("pidlRoot", ctypes.c_void_p), ("pszDisplayName", ctypes.c_void_p),
                        ("lpszTitle", ctypes.c_wchar_p), ("ulFlags", ctypes.c_uint), ("lpfn", CALLBACK),
                        ("lParam", ctypes.c_void_p), ("iImage", ctypes.c_int)]
        u32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        here = start.rstrip("\\/")
        first = ctypes.create_unicode_buffer(here + "\\" if here.endswith(":") else here) if here else None

        def on_event(hwnd, msg, lparam, data):
            if msg == 1 and first is not None and os.path.isdir(first.value):  # BFFM_INITIALIZED: open at the current folder
                u32.SendMessageW(hwnd, 0x467, ctypes.c_void_p(1), ctypes.cast(first, ctypes.c_void_p))  # BFFM_SETSELECTIONW
            return 0
        cb = CALLBACK(on_event)
        name = ctypes.create_unicode_buffer(260)
        bi = BROWSEINFO()
        bi.hwndOwner = owner
        bi.pszDisplayName = ctypes.cast(name, ctypes.c_void_p)
        bi.lpszTitle = title or "Choose a folder"
        bi.ulFlags = 0x1 | 0x10 | 0x40  # BIF_RETURNONLYFSDIRS | BIF_EDITBOX | BIF_NEWDIALOGSTYLE (has "Make New Folder")
        bi.lpfn = cb
        shell32.SHBrowseForFolderW.argtypes = [ctypes.c_void_p]
        shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
        shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return ""
        try:
            path = ctypes.create_unicode_buffer(32768)
            if not shell32.SHGetPathFromIDListW(pidl, path):
                raise OSError("that isn't a folder on disk (pick a drive or folder, not a library or 'This PC')")
            return path.value
        finally:
            ole32.CoTaskMemFree(pidl)
    finally:
        stop.set()
        ole32.OleUninitialize()


def _pick_powershell(kind: str, start: str, title: str) -> str:
    """The same pickers through PowerShell, if opening them directly failed."""
    esc = lambda x: str(x or "").replace("'", "''")
    if kind == "file":
        ps = ("Add-Type -AssemblyName System.Windows.Forms;"
              "$d = New-Object System.Windows.Forms.OpenFileDialog;"
              f"$d.Title = '{esc(title) or 'Choose the program'}'; $d.Filter = 'Programs (*.exe)|*.exe';"
              f"if ('{esc(start)}') {{ $d.InitialDirectory = Split-Path -Parent '{esc(start)}' }};")
    else:
        ps = ("Add-Type -AssemblyName System.Windows.Forms;"
              "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
              f"$d.Description = '{esc(title) or 'Choose a folder'}'; $d.ShowNewFolderButton = $true;"
              f"if ('{esc(start)}') {{ $d.SelectedPath = '{esc(start)}' }};")
    ps += ("$o = New-Object System.Windows.Forms.Form; $o.TopMost = $true;"
           "if ($d.ShowDialog($o) -eq 'OK') { [Console]::OutputEncoding = [Text.Encoding]::UTF8;"
           + ("$d.FileName" if kind == "file" else "$d.SelectedPath") + " }")
    r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True,
                       stdin=subprocess.DEVNULL, timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    err = r.stderr.decode("utf-8", "replace").strip()
    if r.returncode and err:
        raise OSError(err.splitlines()[0][:300])
    return r.stdout.decode("utf-8", "replace").strip()


def pick(kind: str = "folder", start: str = "", title: str = "") -> tuple[str | None, str]:
    """Windows' own folder (or .exe file) picker, for the Settings page.
    Returns (path, error): path is '' if cancelled, None if there's no picker (error says why)."""
    if sys.platform.startswith("linux"):
        return _pick_linux(kind, start, title)
    if sys.platform == "darwin":
        import macos_desktop
        return macos_desktop.pick_path(kind, start, title)
    if os.name != "nt":
        return None, "There's no folder picker on this computer."
    errors = []
    for how in (_pick_native, _pick_powershell):
        try:
            return how(kind, start, title), ""
        except Exception as exc:
            errors.append(f"{how.__name__.strip('_')}: {exc}")
            print(f"(folder picker: {how.__name__} failed: {exc})")
    return None, "Couldn't open the folder picker (" + "; ".join(errors) + "). Type the path instead."
