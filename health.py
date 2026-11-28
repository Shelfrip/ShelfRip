r"""
health.py - noticing when something's wrong before you do.

  leftover_makemkv()   MakeMKV processes still running from before Shelfrip started (a window
                       closed mid-rip on an older version). They hold the drives, so new rips
                       sit on "scanning". The Home tab offers to stop them.
  Watchdog             a drive that's been "scanning" for 30 minutes, or whose rip hasn't
                       grown for 20, is marked stuck, with Stop and Eject buttons.
  makemkv_check()      is MakeMKV's key still good? (the free beta key expires every few months)
  tmdb_check()         does the TMDB key (yours or the shared one) actually work?
"""

from __future__ import annotations

import csv
import io
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import paths

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NAMES = ("makemkvcon.exe", "makemkvcon64.exe")


# --------------------------------------------------------------------------- leftover MakeMKV

def _makemkv_pids() -> list[int]:
    if os.name != "nt":
        return []
    pids = []
    for name in NAMES:
        try:
            out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=20, creationflags=NOWIN).stdout
        except Exception:
            continue
        for row in csv.reader(io.StringIO(out)):
            if len(row) > 1 and row[0].lower() == name and row[1].isdigit():
                pids.append(int(row[1]))
    return pids


def makemkv_window_open() -> bool:
    """Is MakeMKV's own window (makemkv.exe) open? It can hold on to the drives it shows, and two
    programs reading one drive make rips crawl, or fail as if the disc were scratched."""
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq makemkv.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=20, creationflags=NOWIN).stdout
    except Exception:
        return False
    return any(len(row) > 1 and row[0].lower() == "makemkv.exe" for row in csv.reader(io.StringIO(out)))


LEFTOVER: list[int] = []


def find_leftovers() -> list[int]:
    """Run once at start, before Shelfrip starts any rip of its own: every MakeMKV running now is
    left over from before."""
    LEFTOVER[:] = _makemkv_pids()
    return LEFTOVER


def leftovers() -> list[int]:
    """The leftover MakeMKV processes still running."""
    if not LEFTOVER:
        return []
    alive = set(_makemkv_pids())
    LEFTOVER[:] = [p for p in LEFTOVER if p in alive]
    return list(LEFTOVER)


def stop_leftovers() -> int:
    n = 0
    alive = set(_makemkv_pids())
    for pid in list(LEFTOVER):
        if pid in alive:  # still a MakeMKV process (not a reused process id)
            r = subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, creationflags=NOWIN)
            n += r.returncode == 0
    LEFTOVER.clear()
    return n


# --------------------------------------------------------------------------- stuck rips

SCAN_LIMIT = 30 * 60   # reading a disc's titles normally takes a minute or two
STALL_LIMIT = 20 * 60  # no new data for this long while ripping


class Watchdog:
    """Call check() every minute or so with the drives that are ripping."""

    def __init__(self, notify=None) -> None:
        self.seen: dict[str, tuple] = {}   # device -> (what it looked like, since when)
        self.told: set[str] = set()
        self.notify = notify

    @staticmethod
    def _bytes(folder: Path | None) -> int:
        if not folder or not folder.is_dir():
            return 0
        total = 0
        for f in [*folder.glob("*.mkv"), *folder.glob("rip/*")]:
            try:
                total += f.stat().st_size
            except OSError:
                pass
        return total

    def check(self, busy: set[str], status: dict, rip_root: Path) -> None:
        now = time.time()
        for dev in list(self.seen):
            if dev not in busy:
                self.seen.pop(dev, None)
                self.told.discard(dev)
                status.get(dev, {}).pop("stuck", None)
        for dev in busy:
            st = status.setdefault(dev, {})
            state = st.get("state")
            folder = rip_root / st["folder"] if st.get("folder") else None
            look = (state, st.get("pct"), st.get("message"), self._bytes(folder))
            prev = self.seen.get(dev)
            if not prev or prev[0] != look:
                self.seen[dev] = (look, now)
                st.pop("stuck", None)
                continue
            quiet = now - prev[1]
            limit = SCAN_LIMIT if state == "scanning" else STALL_LIMIT
            if state in ("scanning", "ripping") and quiet >= limit:
                mins = int(quiet // 60)
                st["stuck"] = (f"Nothing has happened for {mins} minutes. The disc may be dirty or scratched, "
                               "or another program is using the drive." if state == "ripping" else
                               f"Still reading the disc after {mins} minutes. The disc may be dirty, "
                               "or another program is using the drive.")
                if dev not in self.told and self.notify:
                    self.told.add(dev)
                    self.notify(f"{dev} seems stuck", f"{st.get('label') or 'The disc'}: {st['stuck']}")


# --------------------------------------------------------------------------- live setup checks

_CACHE: dict = {}
_LOCK = threading.Lock()


def _cached(key: str, ttl: float, fn):
    """fn() at most once per ttl seconds; runs in the background so the page never waits."""
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0] < ttl or hit[2]):
        return hit[1]

    def run():
        try:
            val = fn()
        except Exception as exc:
            val = {"ok": None, "detail": str(exc)}
        _CACHE[key] = (time.time(), val, False)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[2]:
            return hit[1]
        _CACHE[key] = (hit[0] if hit else 0, hit[1] if hit else None, True)
    threading.Thread(target=run, daemon=True).start()
    return hit[1] if hit else None


