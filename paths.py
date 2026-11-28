r"""
paths.py - where Shelfrip keeps its files.

  APP_DIR   the program: the .py files, review.html and tools\ (next to Shelfrip.exe).
  DATA_DIR  your own files: config.json, disc_catalog.db, learned_labels.json and the
            .cache folder. %APPDATA%\Shelfrip on Windows, so the program folder can be
            replaced by an update (or live in Program Files) without losing anything.

Put an empty file called portable.txt next to the program to keep your files in the
program folder instead (for a copy on a USB stick). SHELFRIP_DATA overrides both.

First start after updating from a version that kept config.json next to the scripts:
the files are copied (not moved) to DATA_DIR, and the old copies are left where they were.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

APP_NAME = "Shelfrip"
APP_VERSION = "1.2.0"
# Shown to the web services the app talks to (MusicBrainz asks every app for one).
APP_URL = "https://github.com/Shelfrip/ShelfRip"
USER_AGENT = f"Shelfrip/{APP_VERSION} ( {APP_URL} )"
# GitHub repository ("owner/name") whose Releases hold new versions. Empty = don't check for updates.
UPDATE_REPO = "Shelfrip/ShelfRip"
# Where the Donate button goes (empty hides it)
DONATE_URL = "https://ko-fi.com/shelfrip"
# The shared Shelfrip disc database (server/ in the repository). Empty = off. "community_url" in config.json overrides it.
COMMUNITY_URL = "https://shelfrip-db.shelfrip.workers.dev"

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def _installed_data_dir() -> Path:
    """Where Shelfrip keeps your files when it isn't portable (see _data_dir)."""
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")) / "shelfrip"


def _data_dir() -> Path:
    if os.environ.get("SHELFRIP_DATA") or os.environ.get("DISC_RIPPER_DATA"):
        return Path(os.environ.get("SHELFRIP_DATA") or os.environ["DISC_RIPPER_DATA"]).expanduser()
    if (APP_DIR / "portable.txt").exists():
        return APP_DIR
    return _installed_data_dir()


DATA_DIR = _data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
CACHE_DIR = DATA_DIR / ".cache"

# Several parts of the app write config.json (Settings, the setup wizard's autosave, the shared
# database's install id, filled-in defaults). They all go through write_config under this lock,
# each with its own temp file, so two saves at once can't lose each other's changes.
CONFIG_LOCK = threading.RLock()


def write_config(cfg: dict) -> None:
    """Save the whole settings dict to config.json atomically (temp file, then rename).

    Retries briefly when Windows reports the file busy (antivirus, backup tools), and raises
    OSError if it still can't be replaced. Callers that read-modify-write hold CONFIG_LOCK.
    """
    with CONFIG_LOCK:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_name(f"config.{os.getpid()}.{threading.get_ident()}.tmp")
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            if os.name != "nt":
                os.fchmod(f.fileno(), 0o600)  # also tighten an existing temp file before writing secrets
            f.write(json.dumps(cfg, indent=2))
        for attempt in range(5):
            try:
                tmp.replace(CONFIG_PATH)
                return
            except PermissionError:
                if attempt == 4:
                    tmp.unlink(missing_ok=True)
                    raise
                time.sleep(0.2)

# Files an older version kept next to the scripts.
_OLD_FILES = ("config.json", "disc_catalog.db", "learned_labels.json")

# Settings whose defaults changed when the app became shareable. A config made before then
# didn't store them, because the old defaults were right for it: write the old values in,
# so an existing setup keeps behaving exactly as it did.
_OLD_DEFAULTS = {
    "media_server": "plex",
    "format_folders": True,
    "movie_format_folders": {"4k": "4K Blu-Ray", "bluray": "Blu-Ray", "dvd": "DVD"},
    "tv_format_folders": {"4k": "4K Blu Ray", "bluray": "Blu-Ray", "dvd": "DVD"},
    "use_dvdcompare": True,
}


def _old_data_dir() -> Path | None:
    """Where the app kept your files while it was called Disc Ripper (before 2026-11-18)."""
    if sys.platform == "darwin" or os.environ.get("DISC_RIPPER_DATA") or DATA_DIR == APP_DIR:
        return None
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")) / "Disc Ripper"
    return Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")) / "disc-ripper"


# Not worth carrying into a portable copy: logs, problem reports and settings backups.
_PORTABLE_SKIP = shutil.ignore_patterns("*.log", "*.tmp", "reports", "backups")


