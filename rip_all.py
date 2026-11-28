r"""
rip_all.py - Step 1 of the Plex disc pipeline.

Rips every disc currently loaded in your drives AT THE SAME TIME using
MakeMKV's command-line tool (makemkvcon). For each disc it:

  * creates  <output_root>\<DISC_LABEL>__<YYYYMMDD-HHMMSS>\
  * saves    disc_info.json   (titles, run times, chapters, sizes, source playlists)
             makemkv_info.txt (raw MakeMKV scan, kept for troubleshooting)
             rip.log          (raw MakeMKV rip output)
  * rips     every title at least `min_length_seconds` long to .mkv (no transcoding);
             with "skip_duplicate_features" on, duplicate copies (of a feature or an episode),
             scene-by-scene titles (the movie cut into pieces) and TV "Play All" titles whose
             episodes are on the disc too are left out (dupes.py)
  * ejects   the disc when it's finished
  * appends  a line to <output_root>\rip_history.csv

Step 2 (naming/splitting) reads disc_info.json, so don't delete it.

Usage (or just double-click "Rip Discs.bat"):
    python rip_all.py            rip whatever is loaded now, then stop
    python rip_all.py --watch    keep running: rip each new disc as soon as you insert it
    python rip_all.py --list     only show which drives have discs
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import shutil
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

# next to the script, or next to Shelfrip.exe when built with PyInstaller
HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
import paths
CONFIG_PATH = paths.CONFIG_PATH

DEFAULT_CONFIG = {
    # Where MakeMKV is installed. 64-bit is used if it exists.
    "makemkvcon_path": r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe",
    "output_root": "",   # chosen in the setup step (Settings)
    # Titles shorter than this are skipped (menus, logos, FBI warnings).
    # 30s keeps short extras like trailers; raise it if you get junk.
    "min_length_seconds": 30,
    # Eject right after ripping. Off by default: the app ejects once you've filed the disc,
    # so you can still look at the disc while reviewing names.
    "eject_when_done": False,
    "eject_after_filing": True,
    # Optional: restrict to these drive letters, e.g. ["D:", "F:", "G:"]. Empty = all.
    "drives": [],
    # --watch mode: seconds between checks for newly inserted discs.
    "watch_poll_seconds": 15,
    # Optional: TheDiscDB's scan tool, to make an upload-ready disc manifest while the disc
    # is in the drive (download thediscdb-scan.exe from
    # https://github.com/TheDiscDb/optical-disc-manifest/releases?q=scan-cli )
    "thediscdb_scan_path": str(HERE / "tools" / "thediscdb-scan.exe"),
}

# A drive is "in use" while its lock file in output_root is fresh. The rip that
# owns it touches the file every LOCK_HEARTBEAT seconds; a lock older than
# LOCK_STALE (e.g. left by a crashed or closed window) is ignored.
LOCK_HEARTBEAT = 30
LOCK_STALE = 150

# MakeMKV attribute ids (from makemkvcon's robot-mode "-r" output)
AP_NAME = 2
AP_CHAPTER_COUNT = 8
AP_DURATION = 9
AP_DISK_SIZE = 10
AP_DISK_SIZE_BYTES = 11
AP_SOURCE_FILE = 16
AP_SEGMENTS_MAP = 26
AP_OUTPUT_FILE = 27
AP_LANG_CODE = 28
AP_VOLUME_NAME = 32
AP_STREAM_TYPE = 1
AP_CODEC_SHORT = 6
AP_RESOLUTION = 19

DRIVE_LOADED = 2  # DRV state value meaning "disc inserted and readable"

print_lock = threading.Lock()

# Live state for the Shelfrip app (app.py): recent log lines, what each drive is doing,
# the running MakeMKV processes (so a rip can be cancelled) and callbacks run after each rip.
from collections import deque
LOG_LINES: deque = deque(maxlen=300)
STATUS: dict[str, dict] = {}
PROCS: dict[str, subprocess.Popen] = {}
PICKS: dict[str, dict] = {}  # drives waiting for you to choose titles: {device: {"event", "ids"}}
ON_DONE: list = []


LOG_FILE: Path | None = None   # app.py sets this to <rip folder>\disc_ripper.log


def log(msg: str) -> None:
    line = f"[{dt.datetime.now():%H:%M:%S}] {msg}"
    with print_lock:
        LOG_LINES.append(line)
        if LOG_FILE:
            try:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(f"{dt.datetime.now():%Y-%m-%d} {line}\n")
            except OSError:
                pass
        try:
            print(line, flush=True)
        except (OSError, ValueError):  # no console (pythonw)
            pass


# --------------------------------------------------------------------------- config

class SetupError(Exception):
    pass


def load_config(exit_on_error: bool = True) -> dict:
    """Defaults overlaid with config.json, plus MakeMKV's location. Raises SetupError (or exits,
    when run from the command line) if there's no rip folder, no MakeMKV, or the file is unreadable."""
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            mine = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            # don't rip with default folders when the real settings are there but unreadable
            msg = f"Your settings file ({CONFIG_PATH}) couldn't be read: {exc}. Open Settings and save them again."
            if exit_on_error:
                sys.exit(msg)
            raise SetupError(msg)
        if isinstance(mine, dict):
            cfg.update(mine)
    else:
        paths.write_config(DEFAULT_CONFIG)
        log(f"Created default settings file: {CONFIG_PATH}")
    if not str(cfg.get("output_root") or "").strip():
        msg = "Choose a rip folder in Settings (where ripped discs are saved) before ripping."
        if exit_on_error:
            sys.exit(msg)
        raise SetupError(msg)
    exe = find_makemkv(cfg)
    if not exe:
        msg = (f"MakeMKV isn't installed (or isn't where the settings say). Download it from "
               f"{MAKEMKV_URL} and install it, or set its location in Settings.")
        if exit_on_error:
            sys.exit(msg)
        raise SetupError(msg)
    cfg["makemkvcon_path"] = exe
    return cfg


MAKEMKV_URL = "https://www.makemkv.com/download/"