def forget(key: str) -> None:
    _CACHE.pop(key, None)


def makemkv_check(makemkvcon: str) -> dict | None:
    """{"ok": True/False, "detail": ...} or None while it's still being checked."""
    def run():
        r = subprocess.run([makemkvcon, "-r", "info", "disc:9999"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60, creationflags=NOWIN)
        out = (r.stdout or "") + (r.stderr or "")
        low = out.lower()
        if "expired" in low:
            return {"ok": False, "detail": "MakeMKV's key has expired. Get the current free beta key from the "
                                           "MakeMKV forum and enter it in MakeMKV (Help, Register).",
                    "link": "https://forum.makemkv.com/forum/viewtopic.php?t=1053"}
        if "too old" in low:
            return {"ok": False, "detail": "This version of MakeMKV is too old to run. Install the latest one.",
                    "link": "https://www.makemkv.com/download/"}
        return {"ok": True, "detail": ""}
    return _cached("makemkv:" + makemkvcon, 3600, run)


def tmdb_check(key: str, wait: float = 0, base: str = "") -> dict | None:
    """Your own key (key), or the Shelfrip server's shared one (base = its /v1/tmdb address, key "")."""
    def run():
        key_ = key.strip()
        headers = {"Accept": "application/json", "User-Agent": paths.USER_AGENT}  # Cloudflare (the shared key) refuses Python's default agent
        url = (base.rstrip("/") or "https://api.themoviedb.org/3") + "/configuration"
        if base:
            pass
        elif key_.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {key_}"
        else:
            url += "?api_key=" + urllib.request.quote(key_)
        try:
            urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15).close()
            return {"ok": True, "detail": "using Shelfrip's shared key" if base else "working"}
        except urllib.error.HTTPError as exc:
            if base and exc.code == 503:
                return {"ok": False, "detail": "Shelfrip's shared TMDB key isn't available. Add your own free key."}
            if base and exc.code == 429:
                return {"ok": None, "detail": "using Shelfrip's shared key, which is busy right now. Your own free key avoids that."}
            if exc.code == 401:
                return {"ok": False, "detail": "TMDB says this key isn't valid. Copy the \"API Key\" (or the "
                                               "\"API Read Access Token\") from your TMDB account again."}
            return {"ok": None, "detail": f"couldn't check it right now (TMDB answered {exc.code})"}
        except Exception:
            return {"ok": None, "detail": "couldn't reach TMDB to check it (no internet?)"}
    ck = "tmdb:" + (base or key[-8:])
    res = _cached(ck, 3600, run)
    end = time.time() + wait
    while res is None and time.time() < end:  # the first check of a new key: wait for the answer
        time.sleep(0.25)
        hit = _CACHE.get(ck)
        res = hit[1] if hit and not hit[2] else None
    return res


# --------------------------------------------------------------------------- backups

BACKUP_DIR_NAME = "backups"
DATA_FILES = ("config.json", "disc_catalog.db", "learned_labels.json", "shared.json", "recap.json")


def backup(data_dir: Path, rip_root: Path | None, keep: int = 7) -> Path | None:
    """Once a day: zip your own data (settings, disc database, learned labels) and the records of
    every disc you've filed, into <data folder>/backups. The newest `keep` are kept. Returns the
    new backup, or None if today's is already there."""
    import datetime as dt
    import sqlite3
    import tempfile
    import zipfile
    out_dir = data_dir / BACKUP_DIR_NAME
    paths.make_folder(out_dir, "the backup folder")
    dst = out_dir / f"Shelfrip backup {dt.date.today().isoformat()}.zip"
    if dst.exists():
        return None
    tmp = dst.with_suffix(".partial")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name in DATA_FILES:
            f = data_dir / name
            if not f.exists():
                continue
            if f.suffix == ".db":  # copy through SQLite, so a write in progress can't give a broken copy
                with tempfile.TemporaryDirectory() as td:
                    copy = Path(td) / name
                    src, out = sqlite3.connect(str(f)), sqlite3.connect(str(copy))
                    try:
                        src.backup(out)
                    finally:
                        out.close()
                        src.close()
                    z.write(copy, name)
            else:
                z.write(f, name)
        filed = (rip_root / "_Filed") if rip_root else None
        if filed and filed.is_dir():
            for f in filed.rglob("*"):
                if f.is_file() and (f.suffix.lower() == ".json" or f.name == "makemkv_info.txt"):
                    z.write(f, "_Filed/" + f.relative_to(filed).as_posix())
    os.replace(tmp, dst)
    for old in sorted(out_dir.glob("Shelfrip backup *.zip"))[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass
    return dst
