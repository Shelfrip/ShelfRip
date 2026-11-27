r"""
music.py - rip music CDs to FLAC and file them into the Plex music library.

How it fits in the app:
  * The Drives tab notices an audio CD (Windows shows Track01.cda ... on it) and names the disc
    by its MusicBrainz disc id ("CD_<id>"), so the same CD is recognised again later.
  * cyanrip (a free command-line CD ripper, https://github.com/cyanreg/cyanrip) does the ripping:
    secure reads, AccurateRip check, FLAC (lossless). Put cyanrip.exe in tools\cyanrip, or set
    cyanrip_path in Settings.
  * MusicBrainz names the album and tracks. When the disc belongs to several releases, the most
    likely one is used and the Review tab offers the others in a Release list.
  * Filing follows Plex's music naming guide:
        Music\Artist\Album\01 - Title.flac
        Music\Artist\Album\101 - Title.flac        (disc 1 of a multi-disc album)
        Music\Various Artists\Album\01 - Title.flac (compilations)
    with full tags written into each FLAC, cover.jpg in the album folder, and lyrics next to
    each track as .lrc (timed) or .txt (plain) from LRCLIB, which is how Plex reads local lyrics.

Only the Python standard library is used.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
BUNDLED = HERE / "tools" / "cyanrip"
CYANRIP_URL = "https://github.com/cyanreg/cyanrip/releases/latest"
# switches that only turn off online lookups; safe to drop when a cyanrip build doesn't know them
OPTIONAL_FLAGS = {"-N", "-U", "-B", "-A", "-X"}
UNSUPPORTED_FLAGS: set[str] = set()
import paths
UA = paths.USER_AGENT
VARIOUS = "89ad4ac3-39f7-470e-963a-56509c546377"  # MusicBrainz's "Various Artists"
LABEL_PREFIX = "CD_"


# --------------------------------------------------------------------------- the disc

def is_audio_cd(device: str) -> bool:
    """Windows lists an audio CD's tracks as Track01.cda, Track02.cda, ..."""
    try:
        return any(n.lower().endswith(".cda") for n in os.listdir(device + "\\"))
    except OSError:
        return False


def read_toc(device: str) -> dict:
    """The disc's table of contents, straight from the drive (Windows or Linux).

    {first, last, leadout, offsets: [sector of each audio track, +150 like MusicBrainz counts]}"""
    if sys.platform.startswith("linux"):
        import linux_support
        return parse_toc(linux_support.read_toc(device))
    import ctypes
    wintypes = _wintypes()
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    h = k32.CreateFileW("\\\\.\\" + device, 0x80000000, 3, None, 3, 0, None)  # GENERIC_READ, share r/w
    if h in (None, wintypes.HANDLE(-1).value):
        raise OSError(f"can't open {device} ({ctypes.get_last_error()})")
    try:
        buf = ctypes.create_string_buffer(804)
        got = wintypes.DWORD()
        if not k32.DeviceIoControl(h, 0x24000, None, 0, buf, 804, ctypes.byref(got), None):  # IOCTL_CDROM_READ_TOC
            raise OSError(f"can't read the CD's table of contents ({ctypes.get_last_error()})")
        raw = buf.raw
    finally:
        k32.CloseHandle(h)
    return parse_toc(raw)


def _wintypes():
    """The few Windows types we need. (ctypes.wintypes itself isn't bundled in older builds of
    Shelfrip.exe, so they're defined here from plain ctypes types.)"""
    import ctypes

    class W:
        HANDLE = ctypes.c_void_p
        LPVOID = ctypes.c_void_p
        LPCWSTR = ctypes.c_wchar_p
        DWORD = ctypes.c_uint32
    return W


def read_cdtext(device: str) -> dict:
    """The album and track names stored on the CD itself (CD-Text), if it has any (Windows)."""
    if sys.platform.startswith("linux"):
        return {}  # Linux CD-Text is not implemented yet; MusicBrainz still supplies names.
    import ctypes
    wintypes = _wintypes()
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    h = k32.CreateFileW("\\\\.\\" + device, 0x80000000, 3, None, 3, 0, None)
    if h in (None, wintypes.HANDLE(-1).value):
        return {}
    try:
        req = ctypes.create_string_buffer(bytes([0x05, 0, 0, 0]), 4)  # CDROM_READ_TOC_EX_FORMAT_CDTEXT
        buf = ctypes.create_string_buffer(65536)
        got = wintypes.DWORD()
        if not k32.DeviceIoControl(h, 0x24054, req, 4, buf, 65536, ctypes.byref(got), None):  # IOCTL_CDROM_READ_TOC_EX
            return {}
        raw = buf.raw[:got.value]
    finally:
        k32.CloseHandle(h)
    return parse_cdtext(raw)


def parse_cdtext(raw: bytes) -> dict:
    """{album, artist, tracks: {n: {title, artist}}} from CD-Text packs (block 0, single-byte text)."""
    if len(raw) < 4:
        return {}
    texts: dict[int, dict[int, str]] = {0x80: {}, 0x81: {}}
    pending: dict[int, tuple[int, bytes]] = {}
    for i in range(4, len(raw) - 17, 18):
        pack = raw[i:i + 18]
        kind, track, info = pack[0], pack[1] & 0x7F, pack[3]
        if kind not in texts or (info >> 4) & 0x07 != 0 or info & 0x80:  # block 0, not double-byte
            continue
        cur_track, data = pending.get(kind, (track, b""))
        data += pack[4:16]
        parts = data.split(b"\0")
        for part in parts[:-1]:
            text = part.decode("latin-1")
            text = "\t" if text == "\t" else text.strip()
            if text == "\t":  # same as the previous track
                text = texts[kind].get(cur_track - 1, "")
            if text and cur_track not in texts[kind]:
                texts[kind][cur_track] = text
            cur_track += 1
        pending[kind] = (cur_track, parts[-1])
    titles, artists = texts[0x80], texts[0x81]
    if not titles:
        return {}
    nums = sorted(n for n in set(titles) | set(artists) if n > 0)
    return {"album": titles.get(0, ""), "artist": artists.get(0, ""),
            "tracks": {n: {"title": titles.get(n, ""), "artist": artists.get(n, "")} for n in nums}}