def find_makemkv(cfg: dict) -> str | None:
    """makemkvcon: where the settings say, else the usual install folders, else on PATH.

    The setting can point at MakeMKV's own window program (makemkv.exe) or its folder by
    mistake. Running makemkv.exe opens the MakeMKV window instead of ripping (issue #39),
    so only a makemkvcon program counts: from a wrong file, its folder's makemkvcon is used."""
    cands = []
    if cfg.get("makemkvcon_path"):
        p = Path(str(cfg["makemkvcon_path"]).strip().strip('"'))
        folder = p if p.is_dir() else p.parent
        if not p.is_dir() and p.name.lower().startswith("makemkvcon"):
            cands.append(p)
        cands += [folder / "makemkvcon64.exe", folder / "makemkvcon.exe", folder / "makemkvcon"]
    for base in (r"C:\Program Files (x86)\MakeMKV", r"C:\Program Files\MakeMKV"):
        cands += [Path(base) / "makemkvcon64.exe", Path(base) / "makemkvcon.exe"]
    for c in cands:
        if c.is_file():
            return str(c)
    return shutil.which("makemkvcon64") or shutil.which("makemkvcon")


# --------------------------------------------------------------------------- parsing

def split_robot_line(line: str) -> tuple[str, list[str]]:
    """'TINFO:0,2,0,"Name, with comma"' -> ('TINFO', ['0','2','0','Name, with comma'])"""
    if ":" not in line:
        return "", []
    kind, rest = line.split(":", 1)
    fields = next(csv.reader([rest], skipinitialspace=False), [])
    return kind.strip(), fields


def parse_drives(output: str) -> list[dict]:
    """Drives from MakeMKV's robot output (DRV lines): device letter, drive name, disc label,
    and whether a disc is loaded."""
    drives = []
    for line in output.splitlines():
        kind, f = split_robot_line(line)
        if kind != "DRV" or len(f) < 7:
            continue
        index, state = int(f[0]), int(f[1])
        drive_name, disc_label, device = f[4], f[5], f[6]
        if not drive_name and not device:
            continue  # empty slot
        drives.append({
            "index": index,
            "loaded": state == DRIVE_LOADED,
            "drive_name": drive_name,
            "disc_label": disc_label,
            "device": device.rstrip("\\"),  # "D:" on Windows
        })
    return drives


def hms_to_seconds(s: str) -> int:
    parts = [int(p) for p in s.split(":") if p.isdigit()]
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def parse_disc_info(output: str) -> dict:
    """Titles, lengths, sizes, chapters, playlists and streams from `makemkvcon info` robot
    output (CINFO / TINFO / SINFO lines), as the dict saved in disc_info.json."""
    disc: dict = {"titles": {}}
    for line in output.splitlines():
        kind, f = split_robot_line(line)
        if kind == "CINFO" and len(f) >= 3:
            attr, value = int(f[0]), f[2]
            if attr == AP_NAME:
                disc["name"] = value
            elif attr == AP_VOLUME_NAME:
                disc["volume_label"] = value
        elif kind == "TINFO" and len(f) >= 4:
            tid, attr, value = int(f[0]), int(f[1]), f[3]
            t = disc["titles"].setdefault(tid, {"title_id": tid, "streams": []})
            if attr == AP_NAME:
                t["name"] = value
            elif attr == AP_CHAPTER_COUNT:
                t["chapters"] = int(value or 0)
            elif attr == AP_DURATION:
                t["duration"] = value
                t["duration_seconds"] = hms_to_seconds(value)
            elif attr == AP_DISK_SIZE:
                t["size"] = value
            elif attr == AP_DISK_SIZE_BYTES:
                t["size_bytes"] = int(value or 0)
            elif attr == AP_SOURCE_FILE:
                t["source_file"] = value  # e.g. 00800.mpls - useful for matching extras
            elif attr == AP_SEGMENTS_MAP:
                t["segments"] = value
            elif attr == AP_OUTPUT_FILE:
                t["output_file"] = value
        elif kind == "SINFO" and len(f) >= 5:
            tid, sid, attr, value = int(f[0]), int(f[1]), int(f[2]), f[4]
            t = disc["titles"].setdefault(tid, {"title_id": tid, "streams": []})
            while len(t["streams"]) <= sid:
                t["streams"].append({})
            s = t["streams"][sid]
            if attr == AP_STREAM_TYPE:
                s["type"] = value
            elif attr == 2:
                s["name"] = value          # e.g. "Surround 5.1", or a commentary title
            elif attr == 3:
                s["lang3"] = value         # e.g. "eng"
            elif attr == 4:
                s["lang_name"] = value
            elif attr == 13:
                s["bitrate"] = value
            elif attr == 14:
                s["channels"] = int(value or 0) if str(value).isdigit() else 0
            elif attr == 22:
                s["flags"] = int(value or 0) if str(value).isdigit() else 0   # 1/2 = director's comments
            elif attr == 30:
                s["desc"] = value          # e.g. "DTS-HD MA Surround 5.1 English"
            elif attr == AP_CODEC_SHORT:
                s["codec"] = value
            elif attr == AP_LANG_CODE:
                s["lang"] = value
            elif attr == AP_RESOLUTION:
                s["resolution"] = value
    disc["titles"] = [disc["titles"][k] for k in sorted(disc["titles"])]
    return disc


# --------------------------------------------------------------------------- makemkv calls

_JOB = None