def _copy_into_portable() -> str:
    """Portable zips before 1.2.0 had no portable.txt, so a portable copy kept its files in
    %APPDATA%\\Shelfrip like the installed app. The first time a portable copy finds no settings
    of its own, it starts from those (copied, so the installed app keeps its own)."""
    if DATA_DIR != APP_DIR or os.environ.get("SHELFRIP_DATA") or os.environ.get("DISC_RIPPER_DATA"):
        return ""
    src = _installed_data_dir()
    if CONFIG_PATH.exists() or src.resolve() == APP_DIR or not (src / "config.json").is_file():
        return ""
    shutil.copytree(src, DATA_DIR, dirs_exist_ok=True, ignore=_PORTABLE_SKIP)
    return f"This copy is portable now, so it keeps its own settings here. It started from your settings in {src}."


def migrate() -> str:
    """Copy an older version's files into DATA_DIR once. Returns a line for the log, or ''."""
    note = _copy_into_portable()
    if note:
        return note
    old = _old_data_dir()
    if old and not CONFIG_PATH.exists() and (old / "config.json").exists():
        shutil.copytree(old, DATA_DIR, dirs_exist_ok=True)
        return f"Copied your settings from {old} to {DATA_DIR} (the app is called Shelfrip now)."
    if DATA_DIR == APP_DIR or CONFIG_PATH.exists() or not (APP_DIR / "config.json").exists():
        return ""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name in _OLD_FILES:
        src = APP_DIR / name
        if src.exists():
            shutil.copy2(src, DATA_DIR / name)
    if (APP_DIR / ".cache").is_dir() and not CACHE_DIR.exists():
        shutil.copytree(APP_DIR / ".cache", CACHE_DIR)
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        for k, v in _OLD_DEFAULTS.items():
            cfg.setdefault(k, v)
        cfg["migrated_from"] = str(APP_DIR)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        pass
    return f"Moved your settings and disc database to {DATA_DIR} (the old copies in {APP_DIR} are no longer used)."


OLD_COPIES = "Old settings (not used)"


def _merge_discs(old_db: Path, new_db: Path, missing: set) -> None:
    """Copy the discs (and their titles) that only the old disc database has into the current one."""
    import sqlite3
    src, dst = sqlite3.connect(f"file:{old_db}?mode=ro", uri=True), sqlite3.connect(str(new_db))
    try:
        dcols = [r[1] for r in dst.execute("PRAGMA table_info(discs)") if r[1] != "id"]
        tcols = [r[1] for r in dst.execute("PRAGMA table_info(titles)") if r[1] != "disc_id"]
        with dst:
            for row in src.execute(f"SELECT id, disc_label, tmdb_id, {', '.join(dcols)} FROM discs"):
                if ((row[1] or ""), (row[2] or "")) not in missing:
                    continue
                cur = dst.execute(f"INSERT INTO discs ({', '.join(dcols)}) VALUES ({', '.join('?' * len(dcols))})", row[3:])
                for t in src.execute(f"SELECT {', '.join(tcols)} FROM titles WHERE disc_id = ?", (row[0],)):
                    dst.execute(f"INSERT INTO titles (disc_id, {', '.join(tcols)}) VALUES (?, {', '.join('?' * len(tcols))})",
                                (cur.lastrowid, *t))
    finally:
        src.close()
        dst.close()