def parse_toc(raw: bytes) -> dict:
    first, last = raw[2], raw[3]
    tracks = []
    for i in range(last - first + 2):  # every track plus the lead-out (0xAA)
        e = raw[4 + 8 * i: 12 + 8 * i]
        m, s, f = e[5], e[6], e[7]
        tracks.append({"n": e[2], "data": bool(e[1] & 0x04), "offset": (m * 60 + s) * 75 + f})
    leadout = tracks.pop()["offset"]
    # Enhanced CDs end with a data track: MusicBrainz leaves it out and ends the audio 11400 sectors before it
    while tracks and tracks[-1]["data"]:
        leadout = tracks.pop()["offset"] - 11400
    audio = [t for t in tracks if not t["data"]]
    return {"first": audio[0]["n"], "last": audio[-1]["n"], "leadout": leadout,
            "offsets": [t["offset"] for t in audio]}


def disc_id(toc: dict) -> str:
    """MusicBrainz disc id (same algorithm as libdiscid)."""
    s = "%02X%02X%08X" % (toc["first"], toc["last"], toc["leadout"])
    offs = list(toc["offsets"]) + [0] * (99 - len(toc["offsets"]))
    s += "".join("%08X" % o for o in offs[:99])
    b = base64.b64encode(hashlib.sha1(s.encode("ascii")).digest()).decode("ascii")
    return b.replace("+", ".").replace("/", "_").replace("=", "-")


def toc_string(toc: dict) -> str:
    return " ".join(str(x) for x in [toc["first"], toc["last"], toc["leadout"], *toc["offsets"]])


def track_seconds(toc: dict) -> list[float]:
    ends = toc["offsets"][1:] + [toc["leadout"]]
    return [(e - o) / 75.0 for o, e in zip(toc["offsets"], ends)]


def label_drive(d: dict) -> bool:
    """Mark a scanned drive as an audio CD and give it a stable label from its disc id."""
    dev = d.get("device") or ""
    if sys.platform.startswith("linux"):
        if not d.get("audio"):
            return False
    elif not re.fullmatch(r"[A-Za-z]:", dev) or not is_audio_cd(dev):
        return False
    d["audio"] = True
    d["loaded"] = True
    try:
        toc = read_toc(dev)
        d["toc"] = toc
        d["disc_label"] = LABEL_PREFIX + disc_id(toc)
    except Exception:
        d["disc_label"] = d.get("disc_label") or "Audio CD"
    return True


# --------------------------------------------------------------------------- cyanrip

def find_cyanrip(cfg: dict) -> str | None:
    for p in (cfg.get("cyanrip_path"), BUNDLED / "cyanrip.exe", BUNDLED / "cyanrip-win64.exe"):
        if p and Path(p).is_file():
            return str(p)
    return shutil.which("cyanrip")


def drive_offset(cfg: dict, device: str) -> int:
    """Read offset for a drive from the cd_offsets setting, e.g. "D:6, E:667"."""
    text = str(cfg.get("cd_offsets") or "")
    if device.startswith("/dev/"):
        for dev, off in re.findall(r"(/dev/[^\s,=:]+)\s*[=:]\s*([+-]?\d+)", text):
            if os.path.realpath(dev) == os.path.realpath(device):
                return int(off)
    for dev, off in re.findall(r"([A-Za-z]):?\s*[=:]?\s*([+-]?\d+)", text):
        if dev.upper() + ":" == device.upper():
            return int(off)
    m = re.fullmatch(r"\s*([+-]?\d+)\s*", text)
    return int(m.group(1)) if m else 0


def flac_info(path: Path) -> dict:
    """{seconds, rate, samples} from a FLAC file's STREAMINFO, or {} if it isn't a finished FLAC."""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"fLaC":
                return {}
            hdr = f.read(4)
            if hdr[0] & 0x7F != 0:
                return {}
            b = f.read(34)
    except OSError:
        return {}
    rate = (b[10] << 12) | (b[11] << 4) | (b[12] >> 4)
    samples = ((b[13] & 0x0F) << 32) | struct.unpack(">I", b[14:18])[0]
    return {"rate": rate, "samples": samples, "seconds": samples / rate if rate else 0}


def ripped_tracks(out_dir: Path) -> dict[int, Path]:
    """Track number -> FLAC file already in the rip folder."""
    out = {}
    for f in (out_dir / "rip").glob("*.flac") if (out_dir / "rip").is_dir() else []:
        m = re.match(r"(?:\d+\.)?(\d+)", f.stem)
        if m:
            out[int(m.group(1))] = f
    return out