def end_with_us(proc: subprocess.Popen) -> None:
    """Windows: make this MakeMKV process end when Shelfrip closes. Otherwise closing the window
    leaves makemkvcon ripping in the background, holding the drive, with nothing watching it."""
    global _JOB
    if os.name != "nt":
        return
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        if _JOB is None:
            class Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                            ("SchedulingClass", ctypes.c_uint32)]

            class Extended(ctypes.Structure):
                _fields_ = [("Basic", Basic), ("Io", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            job = k32.CreateJobObjectW(None, None)
            info = Extended()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                _JOB = 0
                return
            _JOB = job  # the handle stays open while we run; Windows closes it (and the job) when we exit
        if _JOB:
            k32.AssignProcessToJobObject(_JOB, ctypes.c_void_p(int(proc._handle)))  # type: ignore[attr-defined]
    except Exception:
        pass


SAVE_PHASES = {5024, 5017}  # PRGT "Saving all titles to MKV files" / PRGC "Saving to MKV file"


def run_makemkv(cfg: dict, args: list[str], logfile: Path | None = None,
                on_progress=None, device: str = "", on_phase=None) -> tuple[int, str]:
    """Run makemkvcon in robot mode. on_progress(fraction) gets the saving progress only: before it
    saves, every `mkv` run opens and analyses the whole disc again, and MakeMKV's progress bar runs
    through that too (that's how a rip could sit at 83% with nothing written). on_phase(name) gets
    what MakeMKV is doing meanwhile ("Opening Blu-ray disc", "Analyzing seamless segments", ...)."""
    cmd = [cfg["makemkvcon_path"], "-r", "--progress=-same"] + args
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    end_with_us(proc)
    if device:
        PROCS[device] = proc
    lines = []
    phase = {"seen": False, "saving": False}
    fh = open(logfile, "a", encoding="utf-8") if logfile else None
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip("\r\n")
            if line.startswith(("PRGT:", "PRGC:")):
                _, f = split_robot_line(line)
                try:
                    code = int(f[0])
                except (ValueError, IndexError):
                    code = 0
                name = f[2] if len(f) > 2 else ""
                if line.startswith("PRGT:"):
                    phase["seen"] = True
                    phase["saving"] = code in SAVE_PHASES or "saving" in name.lower()
                elif code in SAVE_PHASES:
                    phase["saving"] = True
                if on_phase:
                    on_phase("" if phase["saving"] else name)
            if line.startswith("PRGV:"):
                if on_progress and (phase["saving"] or not phase["seen"]):
                    _, f = split_robot_line(line)
                    try:
                        cur, total, mx = (int(x) for x in f[:3])
                        on_progress(total / mx if mx else 0)
                    except ValueError:
                        pass
                continue  # don't bloat logs with progress ticks
            lines.append(line)
            if fh:
                fh.write(line + "\n")
    finally:
        proc.wait()
        if device:
            PROCS.pop(device, None)
        if fh:
            fh.close()
    return proc.returncode, "\n".join(lines)


def windows_drives() -> list[dict] | None:
    """Ask Windows (not MakeMKV) which optical drives have a disc.

    This doesn't spin up or open the drives, so it's safe to call while other
    drives are mid-rip. Returns None if PowerShell isn't available.
    """
    ps = ("Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=5' | "
          "Select-Object DeviceID,VolumeName,Size | ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(r.stdout or "[]")
    except Exception:
        return None
    if isinstance(data, dict):
        data = [data]
    return [{
        "index": i,
        "loaded": bool(d.get("Size")),  # Size is empty when no disc is readable
        "drive_name": "",
        "disc_label": d.get("VolumeName") or "",
        "device": (d.get("DeviceID") or "").rstrip("\\"),
    } for i, d in enumerate(data)]


def scan_drives(cfg: dict) -> list[dict]:
    """Optical drives and the discs in them. Asks Windows first (doesn't disturb busy drives),
    falling back to MakeMKV's own drive list."""
    drives = None
    if os.name == "nt":
        drives = windows_drives()
        if drives is None:  # PowerShell can be slow while drives are busy; try once more
            drives = windows_drives()
    if drives is None:
        _, out = run_makemkv(cfg, ["--cache=1", "info", "disc:9999"])
        drives = parse_drives(out)
    if cfg.get("drives"):
        wanted = {d.upper().rstrip("\\") for d in cfg["drives"]}
        drives = [d for d in drives if d["device"].upper() in wanted]
    if os.name == "nt":
        try:  # audio CDs get a label from their MusicBrainz disc id ("Audio CD" isn't unique)
            import music
            for d in drives:
                music.label_drive(d)
        except Exception as exc:
            log(f"couldn't check for audio CDs: {exc}")
    return drives


def makemkv_source(drive: dict) -> str:
    """dev:D: when we know the drive letter, otherwise MakeMKV's own drive index."""
    dev = drive["device"]
    if re.fullmatch(r"[A-Za-z]:", dev):
        return f"dev:{dev.upper()}"
    return f"disc:{drive['index']}"


# --------------------------------------------------------------------------- drive locks

def lock_path(cfg: dict, device: str) -> Path:
    key = re.sub(r"[^A-Za-z0-9]", "", device).upper() or "UNKNOWN"
    return Path(cfg["output_root"]) / f".ripping_{key}.lock"


def drive_locked(cfg: dict, device: str) -> bool:
    p = lock_path(cfg, device)
    try:
        return time.time() - p.stat().st_mtime < LOCK_STALE
    except OSError:
        return False


def acquire_lock(cfg: dict, drive: dict) -> Path | None:
    """Claim a drive across every running copy of this script. None if taken."""
    p = lock_path(cfg, drive["device"])
    for _ in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if drive_locked(cfg, drive["device"]):
                return None
            try:
                p.unlink()  # stale lock from a window that was closed mid-rip
            except OSError:
                return None
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"pid": os.getpid(), "disc_label": drive["disc_label"],
                                 "started": dt.datetime.now().isoformat(timespec="seconds")}))
        return p
    return None


def heartbeat(p: Path, stop: threading.Event) -> None:
    while not stop.wait(LOCK_HEARTBEAT):
        try:
            os.utime(p)
        except OSError:
            pass


def safe_folder_name(label: str) -> str:
    label = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", label or "UNKNOWN_DISC").strip(" .")
    return label or "UNKNOWN_DISC"


def eject(device: str) -> None:
    """Open the tray of a drive given as a Windows drive letter like "E:".

    The letter goes into a PowerShell command, so anything that isn't a plain drive letter is
    ignored rather than passed through (a drive name comes from MakeMKV and config files).
    """
    m = re.fullmatch(r"\s*([A-Za-z]):?\\?\s*", str(device or ""))
    if not m:
        log(f"Not ejecting {device!r}: that isn't a drive letter")
        return
    ps = (f"(New-Object -ComObject Shell.Application).Namespace(17)"
          f".ParseName('{m.group(1).upper()}:').InvokeVerb('Eject')")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def append_history(cfg: dict, row: dict) -> None:
    path = Path(cfg["output_root"]) / "rip_history.csv"
    new = not path.exists()
    with print_lock, open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


# --------------------------------------------------------------------------- rip one disc