def retire_old_copies() -> str:
    """Once your files live in DATA_DIR, the copies an older version left next to the program are
    stale: two sets of settings is confusing, and editing the wrong one does nothing. Learned
    labels only the old copy has are merged in first, then the old files are moved into a folder
    called "Old settings (not used)" (moved, not deleted). The old disc database is left where it
    is if it holds a disc the current one doesn't."""
    if DATA_DIR == APP_DIR or not CONFIG_PATH.exists() or not (APP_DIR / "config.json").exists():
        return ""
    import sqlite3
    old_db, new_db = APP_DIR / "disc_catalog.db", DATA_DIR / "disc_catalog.db"
    keep_db = False
    if old_db.exists():
        def discs(f):
            c = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
            try:
                return {(r[0] or "", r[1] or "") for r in c.execute("SELECT disc_label, tmdb_id FROM discs")}
            finally:
                c.close()
        try:
            missing = discs(old_db) - discs(new_db) if new_db.exists() else {None}
            if missing and new_db.exists():  # filed while the old copy was in use: add them to the current one
                _merge_discs(old_db, new_db, missing)
                missing = discs(old_db) - discs(new_db)
            keep_db = bool(missing)
        except sqlite3.Error:
            keep_db = True
    old_lab, new_lab = APP_DIR / "learned_labels.json", DATA_DIR / "learned_labels.json"
    if old_lab.exists():
        try:
            old = json.loads(old_lab.read_text(encoding="utf-8"))
            new = json.loads(new_lab.read_text(encoding="utf-8")) if new_lab.exists() else {}
            extra = {k: v for k, v in old.items() if k not in new}
            if extra:
                new.update(extra)
                tmp = new_lab.with_suffix(".tmp")
                tmp.write_text(json.dumps(new, indent=2), encoding="utf-8")
                os.replace(tmp, new_lab)
        except (OSError, ValueError):
            return ""  # leave everything as it is rather than lose a label
    dest = APP_DIR / OLD_COPIES
    moved = []
    for f in sorted(APP_DIR.iterdir()):
        stem = f.name.split(".")[0]
        if f.is_file() and stem in ("config", "disc_catalog", "learned_labels") and f.suffix in (".json", ".db"):
            if stem == "disc_catalog" and keep_db:
                continue
            dest.mkdir(exist_ok=True)
            shutil.move(str(f), str(dest / f.name))
            moved.append(f.name)
    if (APP_DIR / ".cache").is_dir() and CACHE_DIR.is_dir():
        dest.mkdir(exist_ok=True)
        shutil.move(str(APP_DIR / ".cache"), str(dest / ".cache"))
        moved.append(".cache")
    if not moved:
        return ""
    (dest / "README.txt").write_text(
        f"Shelfrip keeps your settings, disc database and learned labels in {DATA_DIR}.\n"
        "These are older copies from before it moved them there, and nothing uses them any more.\n"
        "You can delete this folder.\n", encoding="utf-8")
    return (f"Moved the old, unused copies of your settings out of {APP_DIR} into \"{OLD_COPIES}\" "
            f"(the ones in use are in {DATA_DIR})." + (" The old disc database was left, as it has discs the "
                                                       "current one doesn't." if keep_db else ""))


MIGRATION_NOTE = ""
try:
    MIGRATION_NOTE = migrate()
except OSError as exc:
    MIGRATION_NOTE = f"Couldn't copy your settings to {DATA_DIR}: {exc}"
if not MIGRATION_NOTE:
    try:
        MIGRATION_NOTE = retire_old_copies()
    except OSError as exc:
        MIGRATION_NOTE = f"Couldn't tidy the old copies of your settings in {APP_DIR}: {exc}"
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass


# --------------------------------------------------------------------------- folders

def folder_problem(path, exc: BaseException, what: str = "the folder") -> str:
    """A sentence saying which folder couldn't be made and why, in plain words."""
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    if isinstance(exc, PermissionError) or code in (5, 13):
        why = ("Windows refused access. If Windows Security showed \"Unauthorized changes blocked\", its Controlled folder "
               "access is protecting that folder: allow Shelfrip (and MakeMKV) under Virus & threat protection > "
               "Ransomware protection, or choose a folder outside Documents, Videos and Desktop")
        if sys.platform == "darwin":
            why = ("macOS refused access. Check this folder's permissions and allow the app or terminal "
                   "access under System Settings > Privacy & Security > Files and Folders, or choose another folder")
    elif code in (21,):
        why = "the drive isn't ready (is it a disc drive, or a USB drive that was unplugged?)"
    elif code in (53, 67, 64, 59, 1219, 1326):
        why = "the network location can't be reached (is the NAS on, and are you signed in to it?)"
    elif code in (3, 2) or isinstance(exc, FileNotFoundError):
        why = "that drive or the folder above it doesn't exist"
    elif code in (123, 161, 22):
        why = "that isn't a valid folder path"
    elif code in (112, 28):
        why = "the drive is full"
    else:
        why = getattr(exc, "strerror", None) or str(exc)
    return f"Couldn't create {what} {path}: {why}."


def make_folder(path, what: str = "the folder") -> Path:
    """mkdir -p, but a failure says which folder and why (OSError with a readable message)."""
    p = Path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(folder_problem(p, exc, what)) from exc
    return p