def rip_cd(cfg: dict, drive: dict) -> dict:
    """Rip an audio CD with cyanrip into the rip folder. Same shape of result as rip_all.rip_disc."""
    import rip_all
    device, label = drive["device"], drive["disc_label"]
    started = dt.datetime.now()
    st = rip_all.STATUS.setdefault(device, {})
    st.update(label=label, state="scanning", pct=0, started=started.isoformat(timespec="seconds"),
              message="Reading the CD")
    exe = find_cyanrip(cfg)
    if not exe:
        msg = f"cyanrip isn't installed: install it on PATH or choose its location in Settings ({CYANRIP_URL})"
        st.update(state="error", message=msg)
        rip_all.log(f"{device} [{label}] {msg}")
        return {}
    toc = drive.get("toc") or read_toc(device)
    try:
        cdtext = read_cdtext(device)
    except Exception:
        cdtext = {}
    did = disc_id(toc)
    secs = track_seconds(toc)
    n = len(secs)
    resume = rip_all.partial_rip(cfg, label)
    out_dir = resume or Path(cfg["output_root"]) / f"{rip_all.safe_folder_name(label)}__{started:%Y%m%d-%H%M%S}"
    (out_dir / "rip").mkdir(parents=True, exist_ok=True)
    st["folder"] = out_dir.name

    # only rip tracks that aren't already there in full (a cancelled rip leaves the rest)
    have = ripped_tracks(out_dir)
    todo = []
    for k in range(1, n + 1):
        f = have.get(k)
        if f and abs(flac_info(f).get("seconds", -99) - secs[k - 1]) <= 2.5:
            continue
        if f:
            f.unlink(missing_ok=True)
        todo.append(k)
    log_path = out_dir / "rip.log"
    rc, cancelled, tail = 0, False, ""
    if todo:
        args = [exe, "-d", device, "-s", str(drive_offset(cfg, device)), "-o", "flac",
                "-N", "-U", "-B", "-X", "-D", "rip", "-F", "{track}", "-L", "cyanrip"]
        if len(todo) < n:
            args += ["-l", ",".join(map(str, todo))]
            rip_all.log(f"{device} [{label}] {n - len(todo)} track(s) already ripped, ripping the other {len(todo)}")
        rip_all.log(f"{device} [{label}] ripping {len(todo)} track(s) to FLAC -> {out_dir}")
        st.update(state="ripping", message=f"{n} tracks, {int(sum(secs) // 60)} min")
        total = sum(secs[k - 1] for k in todo) or 1
        done_before = {k: sum(secs[j - 1] for j in todo if j < k) for k in todo}
        args = [a for a in args if a not in UNSUPPORTED_FLAGS]
        for _attempt in range(len(OPTIONAL_FLAGS) + 1):
            mark = log_path.stat().st_size if log_path.exists() else 0
            import processes
            proc = processes.spawn(args, cwd=str(out_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            rip_all.end_with_us(proc)
            rip_all.PROCS[device] = proc
            cur = todo[0]
            try:
                with open(log_path, "ab") as lf:
                    buf = b""
                    while True:
                        ch = proc.stdout.read(256)
                        if not ch:
                            break
                        lf.write(ch)
                        buf += ch
                        parts = re.split(rb"[\r\n]", buf)
                        buf = parts.pop()
                        for line in parts:
                            text = line.decode("utf-8", "replace")
                            m = re.search(r"[Tt]rack\s+(\d+)", text)
                            if m and int(m.group(1)) in done_before:
                                cur = int(m.group(1))
                            p = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
                            if p:
                                frac = min(1.0, float(p.group(1)) / 100)
                                st["pct"] = min(99, int(100 * (done_before[cur] + frac * secs[cur - 1]) / total))
                                st["message"] = f"track {cur} of {n}"
                rc = proc.wait()
            finally:
                proc.stdout.close()
                rip_all.PROCS.pop(device, None)
            # older cyanrip builds don't know every switch (e.g. -B); drop the unknown one and try again
            try:
                with open(log_path, "rb") as lf:
                    lf.seek(mark)
                    tail = lf.read().decode("utf-8", "replace")
            except OSError:
                tail = ""
            bad = re.search(r"unknown option -- ?(\w)", tail) or re.search(r"[Uu]nknown option[^\w-]*-(\w)\b", tail)
            flag = "-" + bad.group(1) if bad else ""
            if rc == 0 or st.get("cancel") or flag not in OPTIONAL_FLAGS or flag not in args:
                break
            UNSUPPORTED_FLAGS.add(flag)
            args.remove(flag)
            rip_all.log(f"{device} [{label}] this cyanrip doesn't support {flag}, retrying without it")
        cancelled = bool(st.get("cancel"))
    have = ripped_tracks(out_dir)
    good = [k for k in range(1, n + 1) if k in have and abs(flac_info(have[k]).get("seconds", -99) - secs[k - 1]) <= 2.5]
    failed = n - len(good)
    status = "cancelled" if cancelled else "ok" if rc == 0 and not failed else "check"
    log_text = ""
    for lp in [*out_dir.glob("rip/*.log"), *out_dir.glob("*.log")]:
        if lp.name != "rip.log":
            try:
                log_text += lp.read_text(encoding="utf-8", errors="replace") + "\n"
            except OSError:
                pass
    accurip = [ln.strip() for ln in log_text.splitlines() if re.search(r"accurate|accurip", ln, re.I)][:40]
    disc = {"kind": "music", "drive": device, "drive_name": drive.get("drive_name", ""), "disc_label": label,
            "musicbrainz_discid": did, "toc": toc, "toc_string": toc_string(toc),
            "tracks": [{"n": k, "seconds": round(secs[k - 1], 2),
                        "file": f"rip/{have[k].name}" if k in have else ""} for k in range(1, n + 1)],
            "cdtext": cdtext, "accuraterip": accurip, "ripped_at": started.isoformat(timespec="seconds"),
            "status": status, "titles_saved": len(good), "titles_failed": failed,
            "finished_at": dt.datetime.now().isoformat(timespec="seconds")}
    if not cancelled:
        (out_dir / "disc_info.json").write_text(json.dumps(disc, indent=2), encoding="utf-8")
    mins = (dt.datetime.now() - started).seconds // 60
    rip_all.log(f"{device} [{label}] " + (f"DONE: {n} tracks in {mins} min" if status == "ok" else
                                          "cancelled" if cancelled else
                                          f"FINISHED WITH PROBLEMS: {failed} of {n} tracks missing (see {log_path})"))
    rip_all.append_history(cfg, {"finished": disc["finished_at"], "drive": device, "disc_label": label,
                                 "folder": str(out_dir), "titles_saved": len(good), "titles_failed": failed,
                                 "minutes": mins, "status": status})
    st.update(state="done" if status == "ok" else status, pct=100 if status == "ok" else st.get("pct", 0),
              message=(f"{n} tracks ripped in {mins} min" if status == "ok" else "cancelled" if cancelled
                       else f"{failed} of {n} tracks failed: check rip.log"), cancel=False)
    tool_error = bool(re.search(r"unknown option|usage:", tail, re.I))  # cyanrip refused its arguments
    if failed and not cancelled and not tool_error:
        rip_all.note_damaged(cfg, label, out_dir, 0, failed, [f"track {k}" for k in range(1, n + 1) if k not in good])
    if cfg.get("eject_when_done", False) and not cancelled:
        rip_all.eject(device)
    for cb in rip_all.ON_DONE:
        try:
            cb(disc, out_dir)
        except Exception as exc:
            rip_all.log(f"after-rip step failed: {exc}")
    return disc


# --------------------------------------------------------------------------- MusicBrainz

def _get(url: str, binary: bool = False, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        return (data, r.headers.get("Content-Type", "")) if binary else json.loads(data.decode("utf-8"))


_LAST_MB = [0.0]


def musicbrainz(did: str, toc: str) -> list[dict]:
    """Releases holding this disc. Falls back to a fuzzy match on the track layout."""
    wait = 1.1 - (time.time() - _LAST_MB[0])  # MusicBrainz allows one request a second
    if wait > 0:
        time.sleep(wait)
    _LAST_MB[0] = time.time()
    q = urllib.parse.urlencode({"toc": toc, "inc": "artist-credits labels recordings release-groups",
                                "fmt": "json", "cdstubs": "no"})
    try:
        data = _get(f"https://musicbrainz.org/ws/2/discid/{urllib.parse.quote(did)}?{q}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise
    return data.get("releases") or []


def credit(ac: list[dict] | None) -> str:
    return "".join((c.get("name") or (c.get("artist") or {}).get("name") or "") + (c.get("joinphrase") or "")
                   for c in ac or []).strip()


def _medium(rel: dict, did: str, n_tracks: int) -> tuple[dict | None, bool]:
    media = rel.get("media") or []
    for m in media:
        if any(d.get("id") == did for d in m.get("discs") or []):
            return m, True
    for m in media:
        if len(m.get("tracks") or []) == n_tracks and "cd" in (m.get("format") or "cd").lower():
            return m, False
    return None, False


def release_choices(releases: list[dict], did: str, n_tracks: int, cfg: dict) -> list[dict]:
    """Every usable release, most likely first. Each: {id, label, score, exact, medium position}."""
    pref = [c.strip().upper() for c in str(cfg.get("music_countries") or "US,XW,CA").split(",") if c.strip()]
    out = []
    for rel in releases:
        med, exact = _medium(rel, did, n_tracks)
        if not med:
            continue
        country = (rel.get("country") or "").upper()
        score = (100 if exact else 0) + (3 if (rel.get("status") or "Official") == "Official" else 0)
        if country in pref:
            score += 5 + (len(pref) - pref.index(country))
        if (med.get("format") or "").lower() == "cd":
            score += 2
        date = rel.get("date") or "9999"
        n_media = len(rel.get("media") or [])
        bits = [x for x in (date[:4] if date != "9999" else "", country,
                            f"CD {med.get('position')} of {n_media}" if n_media > 1 else "",
                            ", ".join(f"{(li.get('label') or {}).get('name', '')} {li.get('catalog-number') or ''}".strip()
                                      for li in rel.get("label-info") or [] if li.get("label")),
                            rel.get("disambiguation") or "") if x]
        out.append({"id": rel["id"], "score": score, "exact": exact, "date": date,
                    "label": f"{rel.get('title')} by {credit(rel.get('artist-credit'))}" + (f" ({'; '.join(bits)})" if bits else "")})
    out.sort(key=lambda c: (-c["score"], c["date"]))
    return out


def apply_release(plan: dict, rel: dict, did: str) -> None:
    """Fill album, artist and track names on a plan from one MusicBrainz release."""
    med, _ = _medium(rel, did, len(plan["items"]))
    rg = rel.get("release-group") or {}
    va = any((c.get("artist") or {}).get("id") == VARIOUS for c in rel.get("artist-credit") or [])
    year = (rg.get("first-release-date") or rel.get("date") or "")[:4]
    plan.update(release=rel["id"], album=rel.get("title") or "", album_artist="Various Artists" if va else credit(rel.get("artist-credit")),
                compilation=va, year=year, disc=int((med or {}).get("position") or 1),
                discs=len(rel.get("media") or []) or 1, title=rel.get("title") or "")
    li = next((x for x in rel.get("label-info") or [] if x.get("label")), {})
    plan["mb"] = {"albumid": rel["id"], "releasegroupid": rg.get("id", ""), "date": rel.get("date", ""),
                  "country": rel.get("country", ""), "barcode": rel.get("barcode") or "",
                  "label": (li.get("label") or {}).get("name", ""), "catalognumber": li.get("catalog-number") or "",
                  "albumartistids": [(c.get("artist") or {}).get("id", "") for c in rel.get("artist-credit") or []],
                  "media": (med or {}).get("format", ""), "status": rel.get("status") or "",
                  "type": rg.get("primary-type") or ""}
    tracks = (med or {}).get("tracks") or []
    for it, t in zip(plan["items"], tracks):
        rec = t.get("recording") or {}
        it.update(title=t.get("title") or rec.get("title") or it["title"],
                  artist=credit(t.get("artist-credit") or rec.get("artist-credit")) or plan["album_artist"],
                  track_id=t.get("id", ""), recording_id=rec.get("id", ""),
                  artist_ids=[(c.get("artist") or {}).get("id", "") for c in t.get("artist-credit") or rec.get("artist-credit") or []])


def mmss(s: float) -> str:
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}"


def music_root(cfg: dict) -> Path:
    if cfg.get("music_library_root"):
        return Path(cfg["music_library_root"])
    if not str(cfg.get("plex_library_root") or "").strip():
        return Path(paths.DATA_DIR) / "(folder not chosen yet)"
    lib = Path(cfg["plex_library_root"])
    try:  # an existing music folder, whatever its case or spacing ("music", "My Music" is not matched)
        found = next((d.name for d in sorted(lib.iterdir()) if d.is_dir() and d.name.replace(" ", "").lower() == "music"), None)
    except OSError:
        found = None
    return lib / (found or "Music")


def plan_folder(folder: Path, cfg: dict, version: int = 1) -> dict | None:
    disc = json.loads((folder / "disc_info.json").read_text(encoding="utf-8"))
    tracks = [t for t in disc.get("tracks") or [] if t.get("file")]
    if not tracks:
        return None
    did, warnings = disc["musicbrainz_discid"], []
    plan = {"kind": "music", "status": "proposed", "planner_version": version, "disc_label": disc["disc_label"],
            "disc_folder": str(folder), "discid": did, "format": "FLAC", "album": "", "album_artist": "",
            "title": "", "year": "", "disc": 1, "discs": 1, "compilation": False, "releases": [], "release": "",
            "release_sure": True, "dest_root": str(music_root(cfg)), "accuraterip": disc.get("accuraterip") or [],
            "warnings": warnings,
            "items": [{"source": t["file"], "track": t["n"], "duration_seconds": t["seconds"],
                       "duration": mmss(t["seconds"]), "title": f"Track {t['n']:02d}", "artist": "",
                       "action": "move", "role": "track"} for t in tracks]}
    if disc.get("titles_failed"):
        warnings.append(f"{disc['titles_failed']} track(s) didn't rip. See rip.log in the rip folder.")
    try:
        releases = musicbrainz(did, disc.get("toc_string") or toc_string(disc["toc"]))
        (folder / "musicbrainz.json").write_text(json.dumps(releases, indent=2), encoding="utf-8")
    except Exception as exc:
        releases = []
        warnings.append(f"Couldn't reach MusicBrainz ({exc}). Type the names in, or click Check for new rips later.")
    choices = release_choices(releases, did, len(disc.get("tracks") or []), cfg)
    if not choices:
        ct = disc.get("cdtext") or {}
        if ct.get("album") or ct.get("tracks"):
            plan.update(album=ct.get("album") or "Unknown Album", album_artist=ct.get("artist") or "Unknown Artist")
            plan["title"] = plan["album"]
            for it in plan["items"]:
                t = (ct.get("tracks") or {}).get(str(it["track"])) or (ct.get("tracks") or {}).get(it["track"]) or {}
                it["title"] = t.get("title") or it["title"]
                it["artist"] = t.get("artist") or plan["album_artist"]
            warnings.append("MusicBrainz doesn't know this CD, so it's named from the text stored on the CD "
                            "(CD-Text). Check the names and add the year.")
            join_album(plan, cfg)
            return plan
        if releases is not None and not any("Couldn't reach" in w for w in warnings):
            warnings.append("MusicBrainz doesn't know this CD. Type the album, artist and track names in.")
        plan.update(album="Unknown Album", album_artist="Unknown Artist", title="Unknown Album")
        return plan
    plan["releases"] = [{"id": c["id"], "label": c["label"]} for c in choices]
    best = next(r for r in releases if r["id"] == choices[0]["id"])
    apply_release(plan, best, did)
    # Not sure when another release that fits as well names the album or tracks differently
    names = lambda rel: (rel.get("title"), tuple((t.get("title") or "").lower() for t in (_medium(rel, did, len(plan["items"]))[0] or {}).get("tracks") or []))
    top = [c for c in choices if c["exact"] == choices[0]["exact"]]
    differ = {names(next(r for r in releases if r["id"] == c["id"])) for c in top}
    if len(differ) > 1:
        plan["release_sure"] = False
        warnings.append(f"This CD is on MusicBrainz as {len(top)} releases with different names. "
                        f"Using the first; pick another under Release if it's wrong.")
    if not choices[0]["exact"]:
        warnings.append("Matched by track lengths only (MusicBrainz doesn't list this exact CD), so check it.")
    join_album(plan, cfg)
    return plan


def join_album(plan: dict, cfg: dict) -> None:
    """Disc 2 of a box set goes in the same album folder as disc 1, with the same names,
    even if you changed the album or artist name when you filed disc 1."""
    for did, hit in _index(cfg).items():
        same = (plan.get("release") and hit.get("release") == plan["release"]) or (
            not plan.get("release") and hit.get("album", "").lower() == plan.get("album", "").lower()
            and hit.get("artist", "").lower() == plan.get("album_artist", "").lower())
        if did == plan.get("discid") or not same:
            continue
        plan.update(album=hit["album"], album_artist=hit["artist"], title=hit["album"],
                    compilation=bool(hit.get("compilation")), year=hit.get("year") or plan.get("year"))
        if hit.get("discs"):
            plan["discs"] = max(int(plan.get("discs") or 1), int(hit["discs"]))
        plan.setdefault("notes", []).append(f"Part of a set: joins disc {hit.get('disc')} already filed in {hit['folder']}")
        return


def change_release(folder: Path, plan: dict, rid: str) -> dict:
    releases = json.loads((folder / "musicbrainz.json").read_text(encoding="utf-8"))
    rel = next((r for r in releases if r["id"] == rid), None)
    if not rel:
        raise ValueError("release not found")
    apply_release(plan, rel, plan["discid"])
    plan["release_sure"] = True
    plan["warnings"] = [w for w in plan.get("warnings", []) if "releases with different names" not in w]
    return plan


# --------------------------------------------------------------------------- names

def fs_name(s: str) -> str:
    """Safe for Windows; keeps the name readable ("AC/DC" -> "AC-DC", "What?" -> "What")."""
    s = re.sub(r"\s*[/\\]\s*", "-", s or "")
    s = re.sub(r"\s*:\s*", " - ", s)
    s = re.sub(r'[<>"|?*\x00-\x1f]', "", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:120]


def album_dir(plan: dict, cfg: dict) -> Path:
    artist = "Various Artists" if plan.get("compilation") else plan.get("album_artist")
    return music_root(cfg) / fs_name(artist) / fs_name(plan.get("album"))


def track_name(plan: dict, it: dict) -> str:
    num = f"{int(plan.get('disc') or 1)}{int(it['track']):02d}" if int(plan.get("discs") or 1) > 1 else f"{int(it['track']):02d}"
    return f"{num} - {fs_name(it.get('title'))}.flac"


# --------------------------------------------------------------------------- FLAC tags

def _blocks(f) -> tuple[list[tuple[int, bytes]], int]:
    if f.read(4) != b"fLaC":
        raise ValueError("not a FLAC file")
    blocks = []
    while True:
        hdr = f.read(4)
        if len(hdr) < 4:
            raise ValueError("FLAC header cut short")
        last, typ, size = hdr[0] & 0x80, hdr[0] & 0x7F, int.from_bytes(hdr[1:4], "big")
        blocks.append((typ, f.read(size)))
        if last:
            return blocks, f.tell()


def read_tags(path: Path) -> list[tuple[str, str]]:
    with open(path, "rb") as f:
        blocks, _ = _blocks(f)
    for typ, data in blocks:
        if typ == 4:
            return _parse_comments(data)[1]
    return []


def _parse_comments(data: bytes) -> tuple[bytes, list[tuple[str, str]]]:
    n = struct.unpack("<I", data[:4])[0]
    vendor, pos = data[4:4 + n], 4 + n
    count, pos = struct.unpack("<I", data[pos:pos + 4])[0], pos + 4
    tags = []
    for _ in range(count):
        ln = struct.unpack("<I", data[pos:pos + 4])[0]
        kv = data[pos + 4:pos + 4 + ln].decode("utf-8", "replace")
        pos += 4 + ln
        if "=" in kv:
            k, v = kv.split("=", 1)
            tags.append((k.upper(), v))
    return vendor, tags


def _comments(vendor: bytes, tags: list[tuple[str, str]]) -> bytes:
    out = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(tags))
    for k, v in tags:
        kv = f"{k}={v}".encode("utf-8")
        out += struct.pack("<I", len(kv)) + kv
    return out


def _picture(img: bytes, mime: str) -> bytes:
    w = h = 0
    if img[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", img[16:24])
    m = mime.encode("ascii")
    return (struct.pack(">I", 3) + struct.pack(">I", len(m)) + m + struct.pack(">I", 0)
            + struct.pack(">IIII", w, h, 24, 0) + struct.pack(">I", len(img)) + img)


def write_flac(src: Path, dst: Path, tags: list[tuple[str, str]], cover: tuple[bytes, str] | None = None,
               job: dict | None = None) -> None:
    """Copy src to dst with these tags (others the ripper wrote, like ReplayGain, are kept)."""
    with open(src, "rb") as fi:
        blocks, audio_at = _blocks(fi)
        vendor, old = b"Shelfrip", []
        for typ, data in blocks:
            if typ == 4:
                vendor, old = _parse_comments(data)
        mine = {k for k, _ in tags}
        merged = [(k, v) for k, v in old if k not in mine and k not in ("LYRICS", "UNSYNCEDLYRICS")] + \
                 [(k, v) for k, v in tags if v not in (None, "")]
        keep = [(t, d) for t, d in blocks if t not in (1, 4) and not (t == 6 and cover)]
        keep.insert(1, (4, _comments(vendor, merged)))
        if cover:
            keep.append((6, _picture(*cover)))
        keep.append((1, b"\0" * 4096))  # padding, so tags can be edited later without rewriting
        paths.make_folder(dst.parent, "the music folder")
        tmp = dst.with_name(dst.name + ".partial")
        with open(tmp, "wb") as fo:
            fo.write(b"fLaC")
            for i, (t, d) in enumerate(keep):
                fo.write(bytes([t | (0x80 if i == len(keep) - 1 else 0)]) + len(d).to_bytes(3, "big") + d)
            head = fo.tell()
            fi.seek(audio_at)
            while True:
                buf = fi.read(4 * 1024 * 1024)
                if not buf:
                    break
                fo.write(buf)
                if job is not None:
                    job["done_bytes"] = job.get("done_bytes", 0) + len(buf)
    want = src.stat().st_size - audio_at
    if tmp.stat().st_size - head != want or flac_info(tmp).get("samples") != flac_info(src).get("samples"):
        tmp.unlink(missing_ok=True)
        raise IOError(f"copy check failed for {src.name}")
    os.replace(tmp, dst)


def tags_for(plan: dict, it: dict) -> list[tuple[str, str]]:
    mb = plan.get("mb") or {}
    n_tracks = len(plan["items"])
    t = [("TITLE", it.get("title", "")), ("ARTIST", it.get("artist") or plan.get("album_artist", "")),
         ("ALBUM", plan.get("album", "")),
         ("ALBUMARTIST", "Various Artists" if plan.get("compilation") else plan.get("album_artist", "")),
         ("TRACKNUMBER", str(it["track"])), ("TRACKTOTAL", str(n_tracks)), ("TOTALTRACKS", str(n_tracks)),
         ("DISCNUMBER", str(plan.get("disc") or 1)), ("DISCTOTAL", str(plan.get("discs") or 1)),
         ("TOTALDISCS", str(plan.get("discs") or 1)), ("DATE", str(plan.get("year") or "")),
         ("ORIGINALDATE", str(plan.get("year") or "")), ("RELEASEDATE", mb.get("date", "")),
         ("LABEL", mb.get("label", "")), ("CATALOGNUMBER", mb.get("catalognumber", "")),
         ("BARCODE", mb.get("barcode", "")), ("RELEASECOUNTRY", mb.get("country", "")),
         ("MEDIA", mb.get("media", "")), ("RELEASETYPE", (mb.get("type") or "").lower()),
         ("MUSICBRAINZ_ALBUMID", mb.get("albumid", "")), ("MUSICBRAINZ_RELEASEGROUPID", mb.get("releasegroupid", "")),
         ("MUSICBRAINZ_DISCID", plan.get("discid", "")),
         ("MUSICBRAINZ_TRACKID", it.get("recording_id", "")), ("MUSICBRAINZ_RELEASETRACKID", it.get("track_id", ""))]
    t += [("MUSICBRAINZ_ARTISTID", a) for a in it.get("artist_ids") or [] if a]
    t += [("MUSICBRAINZ_ALBUMARTISTID", a) for a in mb.get("albumartistids") or [] if a]
    if plan.get("compilation"):
        t.append(("COMPILATION", "1"))
    # typed in by hand (MusicBrainz didn't know the CD): no MusicBrainz ids to give
    if not plan.get("release"):
        t = [(k, v) for k, v in t if not k.startswith("MUSICBRAINZ_") or k == "MUSICBRAINZ_DISCID"]
    return t


# --------------------------------------------------------------------------- cover and lyrics

def cover_art(plan: dict) -> tuple[bytes, str] | None:
    mb = plan.get("mb") or {}
    for kind, mid in (("release", mb.get("albumid")), ("release-group", mb.get("releasegroupid"))):
        if not mid:
            continue
        try:
            data, ctype = _get(f"https://coverartarchive.org/{kind}/{mid}/front-1200", binary=True, timeout=60)
            if data[:3] == b"\xff\xd8\xff":
                return data, "image/jpeg"
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                return data, "image/png"
        except Exception:
            continue
    return None


def lyrics(plan: dict, it: dict) -> tuple[str, str] | None:
    """('lrc', text) for timed lyrics, ('txt', text) for plain ones, from LRCLIB, else None."""
    q = urllib.parse.urlencode({"track_name": it.get("title", ""), "artist_name": it.get("artist") or plan.get("album_artist", ""),
                                "album_name": plan.get("album", ""), "duration": int(round(it.get("duration_seconds") or 0))})
    try:
        d = _get(f"https://lrclib.net/api/get?{q}", timeout=20)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    if d.get("instrumental"):
        return None
    if (d.get("syncedLyrics") or "").strip():
        return "lrc", d["syncedLyrics"]
    if (d.get("plainLyrics") or "").strip():
        return "txt", d["plainLyrics"]
    return None


# --------------------------------------------------------------------------- filing

INDEX_NAME = "music_index.json"


def _index(cfg: dict, strict: bool = False) -> dict:
    """Filed CDs by disc id (folder, album, artist), from _Filed\\music_index.json.

    With strict=True (before adding to it) a file that's there but unreadable is first copied to
    music_index.broken.json, so writing the new entry can't silently drop every CD filed before.
    """
    path = Path(cfg["output_root"]) / "_Filed" / INDEX_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        if strict:
            try:
                shutil.copy2(path, path.with_name("music_index.broken.json"))
            except OSError:
                pass
        return {}


def already_filed(label: str, cfg: dict) -> str:
    """A sentence if this CD (by disc id) has been filed before, else ''."""
    did = label[len(LABEL_PREFIX):] if label.startswith(LABEL_PREFIX) else ""
    hit = _index(cfg).get(did) if did else None
    if hit and Path(hit["folder"]).is_dir():
        return f"This CD is already in your music library ({hit['album']} by {hit['artist']}), so it wasn't ripped."
    return ""


def _norm(s: str) -> str:
    """Loose form of an artist or album name for matching folders and Plex titles."""
    s = (s or "").lower().replace("\u2019", "'").replace("&", " and ")
    s = re.sub(r"\s*[\(\[][^)\]]*(remaster|edition|deluxe|expanded|anniversary|bonus|disc|cd|version)[^)\]]*[\)\]]", "", s)
    s = re.sub(r"^the\s+", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def _same(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    # "Chicago IX: Chicago's Greatest Hits" vs "Chicago's Greatest Hits" vs "Chicago IX"
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 6 and short in long_


def _names_for(did: str, toc: dict) -> list[tuple[str, str]]:
    """(album artist, album) pairs MusicBrainz has for this disc."""
    try:
        rels = musicbrainz(did, toc_string(toc))
    except Exception:
        return []
    out = []
    for rel in rels:
        pair = (credit(rel.get("artist-credit")), rel.get("title") or "")
        if pair[1] and pair not in out:
            out.append(pair)
    return out


def _in_folders(pairs: list[tuple[str, str]], cfg: dict) -> str:
    root = music_root(cfg)
    try:
        artists = [a for a in root.iterdir() if a.is_dir()]
    except OSError:
        return ""
    for artist, album in pairs:
        for a in artists:
            if a.name.lower() != "various artists" and not _same(a.name, artist):
                continue
            try:
                for alb in a.iterdir():
                    if alb.is_dir() and _same(alb.name, album) and any(alb.iterdir()):
                        return str(alb)
            except OSError:
                continue
    return ""


def _in_plex(pairs: list[tuple[str, str]], cfg: dict) -> str:
    try:
        import plex
        px = plex.from_config(cfg)
        if not px:
            return ""
        for sec in px.sections():
            if sec.get("type") != "artist":
                continue
            for artist, album in pairs:
                for q in {album, re.split(r"\s*[:(\[-]\s*", album)[0]}:
                    if len(q) < 3:
                        continue
                    found = px.get(f"/library/sections/{sec['key']}/all", type=9, title=q).get("Metadata", [])
                    for m in found:
                        if _same(m.get("title", ""), album) and (
                                _same(m.get("parentTitle", ""), artist) or m.get("parentTitle") == "Various Artists"):
                            return f"{m.get('title')} by {m.get('parentTitle')}"
    except Exception:
        return ""
    return ""


def in_library(d: dict, cfg: dict) -> str:
    """Before ripping: a sentence if this CD's album is already in the music library or Plex, else ''."""
    why = already_filed(d.get("disc_label") or "", cfg)
    if why or not d.get("toc"):
        return why
    did = d["disc_label"][len(LABEL_PREFIX):] if d["disc_label"].startswith(LABEL_PREFIX) else disc_id(d["toc"])
    pairs = _names_for(did, d["toc"])
    if not pairs:
        return ""
    where = _in_folders(pairs, cfg)
    if where:
        return f"{pairs[0][1]} by {pairs[0][0]} is already in your music library ({where}), so it wasn't ripped."
    hit = _in_plex(pairs, cfg)
    if hit:
        return f"Plex already has {hit}, so this CD wasn't ripped."
    return ""


def check_plan(folder: Path, plan: dict, cfg: dict) -> list[str]:
    problems, seen = [], set()
    if not (plan.get("album") or "").strip() or not (plan.get("album_artist") or plan.get("compilation")):
        problems.append("Fill in the album and album artist.")
        return problems
    d = album_dir(plan, cfg)
    for it in plan["items"]:
        if it.get("action") != "move":
            continue
        if not fs_name(it.get("title")):
            problems.append(f"Track {it['track']} has no title.")
            continue
        if not (folder / it["source"]).exists():
            problems.append(f"Track {it['track']} is missing from the rip folder.")
        dst = d / track_name(plan, it)
        if dst in seen:
            problems.append(f"Two tracks would both be named {dst.name}.")
        seen.add(dst)
        if dst.exists():
            problems.append(f"Already in the library: {dst}")
    return problems


def file_job(folder: Path, plan: dict, job: dict, cfg: dict, log=print) -> list[str]:
    """Write each kept track into the library with tags, plus cover and lyrics. Returns the files filed."""
    d = album_dir(plan, cfg)
    moves = [it for it in plan["items"] if it.get("action") == "move"]
    job["total_bytes"] = sum((folder / it["source"]).stat().st_size for it in moves)
    job["current"] = "getting cover art"
    cover = cover_art(plan) if plan.get("release") else None
    d.mkdir(parents=True, exist_ok=True)
    if cover and not any((d / n).exists() for n in ("cover.jpg", "cover.png", "folder.jpg")):
        (d / ("cover.png" if cover[1] == "image/png" else "cover.jpg")).write_bytes(cover[0])
    filed, no_lyrics = [], 0
    for i, it in enumerate(moves, 1):
        dst = d / track_name(plan, it)
        job["current"] = f"{i}/{len(moves)}: {dst.name}"
        write_flac(folder / it["source"], dst, tags_for(plan, it), cover, job)
        it["filed_to"] = str(dst)
        filed.append(str(dst))
        if cfg.get("music_lyrics", True):
            try:
                ly = lyrics(plan, it)
            except Exception as exc:
                ly = None
                log(f"lyrics lookup failed for {dst.name}: {exc}")
            if ly:
                dst.with_suffix("." + ly[0]).write_text(ly[1], encoding="utf-8")
                it["lyrics"] = ly[0]
            else:
                no_lyrics += 1
    notes = []
    if cfg.get("music_lyrics", True):
        notes.append(f"lyrics for {len(moves) - no_lyrics} of {len(moves)} tracks")
    if plan.get("release"):
        notes.append("cover art added" if cover else "no cover art found")
    job["note"] = "; ".join(x for x in (job.get("note"), ", ".join(notes)) if x)
    # remember the disc, so the app knows it's in the library next time it's inserted
    idx_path = Path(cfg["output_root"]) / "_Filed" / INDEX_NAME
    idx = _index(cfg, strict=True)
    idx[plan["discid"]] = {"folder": str(d), "album": plan.get("album"), "artist": plan.get("album_artist"),
                           "disc": plan.get("disc"), "discs": plan.get("discs"), "release": plan.get("release", ""),
                           "compilation": bool(plan.get("compilation")), "year": plan.get("year", ""),
                           "filed_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = idx_path.with_name(INDEX_NAME + ".tmp")  # write then rename, so a crash can't leave half a file
    tmp.write_text(json.dumps(idx, indent=2), encoding="utf-8")
    tmp.replace(idx_path)
    return filed


def plex_scan(cfg: dict, folder: Path, status: dict) -> None:
    """Ask Plex to scan the new album folder in its Music library."""
    import plex
    px = plex.from_config(cfg)
    if not px:
        status.update(state="off", summary="Plex check is off (add plex_url and plex_token in Settings)")
        return
    try:
        mapped = plex.map_folder(px.sections(), str(folder), "music")
        if not mapped:
            status.update(state="error", summary=f"Plex has no Music library that holds {folder}. "
                                                 f"Add {music_root(cfg)} to Plex as a Music library.")
            return
        px.refresh(*mapped)
        status.update(state="done", summary="Plex is scanning the album")
    except Exception as exc:
        status.update(state="error", summary=f"Plex scan failed ({exc})")