def claim_new_file(out_dir: Path, t: dict, since: float, disc: dict) -> None:
    """After ripping one title on its own: if MakeMKV wrote it under another name than the scan gave
    (its file name template, see ripfiles.py), give the new file the title's own name."""
    want = out_dir / t["output_file"]
    if want.exists() and want.stat().st_mtime >= since:
        return
    others = {o.get("output_file") for o in disc.get("titles", []) if o is not t}
    try:
        new = [p for p in out_dir.glob("*.mkv") if p.stat().st_mtime >= since and p.name != want.name
               and p.name not in others]
    except OSError:
        return
    if len(new) == 1:
        try:
            want.unlink(missing_ok=True)
            new[0].rename(want)
            log(f"title {t['title_id']}: MakeMKV saved it as {new[0].name}, renamed to {want.name}")
        except OSError as exc:
            log(f"title {t['title_id']}: couldn't rename {new[0].name} ({exc})")


def partial_rip(cfg: dict, label: str) -> Path | None:
    """An unfinished rip of this disc (no disc_info.json yet, nothing written for 10 minutes)."""
    root, prefix = Path(cfg["output_root"]), safe_folder_name(label) + "__"
    try:
        for d in sorted(root.iterdir(), reverse=True):
            if (d.is_dir() and d.name.startswith(prefix) and not (d / "disc_info.json").exists()
                    and ((d / "makemkv_info.txt").exists() or (d / "rip").is_dir())):  # rip/ = music CD
                newest = max([f.stat().st_mtime for f in [*d.iterdir(), *d.glob("rip/*")]] or [0])
                if time.time() - newest > 600:
                    return d
    except OSError:
        pass
    return None


def bytes_kept(out_dir: Path, rewriting: list[dict]) -> int:
    """Bytes of the MKVs in out_dir this pass won't rewrite (titles finished earlier).

    The progress bar counts every MKV in the folder, so on a resume or retry these are taken
    off; otherwise it would start near 100% and sit at 99% for the whole pass.
    """
    again = {t.get("output_file") for t in rewriting}
    total = 0
    for p in out_dir.glob("*.mkv"):
        if p.name not in again:
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def title_complete(cfg: dict, path: Path, title: dict) -> bool:
    """Is this ripped file finished? Its length must match the disc's (MKVToolNix), or failing
    that its size must be close to MakeMKV's estimate."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        import split
        mkvmerge = split.tool(cfg, "mkvmerge")
    except Exception:
        mkvmerge = None
    if mkvmerge:
        try:
            r = subprocess.run([mkvmerge, "-J", str(path)], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            got = (json.loads(r.stdout or "{}").get("container", {}).get("properties", {}).get("duration") or 0) / 1e9
            want = title.get("duration_seconds") or 0
            if got and want:
                # A rip cut short by a read error is minutes short. A few seconds either way is normal
                # (DVD lengths are rounded, cells get trimmed), and retrying those wasted a full re-read.
                return got >= want - max(10, 0.02 * want)
        except Exception:
            pass
    want = title.get("size_bytes") or 0
    # Only used without MKVToolNix. MakeMKV's estimate can be several percent high, and a 3% margin
    # re-read whole discs for nothing (tester reports, PR #12); a rip cut short by a read error is
    # usually far more than 10% short.
    return bool(want) and path.stat().st_size >= 0.9 * want


# MakeMKV's own read speed, roughly, for deciding how to rip (measured: about 17 MB/s from a
# Blu-ray, a third of that from a DVD)
READ_SPEED = {"dvd": 6e6, "bluray": 17e6}


def rip_all_cheaper(cfg: dict, disc: dict, todo: list[dict], listed: list[dict], scan_secs: float,
                    out_dir: Path) -> bool:
    """With some titles left out, makemkvcon has to be run once per title, and every run opens and
    analyses the whole disc again first (a few minutes on a big Blu-ray). When there are many titles
    to rip and the left-out ones are small, ripping everything in one pass and deleting the extras
    afterwards is far quicker: a disc with 27 short extras spent over an hour just re-reading."""
    if len(todo) <= 1:
        return False
    extra = sum(t.get("size_bytes") or 0 for t in listed if t not in todo)
    layout = str(disc.get("disc_layout") or "").lower()
    speed = READ_SPEED["dvd"] if layout == "dvd" else READ_SPEED["bluray"]
    rescans = (len(todo) - 1) * max(scan_secs, 20)
    if extra / speed >= rescans:
        return False
    try:  # the extra copies need room until they're deleted
        free = shutil.disk_usage(out_dir).free
        if free < sum(t.get("size_bytes") or 0 for t in listed) * 1.05 + float(cfg.get("min_free_gb_rips", 60)) * 1e9 / 2:
            return False
    except OSError:
        pass
    return True


def wait_for_picks(device: str, label: str, st: dict) -> set[int] | None:
    """Wait until you choose this disc's titles on its drive card (choose(device, ids)). Returns
    the title ids to rip, or None if the rip was cancelled meanwhile."""
    ev = threading.Event()
    PICKS[device] = {"event": ev, "ids": None}
    st.update(state="choosing", message="Choose the titles to rip, then press Rip", phase="")
    log(f"{device} [{label}] waiting for you to choose the titles to rip")
    for cb in ON_CHOOSE:
        try:
            cb(device, label)
        except Exception:
            pass
    try:
        while not ev.wait(1):
            if st.get("cancel"):
                return None
        if st.get("cancel"):
            return None
        return set(PICKS[device]["ids"] or [])
    finally:
        PICKS.pop(device, None)


def choose(device: str, ids) -> bool:
    """The titles you ticked on a drive that's waiting for them. False if it isn't waiting."""
    pick = PICKS.get(device)
    if not pick:
        return False
    pick["ids"] = [int(i) for i in ids]
    pick["event"].set()
    return True


def apply_picks(disc: dict, picked: set[int]) -> int:
    """Rip exactly the titles you ticked: one Shelfrip would have left out comes back in, and an
    unticked one is left out like a copy ("you left it out"). Returns how many are left out."""
    out = 0
    for t in disc["titles"]:
        if t.get("title_id") in picked:
            for k in ("skipped_duplicate_of", "skip_reason", "skip_kind"):
                t.pop(k, None)
            continue
        if "skipped_duplicate_of" not in t:
            t.update(skipped_duplicate_of=None, skip_reason="you left it out", skip_kind="chosen")
        out += 1
    return out


def title_summary(disc: dict) -> list[dict]:
    """Each title MakeMKV found, with its length, size and tracks, for the drive card."""
    out = []
    for t in disc.get("titles") or []:
        streams = t.get("streams") or []
        kind = lambda s: str(s.get("type") or "").lower()
        video = next((s for s in streams if kind(s).startswith("video")), {})
        audio = [s for s in streams if kind(s).startswith("audio")]
        subs = [s for s in streams if kind(s).startswith("subtitle")]
        name = lambda s: " ".join(x for x in (s.get("lang_name") or s.get("lang3") or "",
                                              s.get("codec") or "", s.get("name") or "") if x).strip()
        out.append({
            "id": t.get("title_id"), "length": t.get("duration") or "", "size": t.get("size_bytes") or 0,
            "chapters": t.get("chapters") or 0, "source": t.get("source_file") or "",
            "video": " ".join(x for x in (video.get("codec") or "", video.get("resolution") or "") if x),
            "audio": [name(s) for s in audio], "subtitles": [s.get("lang_name") or s.get("lang3") or "" for s in subs],
            "skip": t.get("skip_reason") or ("a copy of the feature" if "skipped_duplicate_of" in t else ""),
        })
    return out


def makemkv_problem(text: str) -> str:
    """The last error MakeMKV reported in its output, in its own words, else ''."""
    found = ""
    for line in text.splitlines():
        kind, f = split_robot_line(line)
        if kind != "MSG" or len(f) < 4:
            continue
        msg = f[3]
        if f[0] in ("4004", "3307", "3025", "3028", "5005", "5004"):  # worked-around glitches, counts
            continue
        if re.search(r"fail|error|can't|cannot|unable|denied|not enough|too long|invalid", msg, re.I):
            found = msg
    return found[:300]


def drive_lost(text: str) -> bool:
    """MakeMKV couldn't open the drive at all (it disconnected, or the disc was taken out): retrying
    the titles can't help, unlike read errors on a scratched disc."""
    for line in text.splitlines():
        kind, f = split_robot_line(line)
        if kind == "MSG" and f and f[0] in ("2024", "5010"):
            return True
    return bool(re.search(r"Unknown device|Failed to open disc", text, re.I))


def rip_disc(cfg: dict, drive: dict) -> dict:
    """Rip one disc: read its titles, leave out duplicates and scene-by-scene copies, rip the
    rest (resuming an interrupted rip), retry titles that came out short, and save
    disc_info.json. Returns the disc info with status ok / check / cancelled."""
    if drive.get("audio"):  # music CD: ripped to FLAC by cyanrip (music.py)
        import music
        return music.rip_cd(cfg, drive)
    device, label = drive["device"], drive["disc_label"]
    started = dt.datetime.now()
    resume = partial_rip(cfg, label)
    out_dir = resume or Path(cfg["output_root"]) / f"{safe_folder_name(label)}__{started:%Y%m%d-%H%M%S}"
    paths.make_folder(out_dir, "this disc's rip folder")
    src = makemkv_source(drive)
    min_len = str(int(cfg["min_length_seconds"]))
    log(f"{device} [{label}] scanning disc -> {out_dir}")
    st = STATUS.setdefault(device, {})
    st.update(label=label, state="scanning", pct=0, folder=out_dir.name, started=started.isoformat(timespec="seconds"),
              message="Reading the disc", phase="", titles=[], bytes_total=0, bytes_base=0)

    # 1. scan titles
    def phase(name: str) -> None:
        st["phase"] = name

    scan_start = time.time()
    rc, info = run_makemkv(cfg, [f"--minlength={min_len}", "info", src], device=device, on_phase=phase)
    scan_secs = time.time() - scan_start
    (out_dir / "makemkv_info.txt").write_text(info, encoding="utf-8")
    if st.get("cancel"):
        st.update(state="cancelled", message="cancelled", cancel=False)
        log(f"{device} [{label}] cancelled while reading the disc")
        return {}
    disc = parse_disc_info(info)
    if not disc["titles"] and cfg.get("retry_failed_titles", True) and not st.get("cancel"):
        # couldn't read the disc at all (often a drive timeout on a dirty or slow disc): try once more
        log(f"{device} [{label}] couldn't read the disc's titles, trying again in 20 seconds")
        st["message"] = "Couldn't read the disc, trying again"
        time.sleep(20)
        scan_start = time.time()
        rc, info = run_makemkv(cfg, [f"--minlength={min_len}", "info", src], device=device, on_phase=phase)
        scan_secs = time.time() - scan_start
        (out_dir / "makemkv_info.txt").write_text(info, encoding="utf-8")
        disc = parse_disc_info(info)
    import ripfiles
    if ripfiles.unclash(disc):
        log(f"{device} [{label}] MakeMKV's file name template gives several titles the same file name; "
            f"Shelfrip tells them apart itself (MakeMKV Preferences > Advanced > Default output file name "
            f"template: keep {{_t:N2}} at the end)")
    disc.update({
        "drive": device,
        "drive_name": drive["drive_name"],
        "disc_label": label,
        "ripped_at": started.isoformat(timespec="seconds"),
        "min_length_seconds": int(min_len),
    })
    if re.fullmatch(r"[A-Za-z]:", device):
        try:  # TheDiscDB fingerprint (file sizes only), used by the naming step
            import discdb
            fp = discdb.fingerprint(device + "\\")
            if fp:
                disc.update(content_hash=fp["content_hash"], disc_layout=fp["layout"])
        except Exception as exc:
            log(f"{device} [{label}] couldn't fingerprint disc ({exc})")
        scanner = Path(cfg.get("thediscdb_scan_path") or "")
        if scanner.is_file():
            try:  # reads disc structure only; the file is offered for upload after filing
                subprocess.run([str(scanner), device + "\\", "--output", str(out_dir / "disc.odm.json"),
                                "--force", "--quiet"], capture_output=True, timeout=300,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception as exc:
                log(f"{device} [{label}] TheDiscDB scan skipped ({exc})")
    n_titles = len(disc["titles"])
    total_min = sum(t.get("duration_seconds", 0) for t in disc["titles"]) // 60
    log(f"{device} [{label}] {n_titles} titles, {total_min} min total. Ripping...")
    st.update(state="ripping", message=f"{n_titles} titles, {total_min} min")

    # 1b. optional: rip each feature or episode only once (dupes.py)
    skipped: dict = {}
    if cfg.get("skip_duplicate_features") and not st.get("cancel"):
        try:
            import dupes
            st["message"] = "Checking for duplicate copies"
            skipped = dupes.plan(disc, cfg, log=log)
        except Exception as exc:
            log(f"{device} [{label}] couldn't check for duplicate copies ({exc}), ripping everything")
            skipped = {}
            for t in disc["titles"]:
                t.pop("skipped_duplicate_of", None)
                t.pop("skip_reason", None)
                t.pop("skip_kind", None)
        kinds = {t["title_id"]: t.get("skip_kind") for t in disc["titles"]}
        for tid, (keep, why) in sorted(skipped.items()):
            log(f"{device} [{label}] not ripping title {tid}: "
                + (why if kinds.get(tid) in ("scene", "play_all") else f"a copy of title {keep} ({why})"))
        if skipped:
            saved_min = sum(t.get("duration_seconds", 0) for t in disc["titles"] if t["title_id"] in skipped) // 60
            saved_gb = sum(t.get("size_bytes") or 0 for t in disc["titles"] if t["title_id"] in skipped) / 1e9
            n_scenes = sum(1 for tid in skipped if kinds.get(tid) == "scene")
            n_plays = sum(1 for tid in skipped if kinds.get(tid) == "play_all")
            n_copies = len(skipped) - n_scenes - n_plays
            if n_copies:
                disc["skipped_duplicates"] = n_copies
            if n_scenes:
                disc["skipped_scenes"] = n_scenes
            if n_plays:
                disc["skipped_play_alls"] = n_plays
            plural = lambda n, one, many: f"{n} {one if n == 1 else many}"
            bits = [plural(n_copies, "duplicate copy", "duplicate copies") if n_copies else "",
                    plural(n_scenes, "scene-by-scene title", "scene-by-scene titles") if n_scenes else "",
                    plural(n_plays, "Play All", "Play Alls") if n_plays else ""]
            what = ", ".join(b for b in bits if b)
            st["message"] = f"{n_titles - len(skipped)} titles to rip ({what} left out)"
            log(f"{device} [{label}] {what} left out ({saved_min} min of video, {saved_gb:.1f} GB)")

    # 1c. optional: wait for you to choose the titles on the drive card (pre-ticked from the plan above)
    st["titles"] = title_summary(disc)  # what's on the disc, for the drive card
    if cfg.get("choose_titles") and disc["titles"] and not st.get("cancel"):
        picked = wait_for_picks(device, label, st)
        if picked is None:
            st.update(state="cancelled", message="cancelled", cancel=False, phase="")
            log(f"{device} [{label}] cancelled while choosing titles")
            return {}
        n_out = apply_picks(disc, picked)
        st["titles"] = title_summary(disc)
        st.update(state="ripping", message=f"{n_titles - n_out} titles to rip ({n_out} left out)")
        log(f"{device} [{label}] ripping {n_titles - n_out} of {n_titles} titles you chose")

    # 2. rip the titles
    last_pct = [-10]

    def progress(frac: float) -> None:
        pct = int(frac * 100)
        st["pct"] = pct
        if pct >= last_pct[0] + 10:
            last_pct[0] = pct - pct % 10
            log(f"{device} [{label}] {last_pct[0]}%")

    def rip_each(titles: list[dict]) -> tuple[int, str]:
        """One makemkvcon run per title. Each run opens and analyses the whole disc again first, so
        progress is weighed by size and a run's own scan doesn't count."""
        total = sum(t.get("size_bytes") or 0 for t in titles) or 1
        rc, outs, before = 0, [], 0
        for t in titles:
            if st.get("cancel"):
                break
            try:
                (out_dir / t["output_file"]).unlink(missing_ok=True)  # an unfinished copy
            except OSError:
                pass
            share = (t.get("size_bytes") or 0) / total
            began = time.time() - 2
            r, o = run_makemkv(cfg, [f"--minlength={min_len}", "mkv", src, str(t["title_id"]), str(out_dir)],
                               logfile=out_dir / "rip.log", device=device, on_phase=phase,
                               on_progress=lambda f, b=before / total, s=share: progress(b + f * s))
            claim_new_file(out_dir, t, began, disc)
            before += t.get("size_bytes") or 0
            rc, outs = rc or r, outs + [o]
        return rc, "\n".join(outs)

    # titles already ripped completely by an earlier, interrupted rip are kept, not ripped again
    listed = [t for t in disc["titles"] if t.get("output_file")]
    wanted = [t for t in listed if "skipped_duplicate_of" not in t]
    todo = list(wanted)
    if resume:
        ripfiles.match_files(out_dir, disc, cfg)
        done = [t for t in todo if title_complete(cfg, out_dir / t["output_file"], t)]
        todo = [t for t in todo if t not in done]
        log(f"{device} [{label}] continuing an unfinished rip: {len(done)} title(s) already done, "
            f"{len(todo)} to rip")
        st["message"] = f"continuing: {len(done)} done, {len(todo)} to rip"
    one_pass = len(todo) == len(listed) or (not resume and rip_all_cheaper(cfg, disc, todo, listed, scan_secs, out_dir))
    st["bytes_total"] = sum(t.get("size_bytes") or 0 for t in (listed if one_pass else todo))
    st["bytes_base"] = bytes_kept(out_dir, listed if one_pass else todo)
    if one_pass:
        if len(todo) != len(listed):
            log(f"{device} [{label}] ripping every title in one pass and deleting the "
                f"{len(listed) - len(todo)} left out afterwards: quicker than re-reading the disc "
                f"({scan_secs / 60:.1f} min) before each of {len(todo)} titles")
        rc, rip_out = run_makemkv(
            cfg, [f"--minlength={min_len}", "mkv", src, "all", str(out_dir)],
            logfile=out_dir / "rip.log", on_progress=progress, device=device, on_phase=phase,
        )
        if ripfiles.match_files(out_dir, disc, cfg):
            log(f"{device} [{label}] matched the ripped files to their titles by length")
        keep = {t["output_file"] for t in wanted}
        for t in listed:
            if t["output_file"] not in keep:
                try:
                    (out_dir / t["output_file"]).unlink(missing_ok=True)
                except OSError as exc:
                    log(f"{device} [{label}] couldn't delete left-out title {t['title_id']} ({exc})")
        if len(todo) != len(listed):
            rip_out = re.sub(r"\d+ titles? saved(?:, \d+ failed)?", "", rip_out)  # counted below from the files
    else:
        rc, rip_out = rip_each(todo)
        rip_out = re.sub(r"\d+ titles? saved(?:, \d+ failed)?", "", rip_out)  # counted below from the files
    cancelled = st.get("cancel")

    # 2b. a scratched or dirty disc: rip the titles that didn't come out whole once more
    lost = not cancelled and drive_lost(rip_out) and not any(out_dir.glob("*.mkv"))
    if lost:
        log(f"{device} [{label}] MakeMKV couldn't open the {device} drive: it disconnected or the disc was "
            f"taken out. Not retrying; put the disc back in and rip it again")
    if not cancelled and not lost and cfg.get("retry_failed_titles", True):
        bad = [t for t in wanted if not title_complete(cfg, out_dir / t["output_file"], t)]
        if bad:
            log(f"{device} [{label}] {len(bad)} title(s) didn't rip cleanly, trying them once more")
            st["message"] = f"retrying {len(bad)} title(s) that had read errors"
            last_pct[0] = -10
            st["bytes_total"] = sum(t.get("size_bytes") or 0 for t in bad)
            st["bytes_base"] = bytes_kept(out_dir, bad)
            _, again = rip_each(bad)
            cancelled = st.get("cancel")
            still = [t for t in bad if not title_complete(cfg, out_dir / t["output_file"], t)]
            rip_out = re.sub(r"\d+ titles? saved(?:, \d+ failed)?", "", rip_out + "\n" + again)
            disc["retried_titles"] = [t["title_id"] for t in bad]
            log(f"{device} [{label}] retry: {len(bad) - len(still)} of {len(bad)} now complete"
                + (f"; {len(still)} still came out shorter than the disc says (read errors in the same "
                   f"place), kept as they are: check them in Review" if still else ""))
    errs = read_errors(out_dir / "rip.log")
    disc["read_errors"], disc["damaged_files"] = errs["count"], errs["files"]

    # 3. record what actually landed on disk
    mkvs = {p.name: p.stat().st_size for p in out_dir.glob("*.mkv")}
    for t in disc["titles"]:
        name = t.get("output_file")
        t["ripped"] = bool(name and name in mkvs)
    m = re.search(r"(\d+) titles? saved(?:, (\d+) failed)?", rip_out)
    saved = int(m.group(1)) if m else len(mkvs)
    failed = int(m.group(2) or 0) if m else max(0, n_titles - len(skipped) - len(mkvs))
    status = "cancelled" if cancelled else "ok" if rc == 0 and failed == 0 and mkvs else "check"
    disc.update({"status": status, "titles_saved": saved, "titles_failed": failed,
                 "finished_at": dt.datetime.now().isoformat(timespec="seconds")})
    problem = "" if status == "ok" else makemkv_problem(rip_out)
    if lost:
        problem = (f"MakeMKV couldn't open the {device} drive, so nothing was saved: it may have disconnected, "
                   f"or the disc was taken out. Put the disc back in and rip it again (a USB drive may need a "
                   f"powered hub or another port).")
        disc["drive_lost"] = True
    if problem:
        disc["makemkv_error"] = problem
    if not cancelled:  # a cancelled rip gets no disc_info.json, so it isn't named or filed
        (out_dir / "disc_info.json").write_text(json.dumps(disc, indent=2), encoding="utf-8")

    mins = (dt.datetime.now() - started).seconds // 60
    if status == "ok":
        log(f"{device} [{label}] DONE: {saved} titles in {mins} min")
    else:
        log(f"{device} [{label}] FINISHED WITH PROBLEMS: {saved} saved, {failed} failed "
            f"(see {out_dir / 'rip.log'})" + (f". MakeMKV said: {problem}" if problem else ""))

    append_history(cfg, {
        "finished": disc["finished_at"], "drive": device, "disc_label": label,
        "folder": str(out_dir), "titles_saved": saved, "titles_failed": failed,
        "minutes": mins, "status": status,
    })
    st.update(state="done" if status == "ok" else status, pct=100 if status == "ok" else st.get("pct", 0),
              message=(f"{saved} titles ripped in {mins} min" if status == "ok" else
                       "cancelled" if cancelled else f"{saved} saved, {failed} failed"
                       + (f". MakeMKV said: {problem}" if problem else ": check rip.log")),
              cancel=False, phase="")
    if not cancelled and not lost and (failed or disc["read_errors"]):  # a lost drive isn't a damaged disc
        note_damaged(cfg, label, out_dir, disc["read_errors"], failed, disc["damaged_files"], problem)
    if cfg.get("eject_when_done", True) and not cancelled:
        eject(device)
    for cb in ON_DONE:
        try:
            cb(disc, out_dir)
        except Exception as exc:
            log(f"after-rip step failed: {exc}")
    return disc


def read_errors(log_path: Path) -> dict:
    """Read errors MakeMKV reported in rip.log: {count, files}."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"count": 0, "files": []}
    # only errors reading this disc's files: MakeMKV also logs errors from other drives it probes
    # (e.g. a music CD in another drive), which name the drive instead of a file
    lines = [ln for ln in text.splitlines()
             if (ln.startswith("MSG:2003,") or re.search(r"occurred while reading|MEDIUM ERROR|hash check failed", ln, re.I))
             and re.search(r"while reading '/", ln)]
    files = []
    for ln in lines:
        m = re.search(r"'([^']+\.(?:m2ts|vob|ifo|mpls))'", ln, re.I)
        if m and m.group(1) not in files:
            files.append(m.group(1))
    return {"count": len(lines), "files": files[:20]}


DAMAGED = "damaged_discs.json"


def note_damaged(cfg: dict, label: str, folder: Path, errors: int, failed: int, files: list[str],
                 problem: str = "") -> None:
    """Remember a disc that had read errors, for the list on the Drives tab."""
    p = Path(cfg["output_root"]) / DAMAGED
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        rows = []
    rows = [r for r in rows if r.get("label") != label]
    rows.append({"label": label, "folder": folder.name, "when": dt.datetime.now().isoformat(timespec="minutes"),
                 "read_errors": errors, "titles_failed": failed, "files": files, "problem": problem})
    try:
        p.write_text(json.dumps(rows[-200:], indent=2), encoding="utf-8")
    except OSError:
        pass


def cancel(device: str) -> bool:
    """Stop the rip running on a drive. Its partial folder is left for you to delete."""
    proc = PROCS.get(device)
    if not proc:
        if device in PICKS:  # waiting for you to choose titles: nothing is running yet
            STATUS.setdefault(device, {})["cancel"] = True
            PICKS[device]["event"].set()
            log(f"{device} rip cancelled")
            return True
        return False
    STATUS.setdefault(device, {})["cancel"] = True
    proc.terminate()
    log(f"{device} rip cancelled")
    return True


# --------------------------------------------------------------------------- main

def already_ripped(cfg: dict, label: str) -> str:
    """Folder name of an earlier rip of this disc that's waiting for review (or filed), else ''.

    A rip that saved nothing (the drive dropped out, every title failed) doesn't count, so
    putting the disc back in rips it again instead of saying it's already done.
    """
    root = Path(cfg["output_root"])
    prefix = safe_folder_name(label) + "__"
    for base in (root, root / "_Filed"):
        try:
            for d in base.iterdir():
                if d.is_dir() and d.name.startswith(prefix) and (d / "disc_info.json").exists():
                    if saved_nothing(d):
                        continue
                    return d.name
        except OSError:
            pass
    return ""


def saved_nothing(folder: Path) -> bool:
    """True when this rip folder's disc_info.json says no title was saved and no MKV is there."""
    try:
        info = json.loads((folder / "disc_info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False  # can't tell: treat as a real rip, the safe side for "don't rip twice"
    return info.get("titles_saved") == 0 and not any(folder.glob("*.mkv"))


ON_LOW_SPACE: list = []   # app.py: alert when a rip is held back for space
ON_CHOOSE: list = []      # app.py: a disc is waiting for you to choose its titles, (device, label)
ON_ERROR: list = []       # app.py: a rip that crashed, (device, label, error), to offer an error report


def low_space(cfg: dict, d: dict) -> str:
    """A sentence if the rip folder's drive is too full to start this disc, else ''."""
    need_gb = 2 if d.get("audio") else float(cfg.get("min_free_gb_rips", 60))
    try:
        free = shutil.disk_usage(cfg["output_root"]).free / 1e9
    except OSError:
        return ""
    if free < need_gb:
        return (f"Not ripped yet: only {free:.0f} GB free in {cfg['output_root']} (keeps {need_gb:.0f} GB free). "
                f"File or discard some rips and it starts by itself.")
    return ""


def rip_loaded(cfg: dict, busy: set[str], finished: dict[str, str],
               notify_locked: bool = True, force: set[str] | None = None,
               skip=None) -> list[threading.Thread]:
    """Start a rip on every loaded drive that isn't already busy.

    Drives being ripped by another window (fresh lock file) are left alone.

    `finished` remembers which disc each drive last ripped, so a disc that
    didn't eject isn't ripped twice. It's cleared once the drive is empty.
    """
    threads = []
    for d in scan_drives(cfg):
        dev = d["device"]
        if not d["loaded"]:
            finished.pop(dev, None)
            continue
        if dev in busy or finished.get(dev) == d["disc_label"]:
            continue
        forced = force is not None and dev in force
        checks = None if forced else skip
        if not forced and already_ripped(cfg, d["disc_label"]):  # still in the drive while you review it
            finished[dev] = d["disc_label"]
            continue
        short = low_space(cfg, d)
        if short:  # leave it in the drive; it's ripped once there's room again
            st = STATUS.setdefault(dev, {})
            if st.get("state") != "low_space" or st.get("label") != d["disc_label"]:
                log(f"{dev} [{d['disc_label']}] {short}")
                for cb in ON_LOW_SPACE:
                    cb(short)
            STATUS[dev] = {"label": d["disc_label"], "state": "low_space", "message": short}
            continue
        lock = acquire_lock(cfg, d)
        if lock is None:
            if notify_locked:
                log(f"{dev} [{d['disc_label']}] is already being ripped by another window, skipping.")
            finished[dev] = d["disc_label"]
            continue
        busy.add(dev)
        finished[dev] = d["disc_label"]

        def worker(drive=d, lock=lock, checks=checks):
            stop = threading.Event()
            threading.Thread(target=heartbeat, args=(lock, stop), daemon=True).start()
            try:
                # The check before ripping (e.g. "is it in Plex already?") reads the disc, which can
                # be slow, so it runs here: one slow drive doesn't hold up the others.
                if checks:
                    STATUS[drive["device"]] = {"label": drive["disc_label"], "state": "scanning",
                                               "message": "Checking the disc"}
                    if checks(drive):
                        return
                rip_disc(cfg, drive)
            except Exception as exc:  # keep the other drives going
                import traceback
                log(f"{drive['device']} ERROR: {exc}\n" + traceback.format_exc())
                STATUS.setdefault(drive["device"], {}).update(state="error", message=str(exc))
                for cb in ON_ERROR:
                    try:
                        cb(drive["device"], drive["disc_label"], str(exc))
                    except Exception:
                        pass
            finally:
                stop.set()
                try:
                    lock.unlink()
                except OSError:
                    pass
                busy.discard(drive["device"])

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        threads.append(t)
    return threads


def main() -> None:
    ap = argparse.ArgumentParser(description="Rip all loaded discs with MakeMKV.")
    ap.add_argument("--watch", action="store_true",
                    help="keep running and rip each new disc as it's inserted")
    ap.add_argument("--list", action="store_true", help="just list drives and discs")
    args = ap.parse_args()

    cfg = load_config()
    paths.make_folder(cfg["output_root"], "the rip folder")

    if args.list:
        for d in scan_drives(cfg):
            state = f"disc: {d['disc_label']}" if d["loaded"] else "empty"
            if d["loaded"] and drive_locked(cfg, d["device"]):
                state += "  (ripping now)"
            print(f"{d['device']:4} {d['drive_name']:40} {state}")
        return

    busy: set[str] = set()
    finished: dict[str, str] = {}
    if not args.watch:
        threads = rip_loaded(cfg, busy, finished)
        if not threads:
            log("No new discs to rip (drives are empty or already being ripped by another window).")
            return
        log(f"Ripping {len(threads)} disc(s) in parallel...")
        for t in threads:
            t.join()
        log("All done.")
        return

    log("Watch mode: insert discs any time. Press Ctrl+C to stop.")
    try:
        while True:
            rip_loaded(cfg, busy, finished)
            time.sleep(cfg["watch_poll_seconds"])
    except KeyboardInterrupt:
        if busy:
            log(f"Stopping. Still ripping: {', '.join(sorted(busy))} (those rips will be cut off).")
        else:
            log("Stopped.")


if __name__ == "__main__":
    main()
