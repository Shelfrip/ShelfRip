r"""
app.py - Shelfrip: one app for ripping, naming, reviewing and filing.

Double-click Shelfrip.exe (installed), or "Run Shelfrip.bat" in a copy run from the scripts. It opens one
page in your browser
(http://localhost:8765, only reachable from this PC) with three tabs:

  Drives    what's in each drive and how far each rip has got. Auto-rip rips every disc
            as soon as you insert it; "Rip loaded discs now" rips what's in the drives once.
  Review    the review page: check names, then file into Plex. New rips are named here
            automatically as soon as they finish.
  Settings  folders, MakeMKV/MKVToolNix locations, Plex address, and your keys (keys are
            never shown, only whether they're set).

Keep the app's window open while it works (it's minimized to the taskbar). Use Quit on the
page, or close that window, to stop it.

Only the Python standard library is used.
"""

from __future__ import annotations

import json
from pathlib import Path
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import desktop
import health
import tidy
import progress  # noqa: F401  (bundled into the exe)
import trouble
import identify
import naming
import paths
import review
import rip_all

JOB_DEFAULTS = {"auto_file": True, "auto_file_minutes": 15, "audio_language": "eng", "subtitle_language": "",
                "forced_subtitles": True, "retry_failed_titles": True, "job_total_discs": 0,
                "min_free_gb_rips": 60, "app_window": True, "tray_icon": True, "sounds": True,
                "skip_clutter": True, "skip_duplicate_features": False, "offer_error_reports": True}
SECRETS = ("tmdb_api_key", "omdb_api_key", "plex_token")
SETTINGS = [  # (key, label, kind)
    ("output_root", "Rip folder (where discs are ripped to before review)", "text"),
    ("plex_library_root", "Media library folder (holds your Movies and TV Shows folders)", "text"),
    ("movies_folder", "Movies folder (inside the media library or anywhere; empty = find it, else Movies)", "text"),
    ("tv_folder", "TV shows folder (inside the media library or anywhere; empty = find it, else TV Shows)", "text"),
    ("music_library_root", "Music folder (empty = the music folder in the media library, else Music)", "text"),
    ("media_server", "Media server (decides how files are named)", "select"),
    ("extras_style", "How special features are filed", "select"),
    ("format_folders", "Separate folders per format (Movies\\Blu-Ray\\..., Movies\\DVD\\...)", "bool"),
    ("use_dvdcompare", "Also look up extras names on DVDCompare (reads its web pages)", "bool"),
    ("use_community", "Look discs up in the shared Shelfrip disc database", "bool"),
    ("share_discs", "Share what I file with the Shelfrip disc database (anonymous: disc fingerprint, title lengths and names only)", "bool"),
    ("makemkvcon_path", "MakeMKV (makemkvcon64.exe)", "text"),
    ("mkvtoolnix_dir", "MKVToolNix folder", "text"),
    ("plex_url", "Plex address (Plex only, optional: lets the app check what Plex matched)", "text"),
    ("cyanrip_path", "cyanrip for music CDs (empty = tools\\cyanrip\\cyanrip.exe)", "text"),
    ("cd_offsets", "CD drive read offsets, e.g. D:6, E:667 (for AccurateRip)", "text"),
    ("music_lyrics", "Save lyrics next to music tracks (.lrc / .txt, from LRCLIB)", "bool"),
    ("min_length_seconds", "Skip titles shorter than (seconds)", "number"),
    ("skip_extras_under_seconds", "Set extras shorter than this to Skip (seconds)", "number"),
    ("skip_clutter", "Set menus, warnings, promos, trailers for other films, unnamed short clips and Play All titles to Skip", "bool"),
    ("watch_poll_seconds", "Check drives every (seconds)", "number"),
    ("eject_after_filing", "Eject each disc once you've filed (or discarded) it", "bool"),
    ("eject_when_done", "Eject each disc as soon as its rip finishes instead", "bool"),
    ("check_plex_before_rip", "Don't rip discs your library already has in the same or better quality", "bool"),
    ("notifications", "Windows notifications when a rip finishes", "bool"),
    ("phone_notify_url", "Phone alerts: ntfy.sh topic address (optional, e.g. https://ntfy.sh/your-topic)", "text"),
    ("auto_rip", "Turn auto-rip on when the app opens", "bool"),
    ("auto_file", "File discs automatically when everything matched with confidence", "bool"),
    ("auto_file_minutes", "…after waiting this many minutes (so you can step in)", "number"),
    ("audio_language", "Default audio language (e.g. eng, jpn)", "text"),
    ("subtitle_language", "Turn on full subtitles in this language (empty = off)", "text"),
    ("forced_subtitles", "Show forced subtitles (foreign-dialogue lines) automatically", "bool"),
    ("retry_failed_titles", "Retry titles that hit read errors once", "bool"),
    ("offer_error_reports", "When something fails, offer to send an error report", "bool"),
    ("skip_duplicate_features", "Rip only one copy of a movie disc's feature (no duplicate copies, no scene-by-scene titles)", "bool"),
    ("job_total_discs", "Discs you plan to rip, for a progress bar (0 = no goal)", "number"),
    ("min_free_gb_rips", "Don't start a rip with less than this free in the rip folder (GB)", "number"),
    ("tmdb_api_key", "TMDB key", "secret"),
    ("omdb_api_key", "OMDb key", "secret"),
    ("plex_token", "Plex token (Plex only, optional)", "secret"),
    ("check_updates", "Tell me when a new version of Shelfrip is out", "bool"),
    ("sounds", "Play sounds (a chime when a disc is filed, a fanfare when you level up)", "bool"),
    ("app_window", "Open Shelfrip in its own window (not a browser tab)", "bool"),
    ("tray_icon", "Keep Shelfrip running by the clock when its window is closed (Quit from the icon's menu)", "bool"),
]
CHOICES = {"media_server": [{"value": k, "label": v} for k, v in naming.SERVERS.items()],
           "extras_style": [{"value": "folders", "label": "In folders: Featurettes\\Making Of.mkv"},
                            {"value": "suffix", "label": "Tagged in the name: Making Of-featurette.mkv"}]}


# --------------------------------------------------------------------------- alerts

def notify(title: str, text: str) -> None:
    """A Windows notification (and, if set, a phone push via ntfy.sh or similar). Never blocks."""
    cfg = _read_config()
    if not cfg.get("notifications", True):
        return

    def run():
        if TRAY and TRAY.notify(title, text):
            pass
        elif os.name == "nt":
            esc = lambda x: str(x).replace("'", "''").replace("&", "and").replace("<", "").replace(">", "")
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
                "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                f"$t = $x.GetElementsByTagName('text'); $t.Item(0).AppendChild($x.CreateTextNode('{esc(title)}')) > $null;"
                f"$t.Item(1).AppendChild($x.CreateTextNode('{esc(text)}')) > $null;"
                "$n = [Windows.UI.Notifications.ToastNotification]::new($x);"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($n)")
            try:
                import subprocess
                subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=30,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:
                pass
        url = (cfg.get("phone_notify_url") or "").strip()
        if url.startswith("https://"):
            try:
                req = urllib.request.Request(url, data=text.encode("utf-8"), method="POST",
                                             headers={"Title": title.encode("ascii", "ignore").decode()})
                urllib.request.urlopen(req, timeout=15).close()
            except Exception:
                pass
    threading.Thread(target=run, daemon=True).start()


# --------------------------------------------------------------------------- before ripping

def disc_kind(root: str) -> str:
    """'dvd', 'bluray' or '4k' from the disc's folders (UHD discs use index.bdmv version 0300)."""
    from pathlib import Path as _P
    try:
        idx = _P(root) / "BDMV" / "index.bdmv"
        if idx.is_file():
            return "4k" if idx.read_bytes()[:8] == b"INDX0300" else "bluray"
        if (_P(root) / "VIDEO_TS").is_dir():
            return "dvd"
    except OSError:
        pass
    return ""


def identify_disc(dev: str, label: str, cfg: dict) -> dict | None:
    """What a disc is, before ripping it: from your catalog or TheDiscDB (by fingerprint), else
    from a label you've filed before. {kind, title, year, tmdb_id, episodes: [(season, ep)], how}"""
    import catalog
    import discdb
    fp = discdb.fingerprint(dev + "\\") if len(dev) == 2 and dev[1] == ":" else None
    h = (fp or {}).get("content_hash")
    if h and catalog.DB_PATH.exists():
        con = catalog.connect()
        try:
            row = con.execute("SELECT * FROM discs WHERE content_hash = ? ORDER BY filed_at DESC LIMIT 1", (h,)).fetchone()
            if row:
                eps = [(r["season"], e) for r in con.execute(
                    "SELECT * FROM titles WHERE disc_id = ? AND kept = 1 AND role = 'episode'", (row["id"],))
                    for e in range(r["episode"], (r["episode_last"] or r["episode"]) + 1) if r["episode"] is not None]
                return {"kind": row["kind"], "title": row["title"], "year": row["year"], "tmdb_id": row["tmdb_id"],
                        "episodes": eps, "how": "your catalog"}
        finally:
            con.close()
    if h and cfg.get("use_thediscdb", True):
        try:
            for item in discdb._query(discdb.Q_HASH, {"h": h}, f"tdbhash:{h}"):
                for rel in item.get("releases") or []:
                    for d in rel.get("discs") or []:
                        if (d.get("contentHash") or "").upper() != h:
                            continue
                        kind = "tv" if (item.get("type") or "").lower() in ("series", "tv", "show") else "movie"
                        eps = [(int(t["season"]), int(t["episode"])) for t in d.get("titles") or []
                               if (t.get("itemType") or "").lower() == "episode"
                               and str(t.get("season")).isdigit() and str(t.get("episode")).isdigit()]
                        return {"kind": kind, "title": item.get("title"), "year": str(item.get("year") or ""),
                                "tmdb_id": str((item.get("externalids") or {}).get("tmdb") or ""),
                                "episodes": eps, "how": "TheDiscDB"}
        except Exception:
            pass
    prev = discdb.learned(label)
    if prev and prev.get("kind") == "movie" and prev.get("tmdb_id"):
        return {"kind": "movie", "title": prev.get("title"), "year": str(prev.get("year") or ""),
                "tmdb_id": str(prev["tmdb_id"]), "episodes": [], "how": "a label you've filed before"}
    return None


def in_plex_already(dev: str, label: str, cfg: dict, ident: dict | None = None) -> str:
    """A sentence saying Plex already has this disc in the same or better quality, else ''."""
    kind = disc_kind(dev + "\\")
    ident = ident if ident is not None else identify_disc(dev, label, cfg)
    if not ident or not kind:
        return ""
    full = {**identify.DEFAULTS, **cfg}
    fmt = (full["tv_format_folders"] if ident["kind"] == "tv" else full["movie_format_folders"]).get(kind, "")
    plan = {"kind": ident["kind"], "title": ident["title"], "year": ident["year"], "tmdb_id": ident["tmdb_id"],
            "format": fmt, "items": []}
    if ident["kind"] == "tv":
        if not ident["episodes"]:
            return ""
        plan["items"] = [{"source": f"{s}x{e}", "role": "episode", "season": s, "episodes": [e]}
                         for s, e in ident["episodes"]]
    else:
        plan["items"] = [{"source": "main", "role": "main", "name": ""}]
    mine = identify.quality_rank(fmt, full)
    copies = identify.library_copies(plan, review.library(), full)
    have = [max(c, key=lambda h: h["rank"]) for c in copies.values()]
    if not mine or len(copies) < len(plan["items"]) or any(h["rank"] < mine for h in have):
        return ""
    best = max(have, key=lambda h: h["rank"])
    what = f"{ident['title']}" + (f" ({ident['year']})" if ident.get("year") else "")
    if ident["kind"] == "tv":
        eps = sorted(ident["episodes"])
        what += f" s{eps[0][0]:02d}e{eps[0][1]:02d}-e{eps[-1][1]:02d}"
    return (f"Plex already has {what} as {best['format']}"
            f"{' (better quality)' if best['rank'] > mine else ''}, so it wasn't ripped "
            f"(recognised from {ident['how']}).")


# --------------------------------------------------------------------------- ripping

class Ripper:
    """Watches the drives and runs rips (the same code as rip_all.py --watch)."""

    def __init__(self) -> None:
        self.cfg: dict | None = None
        self.error = ""
        self.drives: list[dict] = []
        self.busy: set[str] = set()
        self.finished: dict[str, str] = {}
        self.wake = threading.Event()
        self.rip_once = False
        self.force: set[str] = set()
        self.held: dict[str, str] = {}   # device -> label held back because Plex has it
        self.match: dict[str, dict] = {}  # device -> what its disc was recognised as before ripping
        raw = _read_config()
        self.auto = bool(raw.get("auto_rip", False))
        self.watch = health.Watchdog(notify=notify)
        self._watched = 0.0
        self._upkeep = 0.0
        self._told_folder = ""
        self.beat = time.time()
        rip_all.ON_DONE.append(self.after_rip)
        rip_all.ON_LOW_SPACE.append(lambda why: notify("Rip folder is full", why))
        rip_all.ON_ERROR.append(lambda dev, label, err: trouble.record("Rip failed", label, err, f"drive {dev}", with_trace=True))
        threading.Thread(target=self.loop, daemon=True).start()

    def load(self) -> None:
        """Re-read settings each loop, so changes in Settings apply without a restart. A setup problem
        (no rip folder, no MakeMKV) is kept in self.error for the Home page instead of raising."""
        try:
            self.cfg = rip_all.load_config(exit_on_error=False)
            self.error = ""
            from pathlib import Path
            rip_all.LOG_FILE = Path(self.cfg["output_root"]) / "disc_ripper.log"
        except rip_all.SetupError as exc:
            self.cfg, self.error = None, str(exc)
        except Exception as exc:
            self.cfg, self.error = None, f"Couldn't read the settings: {exc}"

    def loop(self) -> None:
        """The background loop: watch the drives, start a rip for each new disc (when auto-rip is on
        or you clicked Rip), plan finished rips for review, and do the hourly upkeep."""
        while True:
            self.beat = time.time()
            self.load()
            if self.cfg:
                try:
                    paths.make_folder(self.cfg["output_root"], "the rip folder")
                except OSError as exc:
                    self.error = str(exc)
                    if self._told_folder != self.error:  # once per problem, not every few seconds
                        self._told_folder = self.error
                        rip_all.log(self.error)
                        trouble.record("Couldn't create the rip folder", "", self.error)
                    self.wake.wait(max(15, int(self.cfg.get("watch_poll_seconds") or 15)))
                    self.wake.clear()
                    continue
                self._told_folder = ""
                try:
                    drives = rip_all.scan_drives(self.cfg)
                    for d in drives:
                        d["locked_elsewhere"] = (d["device"] not in self.busy and d["loaded"]
                                                 and rip_all.drive_locked(self.cfg, d["device"]))
                    self.drives = drives
                    try:
                        for t in review.auto_file_tick(log=rip_all.log):
                            notify("Filed automatically", f"{t} matched with confidence and is being filed into your library.")
                    except Exception as exc:
                        rip_all.log(f"auto-file check failed: {exc}")
                    if time.time() - self._watched > 60:  # stuck rips
                        self._watched = time.time()
                        try:
                            from pathlib import Path as _P
                            self.watch.check(self.busy, rip_all.STATUS, _P(self.cfg["output_root"]))
                        except Exception as exc:
                            rip_all.log(f"stuck-rip check failed: {exc}")
                    try:
                        for t in review.resume_tick(log=rip_all.log):
                            notify("Finishing a filing", f"{t}: picking up the filing that was interrupted.")
                    except Exception as exc:
                        rip_all.log(f"resume check failed: {exc}")
                    if time.time() - self._upkeep > 600:  # every 10 minutes
                        self._upkeep = time.time()
                        try:  # discs that couldn't be named (say TMDB was unreachable): try again
                            if review.unnamed_folders():
                                n = review.plan_new_folders()
                                if n:
                                    rip_all.log(f"Named {n} disc(s) that couldn't be named before")
                        except Exception as exc:
                            rip_all.log(f"naming retry failed: {exc}")
                        try:
                            made = health.backup(paths.DATA_DIR, Path(self.cfg["output_root"]))
                            if made:
                                rip_all.log(f"Backed up your Shelfrip data: {made.name}")
                        except Exception as exc:
                            rip_all.log(f"backup failed: {exc}")
                    try:
                        weekly_recap()
                    except Exception as exc:
                        rip_all.log(f"weekly recap failed: {exc}")
                    if self.auto or self.rip_once:
                        self.rip_once = False
                        force, self.force = self.force, set()
                        rip_all.rip_loaded(self.cfg, self.busy, self.finished, notify_locked=False,
                                           force=force, skip=self.check_first)
                except Exception as exc:
                    self.error = f"Couldn't check the drives: {exc}"
                    trouble.record("Couldn't check the drives", "", str(exc), with_trace=True)
            poll = int((self.cfg or {}).get("watch_poll_seconds") or 15)
            self.wake.wait(max(5, poll))
            self.wake.clear()

    def check_first(self, d: dict) -> bool:
        """Before ripping: hold the disc back if Plex already has it (same or better quality)."""
        ident = None if d.get("audio") else self.recognise(d)
        if not self.cfg.get("check_plex_before_rip", True):
            return False
        if d.get("audio"):  # music CD: filed before (by its disc id)?
            import music
            try:
                why = music.in_library(d, {**identify.DEFAULTS, **self.cfg})
            except Exception as exc:
                rip_all.log(f"{d['device']} [{d['disc_label']}] couldn't check the music library first ({exc})")
                why = ""
            if why:
                self.finished[d["device"]] = d["disc_label"]
                self.held[d["device"]] = d["disc_label"]
                rip_all.STATUS[d["device"]] = {"label": d["disc_label"], "state": "in_plex", "message": why}
                rip_all.log(f"{d['device']} [{d['disc_label']}] {why}")
                notify("Already in Plex", f"{why} Swap the disc, or click Rip anyway.")
            return bool(why)
        try:
            why = in_plex_already(d["device"], d["disc_label"], self.cfg, ident=ident)
        except Exception as exc:
            rip_all.log(f"{d['device']} [{d['disc_label']}] couldn't check Plex first ({exc})")
            return False
        if why:
            self.finished[d["device"]] = d["disc_label"]
            self.held[d["device"]] = d["disc_label"]
            rip_all.STATUS[d["device"]] = {"label": d["disc_label"], "state": "in_plex", "message": why}
            rip_all.log(f"{d['device']} [{d['disc_label']}] {why}")
            notify("Already in Plex", f"{d['disc_label']}: {why} Swap the disc, or click Rip anyway.")
            return True
        return False

    def recognise(self, d: dict) -> dict | None:
        """What the disc in this drive is (before ripping), remembered so the page can show its box art."""
        try:
            ident = identify_disc(d["device"], d["disc_label"], self.cfg)
        except Exception:
            ident = None
        if ident:
            eps = ident.get("episodes") or []
            self.match[d["device"]] = {"label": d["disc_label"], "kind": ident["kind"], "title": ident.get("title") or "",
                                       "year": ident.get("year") or "", "tmdb_id": ident.get("tmdb_id") or "",
                                       "season": eps[0][0] if eps else None, "how": ident.get("how") or ""}
        return ident or {}  # {} = looked, not recognised (in_plex_already won't look again)

    def after_rip(self, disc: dict, out_dir) -> None:
        """Name the new rip straight away so it's waiting on the Review tab."""
        if not disc:
            return
        label, dev = disc.get("disc_label"), disc.get("drive")
        try:
            n = review.plan_new_folders()
            saved = disc.get("titles_saved", 0) if disc.get("status") != "ok" else None
            rip_all.log(f"{label}: named, ready to review" if n else
                        f"{label}: nothing was saved" if saved == 0 else f"{label}: ripped")
        except Exception as exc:
            rip_all.log(f"{label}: couldn't name it ({exc})")
        ok = disc.get("status") == "ok"
        if not ok and disc.get("status") != "cancelled":
            trouble.record("Rip didn't finish", label or "",
                           f"{disc.get('titles_saved', 0)} titles saved, {disc.get('titles_failed', 0)} failed"
                           + (f". MakeMKV said: {disc['makemkv_error']}" if disc.get("makemkv_error") else ""),
                           f"drive {dev}; read errors: {disc.get('read_errors', 0)}; "
                           f"damaged files: {', '.join(disc.get('damaged_files') or []) or 'none'}")
        notify(f"{dev} finished" if ok else f"{dev} needs a look",
               f"{label} is ripped and waiting in Review. Check the names, then file it to eject the disc."
               if ok else f"{label}: {disc['makemkv_error']}" if disc.get("drive_lost")
               else f"{label} finished with problems ({disc.get('titles_failed', 0)} titles failed).")

    def state(self) -> dict:
        """Everything the Home page polls for: each drive with its rip progress and filing status,
        setup problems, queue stats, errors to offer reporting, and the latest log lines."""
        drives = []
        for d in self.drives:
            st = dict(rip_all.STATUS.get(d["device"], {}))
            st.pop("cancel", None)
            ripping = d["device"] in self.busy
            if not ripping and st.get("state") in ("scanning", "ripping"):
                st["state"] = "idle"
            drives.append({"device": d["device"], "loaded": d["loaded"], "label": d["disc_label"],
                           "ripping": ripping, "elsewhere": d.get("locked_elsewhere", False),
                           "already": self.finished.get(d["device"]) == d["disc_label"] and not ripping,
                           **{k: v for k, v in st.items() if k != "label"},
                           "rip_label": st.get("label", "")})
        try:
            plans = review.list_plans()
        except Exception:
            plans = []
        waiting = len(plans)
        by_disc = {p.get("disc_label"): p for p in plans}
        for d in drives:  # what's in each drive, for its box art
            p = by_disc.get(d["label"]) if d["label"] else None
            m = self.match.get(d["device"])
            if p and p.get("kind") in ("movie", "tv", "music"):
                d["match"] = {"kind": p["kind"], "title": p.get("album") if p["kind"] == "music" else p.get("title"),
                              "year": p.get("year") or "", "tmdb_id": str(p.get("tmdb_id") or ""),
                              "season": p.get("season"), "folder": p["folder"], "how": "named for review"}
            elif m and m.get("label") == d["label"]:
                d["match"] = m
            fs = review.filing_status(p) if p else None
            if fs:
                d["filing"] = fs
            elif d["label"] and not p and d["already"] and review.filed_label(d["label"]):
                d["filed"] = True  # filed, and the disc is still in the drive (eject after filing is off)
        try:
            setup = setup_check()
        except Exception:
            setup = []
        active = rips_in_progress(review.RIP_ROOT)
        by_label = {a["label"]: a for a in active}
        for d in drives:  # attach folder-based progress (and speed / time left) to each drive
            a = by_label.get(rip_all.safe_folder_name(d["label"] or ""))
            if a and (d["ripping"] or d["elsewhere"]):
                want = d.get("bytes_total") or 0
                if want and d["ripping"]:  # only what this rip saves (not titles left out, or already done)
                    got = max(0, a["done"] - (d.get("bytes_base") or 0))  # files finished before a resume/retry don't count
                    a = {**a, "total": want, "pct": min(99, int(100 * got / want)),
                         "eta_s": int((want - got) / (a["mb_s"] * 1e6)) if a["mb_s"] and want > got else None}
                d["progress"] = a
        try:
            stats = job_stats(waiting, len(self.busy))
        except Exception as exc:
            stats = {"error": str(exc)}
        filing = sum(1 for jb in review.JOBS.values() if jb.get("state") == "running")
        if time.time() - _LEFT["t"] > 20:
            _LEFT.update(t=time.time(), n=len(health.leftovers()), gui=health.makemkv_window_open())
        offer = {**JOB_DEFAULTS, **_read_config()}.get("offer_error_reports", True)
        return {"failures": trouble.pending() if offer else [], "leftover_makemkv": _LEFT["n"], "makemkv_open": _LEFT["gui"], "auto": self.auto, "error": self.error, "drives": drives, "stats": stats, "filing": filing, "waiting": waiting, "setup": setup, "active": active,
                "log": list(rip_all.LOG_LINES)[-40:]}


RIPPER: Ripper | None = None
_LEFT = {"t": 0.0, "n": 0, "gui": False}


def weekly_recap() -> None:
    """Sunday evening: a notification with your week (once a week, only if you filed something)."""
    import datetime as _dt
    now = _dt.datetime.now()
    if now.weekday() != 6 or now.hour < 18:
        return
    state_path = paths.DATA_DIR / "recap.json"
    try:
        sent = json.loads(state_path.read_text(encoding="utf-8")).get("sent")
    except Exception:
        sent = None
    week = (now.date() - _dt.timedelta(days=6)).isoformat()
    if sent == week:
        return
    cfg = {**rip_all.DEFAULT_CONFIG, **JOB_DEFAULTS, **_read_config()}
    if not cfg.get("output_root"):
        return
    w = progress.compute(Path(cfg["output_root"]), int(cfg.get("job_total_discs") or 0))["week"]["this"]
    state_path.write_text(json.dumps({"sent": week}), encoding="utf-8")
    if w["discs"]:
        last = progress.compute(Path(cfg["output_root"]), int(cfg.get("job_total_discs") or 0))["week"]["last"]
        more = f" That's {w['discs'] - last['discs']} more than last week." if w["discs"] > last["discs"] else ""
        notify("Your week on the shelf", f"You filed {w['discs']} disc{'s' if w['discs'] != 1 else ''}: "
               f"{w['hours']:g} hours and {w['extras']} extras, for {w['xp']:,} XP.{more}")


def busy_labels() -> set[str]:
    """Discs in a drive right now: their folders are never tidied away."""
    return {d["disc_label"] for d in (RIPPER.drives if RIPPER else []) if d.get("loaded") and d.get("disc_label")}
TRAY: "desktop.Tray | None" = None


def tray_status() -> str:
    """One line for the tray icon: what the drives are doing and what's waiting."""
    if not RIPPER:
        return "Shelfrip"
    parts = []
    for d in RIPPER.drives:
        st = rip_all.STATUS.get(d["device"], {})
        if d["device"] in RIPPER.busy:
            pct = st.get("pct")
            parts.append(f"{d['device']} ripping" + (f" {pct}%" if pct else ""))
    try:
        n = len(review.list_plans())
    except Exception:
        n = 0
    if n:
        parts.append(f"{n} to review")
    return " · ".join(parts) or ("Auto-rip on, waiting for a disc" if RIPPER.auto else "Idle")


def quit_app(ask: bool = True) -> None:
    """Stop Shelfrip. With ask=True, a rip or filing still running asks first (Windows message box)."""
    running = sorted(RIPPER.busy) if RIPPER else []
    running += [k for k, j in review.JOBS.items() if j.get("state") == "running"]
    if running and ask and os.name == "nt":
        import ctypes
        u32 = ctypes.WinDLL("user32")
        u32.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        if u32.MessageBoxW(None, f"Still working on: {', '.join(running)}.\n\nQuitting stops it (a rip is cut off and "
                           "starts again next time). Quit anyway?", "Quit Shelfrip", 0x4 | 0x30 | 0x40000) != 6:
            return  # MB_YESNO | MB_ICONWARNING | MB_TOPMOST; 6 = Yes
    if TRAY:
        TRAY.remove()
    os._exit(0)


HUNG = {"why": ""}


def app_problem(url: str) -> str:
    """'' if Shelfrip is working, else what isn't (checked from the tray's own thread)."""
    try:
        with urllib.request.urlopen(url + "api/version", timeout=8) as r:
            r.read()
    except Exception:
        return "Its page has stopped answering."
    if RIPPER and time.time() - RIPPER.beat > 20 * 60:
        return "It has stopped checking the drives."
    return ""


def restart_app() -> None:
    """Start a fresh Shelfrip (after a short pause, so this one has let go of its port) and quit."""
    import base64
    import subprocess
    if getattr(sys, "frozen", False):
        exe, args = sys.executable, []
    else:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        exe, args = str(pyw if pyw.exists() else sys.executable), [str(Path(__file__).resolve())]
    q = lambda x: "'" + str(x).replace("'", "''") + "'"
    ps = f"Start-Sleep -Seconds 4; Start-Process -FilePath {q(exe)}" + (f" -ArgumentList {','.join(q(a) for a in args)}" if args else "")
    if os.name == "nt":
        subprocess.Popen(["powershell", "-NoProfile", "-EncodedCommand", base64.b64encode(ps.encode("utf-16-le")).decode()],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0),
                         close_fds=True)
    quit_app(ask=True)


def start_tray(url: str, use_window: bool) -> None:
    """The notification-area icon and its menu (open, auto-rip on/off, show the log, quit)."""
    global TRAY
    shown = {"console": False}

    def toggle_auto():
        RIPPER.auto = not RIPPER.auto
        rip_all.log("Auto-rip on: insert discs any time" if RIPPER.auto else "Auto-rip off")
        RIPPER.wake.set()

    def toggle_console():
        shown["console"] = not shown["console"]
        desktop.show_console(shown["console"])

    def menu():
        try:
            n = len(review.list_plans())
        except Exception:
            n = 0
        items = [(f"Not responding: {HUNG['why']}", None) if HUNG["why"] else (f"Shelfrip: {tray_status()}", None), None,
                 ("Open Shelfrip", lambda: desktop.open_window(url, use_window))]
        if n:
            items.append((f"Review {n} disc{'s' if n != 1 else ''}", lambda: desktop.open_window(url + "#review", use_window)))
        items.append(("Pause auto-rip" if RIPPER and RIPPER.auto else "Turn auto-rip on", toggle_auto))
        if console:
            items.append(("Hide log window" if shown["console"] else "Show log window", toggle_console))
        elif desktop.LOG_PATH.exists():
            items.append(("Open the log", lambda: desktop.open_file(desktop.LOG_PATH)))
        items += [None, ("Restart Shelfrip", restart_app), ("Quit Shelfrip", quit_app)]
        return items

    console = desktop.owns_console()
    tray = desktop.Tray("Shelfrip", lambda: desktop.open_window(url, use_window), menu)
    if not tray.start():
        return
    TRAY = tray
    if console:
        desktop.show_console(False)  # the tray icon is the way back in now

    def keep_tip_fresh():
        fails, warned = 0, False
        while True:
            time.sleep(10)
            problem = app_problem(url)  # is the page still answering, and the drive watcher still running?
            fails = fails + 1 if problem else 0
            HUNG["why"] = problem if fails >= 3 else ""
            try:
                if HUNG["why"]:
                    tray.set_tip("Shelfrip isn't responding: right-click to restart it")
                    if not warned:
                        warned = True
                        tray.notify("Shelfrip isn't responding", f"{problem} Right-click the Shelfrip icon by the clock "
                                    "and choose Restart Shelfrip.")
                else:
                    warned = False
                    tray.set_tip(f"Shelfrip: {tray_status()}")
            except Exception:
                pass
    threading.Thread(target=keep_tip_fresh, daemon=True).start()
_STATS: dict = {"t": 0.0, "filed": {}}


def job_stats(waiting: int, ripping: int) -> dict:
    """Progress through the whole job, free space, and discs that had read errors."""
    import shutil
    from pathlib import Path
    cfg = {**rip_all.DEFAULT_CONFIG, **identify.DEFAULTS, **JOB_DEFAULTS, **_read_config()}
    root = Path(cfg["output_root"])
    if time.time() - _STATS["t"] > 60:  # reading every filed plan is cheap, but not every 2 seconds
        filed = {"discs": 0, "movies": 0, "tv": 0, "music": 0, "hours": 0.0, "today": 0}
        today = time.strftime("%Y-%m-%d")
        for pj in (root / "_Filed").glob("*/plan.json"):
            try:
                plan = json.loads(pj.read_text(encoding="utf-8"))
            except Exception:
                continue
            if plan.get("status") != "filed":
                continue
            filed["discs"] += 1
            kind = {"movie": "movies", "tv": "tv", "music": "music"}.get(plan.get("kind"), "movies")
            filed[kind] += 1
            filed["hours"] += sum(float(it.get("duration_seconds") or 0) for it in plan.get("items", [])
                                  if it.get("action") == "move") / 3600
            filed["today"] += str(plan.get("filed_at", "")).startswith(today)
        filed["hours"] = round(filed["hours"], 1)
        _STATS.update(t=time.time(), filed=filed)
    space = []
    for name, path, need in (("Rip folder", cfg["output_root"], float(cfg.get("min_free_gb_rips") or 60)),
                             ("Library", cfg["plex_library_root"], 100.0)):
        try:
            du = shutil.disk_usage(path)
            space.append({"name": name, "path": path, "free_gb": round(du.free / 1e9), "total_gb": round(du.total / 1e9),
                          "low": du.free / 1e9 < need})
        except OSError:
            space.append({"name": name, "path": path, "free_gb": None, "total_gb": None, "low": False})
    try:
        damaged = json.loads((root / rip_all.DAMAGED).read_text(encoding="utf-8"))[-20:][::-1]
    except Exception:
        damaged = []
    return {**_STATS["filed"], "total": int(cfg.get("job_total_discs") or 0), "waiting": waiting,
            "ripping": ripping, "space": space, "damaged": damaged}


def dismiss_damaged(label: str) -> None:
    from pathlib import Path
    p = Path({**rip_all.DEFAULT_CONFIG, **_read_config()}["output_root"]) / rip_all.DAMAGED
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
        p.write_text(json.dumps([r for r in rows if r.get("label") != label], indent=2), encoding="utf-8")
    except Exception:
        pass
_RATE: dict[str, tuple[float, int, float]] = {}   # folder -> (time, bytes, bytes per second)


def rips_in_progress(root) -> list[dict]:
    """Every rip being written right now, from this app or any other Rip window.

    Progress = bytes written so far / the sizes MakeMKV listed for the titles it's ripping."""
    import re as _re
    out, now = [], time.time()
    try:
        folders = [d for d in root.iterdir() if d.is_dir() and not d.name.startswith("_")
                   and not (d / "disc_info.json").exists() and (d / "makemkv_info.txt").exists()]
    except OSError:
        return out
    for d in folders:
        try:
            files = [f for f in d.iterdir() if f.suffix.lower() == ".mkv"]
            done = sum(f.stat().st_size for f in files)
            newest = max([f.stat().st_mtime for f in files] + [(d / "makemkv_info.txt").stat().st_mtime])
            if now - newest > 600:  # nothing written for 10 min: not running (cancelled or crashed)
                continue
            info = (d / "makemkv_info.txt").read_text(encoding="utf-8", errors="replace")
            total = sum(int(m) for m in _re.findall(r'^TINFO:\d+,11,0,"(\d+)"', info, _re.M))
            n_titles = len(_re.findall(r'^TINFO:\d+,27,0,', info, _re.M))
        except (OSError, ValueError):
            continue
        prev = _RATE.get(d.name)
        rate = prev[2] if prev else 0.0
        if prev and now - prev[0] >= 5:
            inst = (done - prev[1]) / (now - prev[0])
            rate = inst if not prev[2] else 0.7 * prev[2] + 0.3 * inst
            _RATE[d.name] = (now, done, rate)
        elif not prev:
            _RATE[d.name] = (now, done, 0.0)
        pct = min(99, int(100 * done / total)) if total else 0
        eta = int((total - done) / rate) if rate > 0 and total > done else None
        out.append({"folder": d.name, "label": d.name.rsplit("__", 1)[0], "pct": pct, "done": done,
                    "total": total, "titles_done": len(files), "titles": n_titles,
                    "mb_s": round(rate / 1e6, 1) if rate else 0, "eta_s": eta})
    return out


def rip_folder_in_library(rip: str, cfg: dict) -> str:
    """The library folder the rip folder sits in (Plex would scan the raw rips), else ''."""
    def norm(x: str) -> str:
        return os.path.normcase(os.path.abspath(str(x).rstrip("\\/"))) if str(x or "").strip() else ""
    r = norm(rip)
    lib = str(cfg.get("plex_library_root") or "").strip()
    places = [lib] + [str(cfg.get(k) or "") for k in ("movies_folder", "tv_folder") if identify.is_full_path(str(cfg.get(k) or ""))]
    for place in places:
        q = norm(place)
        if q and r and (r == q or r.startswith(q.rstrip("\\/") + os.sep)):
            return place
    return ""


def setup_check() -> list[dict]:
    """What the app needs, whether it's there, and where to get it if not."""
    import split
    cfg = {**rip_all.DEFAULT_CONFIG, **identify.DEFAULTS, **_read_config()}
    out = []
    for key, name, what in (("output_root", "Rip folder", "where discs are ripped to before review"),
                            ("plex_library_root", "Media library folder", "the folder holding your Movies and TV Shows folders")):
        p = str(cfg.get(key) or "").strip()
        ok = bool(p) and (os.path.isdir(p) or (key == "output_root" and os.path.isdir(os.path.dirname(p.rstrip("\\/")) or "")))
        row = {"name": name, "ok": ok, "required": True, "link": "",
               "detail": p if ok else (f"{p} doesn't exist (or isn't connected)" if p else f"not chosen yet: {what}")}
        if key == "output_root" and p:
            inside = rip_folder_in_library(p, cfg)
            if inside:
                row["warn"] = (f"This is inside your media library ({inside}), so Plex will pick up the raw rips "
                               f"as extra movies. Choose a folder outside it, for example "
                               f"{os.path.splitdrive(p)[0] or 'D:'}\\Ripped.")
        out.append(row)
    mk = rip_all.find_makemkv(cfg)
    row = {"name": "MakeMKV", "ok": bool(mk), "detail": mk or "not installed: needed to rip discs",
           "link": "" if mk else rip_all.MAKEMKV_URL, "required": True}
    if mk and os.name == "nt":
        chk = health.makemkv_check(mk)  # is its key still good?
        if chk and chk.get("ok") is False:
            row.update(ok=False, detail=chk["detail"], link=chk.get("link", ""))
    out.append(row)
    mm = split.tool(cfg, "mkvmerge")
    bundled = bool(mm) and str(split.BUNDLED).lower() in mm.lower()
    out.append({"name": "MKVToolNix", "ok": bool(mm),
                "detail": (("bundled copy: " if bundled else "") + os.path.dirname(mm)) if mm else
                          f"not found: needed to split episodes. Install it, or put the portable version in {split.BUNDLED}",
                "link": "" if mm else split.MKVTOOLNIX_URL, "required": False})
    key = str(cfg.get("tmdb_api_key") or "")
    row = {"name": "TMDB key", "ok": bool(key) and "paste" not in key,
           "detail": "set" if key and "paste" not in key else "not set: needed to name movies and episodes (add it in Settings)",
           "link": "" if key and "paste" not in key else "https://www.themoviedb.org/settings/api", "required": True}
    if row["ok"]:
        chk = health.tmdb_check(key, wait=8)  # does it actually work?
        if chk and chk.get("ok") is False:
            row.update(ok=False, detail=chk["detail"], link="https://www.themoviedb.org/settings/api")
        elif chk:
            row["detail"] = chk["detail"]
    out.append(row)
    import music
    cy = music.find_cyanrip(cfg)
    out.append({"name": "cyanrip (music CDs)", "ok": bool(cy),
                "detail": cy or f"not found: needed to rip music CDs. Put cyanrip.exe (cyanrip-win64.exe) in {music.BUNDLED}",
                "link": "" if cy else music.CYANRIP_URL, "required": False})
    mr = music.music_root(cfg)
    out.append({"name": "Music library folder", "ok": mr.is_dir(),
                "detail": str(mr) if mr.is_dir() else f"{mr} doesn't exist yet: it's created when you file your first CD. "
                          "Then add it to Plex as a Music library.",
                "link": "" if mr.is_dir() else "https://support.plex.tv/articles/200288926-creating-libraries/", "required": False})
    plex = bool(cfg.get("plex_url")) and bool(cfg.get("plex_token")) and "paste" not in str(cfg.get("plex_token")).lower()
    out.append({"name": "Plex check", "ok": plex, "detail": "on" if plex else "off (optional: add the Plex address and token in Settings)",
                "link": "" if plex else "https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/",
                "required": False})
    return out


# --------------------------------------------------------------------------- library report

REPORT: dict = {"state": "none"}


def build_report() -> None:
    """Episode gaps, titles owned in several formats, and what Plex hasn't matched right."""
    import re as _re
    REPORT.clear()
    REPORT.update(state="running", step="Reading the library folders")
    try:
        lib = review.library(refresh=True)
        cfg = review.CFG
        rank = lambda f: identify.quality_rank(f, cfg)
        # --- movies in more than one format
        movies: dict[str, list[dict]] = {}
        for m in lib["movies"]:
            key = f"tmdb-{m['id']}" if m["id_src"] == "tmdb" else f"{m['norm']}|{m['year']}"
            movies.setdefault(key, []).append(m)
        dup_movies = [{"title": v[0]["title"], "year": v[0]["year"],
                       "formats": sorted({x["format_folder"] for x in v}, key=rank, reverse=True)}
                      for v in movies.values() if len({x["format_folder"] for x in v}) > 1]
        # --- shows: episodes per season, across format folders
        shows: dict[str, dict] = {}
        for sh in lib["shows"]:
            key = f"tmdb-{sh['id']}" if sh["id_src"] == "tmdb" else sh["norm"]
            e = shows.setdefault(key, {"title": sh["title"], "year": sh["year"], "id": sh["id"] if sh["id_src"] == "tmdb" else "",
                                       "eps": {}, "formats": set()})
            e["formats"].add(sh["format_folder"])
            try:
                files = [f for f in sh["folder"].rglob("*") if f.suffix.lower() in identify.VIDEO_EXT]
            except OSError:
                files = []
            for f in files:
                m = _re.search(r"s(\d{1,2})e(\d{1,3})(?:-e(\d{1,3}))?", f.name, _re.I)
                if not m:
                    continue
                s_, a = int(m.group(1)), int(m.group(2))
                for ep in range(a, int(m.group(3) or a) + 1):
                    e["eps"].setdefault(s_, {}).setdefault(ep, set()).add(sh["format_folder"])
        REPORT["step"] = "Checking episode counts on TMDB"
        tv_rows = []
        for n, e in enumerate(sorted(shows.values(), key=lambda x: x["title"].lower()), 1):
            REPORT["step"] = f"Checking episode counts on TMDB ({n} of {len(shows)})"
            counts = {}
            if review.TMDB and e["id"]:
                try:
                    for se in review.TMDB.tv(int(e["id"])).get("seasons") or []:
                        counts[se["season_number"]] = se.get("episode_count") or 0
                except Exception:
                    pass
            seasons = []
            for s_ in sorted(set(e["eps"]) | {k for k in counts if k > 0}):
                have = e["eps"].get(s_, {})
                nums = sorted(have)
                missing = [x for x in range(nums[0], nums[-1] + 1) if x not in have] if nums else []
                total = counts.get(s_)
                if total and nums and nums[0] == 1 and nums[-1] < total:   # numbered 1..N in TMDB
                    missing += list(range(nums[-1] + 1, total + 1))
                elif total and not nums:
                    missing = None   # whole season missing
                if s_ == 0:
                    missing = []  # specials are rarely all on disc; don't list gaps
                multi = sorted(x for x, f in have.items() if len(f) > 1)
                if s_ == 0 and not nums:
                    continue
                if missing or missing is None or multi:
                    seasons.append({"season": s_, "have": len(nums), "total": total, "missing": missing, "multi": multi})
            if seasons or len(e["formats"]) > 1:
                tv_rows.append({"title": e["title"], "year": e["year"], "formats": sorted(e["formats"], key=rank, reverse=True),
                                "seasons": seasons})
        REPORT["step"] = "Asking Plex what it matched"
        try:
            import plex
            audit = plex.audit(cfg)
        except Exception as exc:
            audit = {"error": str(exc)}
        REPORT.update(state="done", built=time.strftime("%Y-%m-%d %H:%M"), dup_movies=dup_movies, tv=tv_rows,
                      plex=audit, counts={"movies": len(movies), "shows": len(shows)})
    except Exception as exc:
        REPORT.update(state="error", error=str(exc))


# --------------------------------------------------------------------------- updates

UPDATE: dict = {"app": paths.APP_NAME, "current": paths.APP_VERSION}
BOOT = f"{os.getpid()}-{time.time():.0f}"  # changes when Shelfrip restarts, so an open page knows to reload


def _ver(v: str) -> tuple:
    return tuple(int(x) for x in __import__("re").findall(r"\d+", v)[:3])


def installed_copy() -> bool:
    """Is this the installed Shelfrip (not the portable zip or the scripts)? Only then can it update itself."""
    return bool(getattr(sys, "frozen", False)) and (Path(sys.executable).parent / "unins000.exe").exists()


def install_update() -> None:
    """Download the new installer and run it. It closes Shelfrip, updates it and starts it again."""
    import tempfile
    url, size = UPDATE.get("installer"), UPDATE.get("size") or 0
    UPDATE.update(step="downloading", got=0)
    try:
        dst = Path(tempfile.gettempdir()) / f"Shelfrip Setup {UPDATE.get('latest')}.exe"
        req = urllib.request.Request(url, headers={"User-Agent": paths.USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as r, open(dst, "wb") as f:
            while True:
                buf = r.read(1 << 20)
                if not buf:
                    break
                f.write(buf)
                UPDATE["got"] = UPDATE.get("got", 0) + len(buf)
        if size and dst.stat().st_size != size:
            raise OSError("the download was incomplete")
        UPDATE["step"] = "installing"
        import subprocess
        subprocess.Popen([str(dst), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                         close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        threading.Timer(2.0, lambda: quit_app(ask=False)).start()
    except Exception as exc:
        UPDATE.update(step="error", error=f"Couldn't update: {exc}. Download it from the link instead.")


def make_problem_report() -> Path:
    """A zip of the logs and settings (keys removed) to send when asking for help."""
    import platform
    import zipfile
    cfg = _read_config()
    secret = ("key", "token", "secret", "password", "notify_url", "install_id")
    safe_cfg = {k: ("(removed)" if any(s in k.lower() for s in secret) and v else v) for k, v in cfg.items()}
    out_dir = paths.DATA_DIR / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"Shelfrip report {time.strftime('%Y-%m-%d %H%M')}.zip"
    rip_root = review.RIP_ROOT
    info = [f"Shelfrip {paths.APP_VERSION}", f"Python {platform.python_version()} on {platform.platform()}",
            f"Frozen: {bool(getattr(sys, 'frozen', False))}, installed: {installed_copy()}", "", "Setup:"]
    try:
        info += [f"  {x['name']}: {'ok' if x['ok'] else 'NOT OK'} ({x['detail']})" for x in setup_check()]
    except Exception as exc:
        info.append(f"  couldn't check: {exc}")
    try:
        info += ["", "Drives:"] + [f"  {d['device']} {d.get('label') or '(empty)'}: {d.get('state') or ''} "
                                   f"{d.get('message') or ''} {d.get('stuck') or ''}" for d in RIPPER.state()["drives"]]
    except Exception:
        pass
    try:
        info += ["", "Rip folder:"] + [f"  {d.name}  {sum(f.stat().st_size for f in d.rglob('*') if f.is_file()) / 1e9:.1f} GB"
                                       for d in sorted(rip_root.iterdir()) if d.is_dir() and not d.name.startswith("_")]
    except Exception:
        pass
    try:
        info += ["", f"MakeMKV window open: {health.makemkv_window_open()}; "
                     f"makemkvcon left over from before: {len(health.leftovers())}"]
    except Exception:
        pass
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("about.txt", "\n".join(info))
        z.writestr("config (keys removed).json", json.dumps(safe_cfg, indent=2))
        try:  # MakeMKV's own output for the latest rips: what it read, and any error it gave
            recent = sorted([d for d in rip_root.iterdir() if d.is_dir() and (d / "makemkv_info.txt").exists()]
                            + [d for d in (rip_root / "_Filed").glob("*") if (d / "makemkv_info.txt").exists()],
                            key=lambda d: (d / "makemkv_info.txt").stat().st_mtime, reverse=True)[:3]
            for d in recent:
                for name in ("rip.log", "makemkv_info.txt", "disc_info.json"):
                    f = d / name
                    if f.exists():
                        z.writestr(f"rips/{d.name}/{name}", f.read_bytes()[-3_000_000:])
        except OSError:
            pass
        for f, name in ((desktop.LOG_PATH, "shelfrip.log"), (desktop.LOG_PATH.with_suffix(".old.log"), "shelfrip.old.log"),
                        (rip_root / "disc_ripper.log", "disc_ripper.log"), (rip_root / "rip_history.csv", "rip_history.csv"),
                        (rip_root / "damaged_discs.json", "damaged_discs.json")):
            try:
                if f.exists():
                    data = f.read_bytes()
                    z.writestr(name, data[-3_000_000:])
            except OSError:
                pass
    return dst


def check_update() -> None:
    """Ask GitHub once, at start, whether there's a newer release. Never blocks or fails loudly."""
    if not paths.UPDATE_REPO or not _read_config().get("check_updates", True):
        return
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{paths.UPDATE_REPO}/releases/latest",
                                     headers={"User-Agent": paths.USER_AGENT, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            rel = json.loads(r.read().decode("utf-8"))
        tag = str(rel.get("tag_name") or "")
        if tag and _ver(tag) > _ver(paths.APP_VERSION):
            UPDATE.update(latest=tag.lstrip("v"), url=rel.get("html_url") or "")
            import re as _re
            # downloads must come from this same repo, whatever its owner is called now (repos can move)
            home = str(rel.get("html_url") or "").split("/releases/")[0].lower()
            for a in rel.get("assets") or []:
                url = str(a.get("browser_download_url") or "")
                if _re.search(r"setup.*\.exe$", str(a.get("name") or ""), _re.I) and home.startswith("https://github.com/") and \
                        url.lower().startswith(home + "/releases/download/"):
                    UPDATE.update(installer=url, size=int(a.get("size") or 0), can_install=installed_copy())
    except Exception:
        pass


# --------------------------------------------------------------------------- settings

def _read_config(strict: bool = False) -> dict:
    """The saved settings as a dict ({} when there are none yet).

    With strict=True (used before saving) a settings file that exists but can't be read raises
    instead of returning {}, so a locked or half-written file never gets overwritten with only
    the one value being saved (which would wipe the TMDB key, folders and everything else).
    A file that is there but isn't valid JSON is copied to config.broken.json first.
    """
    path = identify.CONFIG_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except ValueError:
        if strict:
            try:
                import shutil
                shutil.copy2(path, path.with_name("config.broken.json"))
            except OSError as e:
                raise ValueError(f"Your settings file couldn't be read ({e}). Close anything that has it open and try again.")
            return {}  # kept a copy; start from defaults rather than refusing forever
        return {}
    except OSError as e:
        if strict:
            raise ValueError(f"Your settings file couldn't be read ({e}). Close anything that has it open and try again.")
        return {}


def settings_view() -> dict:
    """Every setting for the Settings page and setup wizard: saved value or default, label,
    kind and choices. Secrets are never sent back, only whether one is saved."""
    raw = _read_config()
    merged = {"check_plex_before_rip": True, "notifications": True, "check_updates": True, **JOB_DEFAULTS}
    merged.update(rip_all.DEFAULT_CONFIG)
    merged.update(identify.DEFAULTS)
    merged.update(raw)
    out = []
    for key, label, kind in SETTINGS:
        v = merged.get(key, "")
        if kind == "secret":  # never send a key back to the page
            v = bool(v) and "paste" not in str(v).lower()
        row = {"key": key, "label": label, "kind": kind, "value": v}
        if kind == "select":
            row["choices"] = CHOICES.get(key, [])
        out.append(row)
    lib = str(merged.get("plex_library_root") or "").strip()
    found = identify.library_dirs(merged)
    for row in out:  # what an empty Movies/TV folder setting means right now
        kind = {"movies_folder": "movies", "tv_folder": "tv"}.get(row["key"])
        if kind:
            name = found[kind]
            there = (bool(lib) or identify.is_full_path(name)) and identify.lib_path(Path(lib), name).is_dir()
            row["placeholder"] = f"{name} ({'found in your library' if there else 'made when the first disc is filed'})"
            row["value"] = identify.clean_lib_dir(row["value"], lib) or row["value"]
        elif row["key"] == "music_library_root" and lib:
            import music
            mr = str(music.music_root(merged))
            row["placeholder"] = f"{mr} ({'found in your library' if os.path.isdir(mr) else 'made when the first CD is filed'})"
    return {"settings": out, "path": str(identify.CONFIG_PATH), "setup": setup_check(), "version": UPDATE,
            "first_run": not raw.get("setup_done") and not raw.get("migrated_from"),
            "setup_step": raw.get("setup_step") or 0}


def save_settings(values: dict) -> None:
    """Merge the values from the Settings page or setup wizard into config.json.

    Only known keys are kept, each checked against its kind (number, yes/no, one of the listed
    choices, folder). An empty secret keeps the saved key. Raises ValueError with a message
    for the user when a value can't be used or the file can't be read.
    """
    with paths.CONFIG_LOCK:  # the wizard autosaves while you type: one read-modify-write at a time
        _save_settings(values)
    review.reload_config()
    if RIPPER:
        RIPPER.wake.set()


def _save_settings(values: dict) -> None:
    raw = _read_config(strict=True)
    if values.get("setup_done"):
        raw["setup_done"] = True
    if "setup_step" in values:  # which first-time setup screen you were on, so quitting halfway resumes there
        try:
            raw["setup_step"] = max(0, min(20, int(values["setup_step"])))
        except (TypeError, ValueError):
            pass
    kinds = {k: kind for k, _, kind in SETTINGS}
    for key, v in values.items():
        kind = kinds.get(key)
        if not kind:
            continue
        if kind == "secret":
            if isinstance(v, str) and v.strip():  # empty = keep the current key
                raw[key] = v.strip()
            elif v is None:
                raw[key] = ""
            continue
        if kind == "number":
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
        elif kind == "bool":
            v = bool(v)
        elif kind == "select":
            v = str(v)
            if v not in {c["value"] for c in CHOICES.get(key, [])}:
                continue
        else:
            v = str(v).strip()
        if key in ("movies_folder", "tv_folder") and v:
            lib = str(values.get("plex_library_root", raw.get("plex_library_root")) or "").strip()
            rel = identify.clean_lib_dir(v, lib)
            if not rel:
                raise ValueError("That isn't a folder Shelfrip can use. Pick one with Browse.")
            v = rel
        raw[key] = v
    try:
        paths.write_config(raw)
    except OSError as e:
        raise ValueError(f"Couldn't save settings ({e}). Another program may have config.json open: try again in a moment.")


# --------------------------------------------------------------------------- web

class Handler(review.Handler):
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/api/drives":
                return self.send(200, {**RIPPER.state(), "boot": BOOT})
            if u.path == "/api/settings":
                return self.send(200, settings_view())
            if u.path == "/api/report":
                return self.send(200, REPORT)
            if u.path == "/api/version":
                import platform
                return self.send(200, {**UPDATE, "os": platform.platform(terse=True), "donate": paths.DONATE_URL})
            if u.path == "/api/inbox":  # feedback and wrong-answer reports, on the copy that deployed the database
                import community
                if not community.admin_token():
                    return self.send(200, {"admin": False})
                cfg = {**rip_all.DEFAULT_CONFIG, **JOB_DEFAULTS, **_read_config()}
                try:
                    fb = community.admin("feedback" + ("?all=1" if "all=1" in (u.query or "") else ""), cfg)
                    rp = community.admin("reports", cfg)
                except Exception as exc:
                    return self.send(200, {"admin": True, "error": f"Couldn't reach the disc database: {exc}"})
                return self.send(200, {"admin": True, "feedback": fb.get("feedback", []), "reports": rp.get("reports", [])})
            if u.path == "/api/tidy":
                return self.send(200, tidy.scan(review.RIP_ROOT, busy_labels()))
            if u.path == "/api/seasons":
                return self.send(200, progress.seasons(review.TMDB, review.RIP_ROOT, review.LIB_ROOT))
            if u.path == "/api/progress":
                cfg = {**rip_all.DEFAULT_CONFIG, **JOB_DEFAULTS, **_read_config()}
                if not cfg.get("output_root"):
                    return self.send(200, {"none": True})
                return self.send(200, progress.compute(Path(cfg["output_root"]), int(cfg.get("job_total_discs") or 0)))
        except Exception as exc:
            return self.send(500, {"error": str(exc)})
        return super().do_GET()

    def do_POST(self):
        if self.headers.get("Origin") not in (None, f"http://localhost:{review.PORT}",
                                              f"http://127.0.0.1:{review.PORT}"):
            return self.send(403, {"error": "forbidden"})
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/report":
            if REPORT.get("state") != "running":
                threading.Thread(target=build_report, daemon=True).start()
                REPORT.update(state="running", step="Starting")
            return self.send(200, REPORT)
        if u.path == "/api/pick":  # the Browse buttons on the Settings page
            b = self.body()
            got, err = desktop.pick("file" if b.get("kind") == "file" else "folder", str(b.get("start") or ""),
                                    str(b.get("title") or ""))
            if err:
                rip_all.log(f"Browse: {err}")
            return self.send(200, {"path": got, "available": got is not None, "error": err})
        if u.path == "/api/tidy":
            res = tidy.clean(review.RIP_ROOT, [str(x) for x in (self.body().get("ids") or [])], busy_labels())
            if res["done"]:
                rip_all.log(f"Tidied the rip folder: {len(res['done'])} item(s), {res['freed'] / 1e9:.1f} GB freed")
            return self.send(200, res)
        if u.path == "/api/trouble":  # an error report offered after a failure: send it, or not
            b = self.body()
            key = str(b.get("id") or "")
            if b.get("action") == "send":
                cfg = {**rip_all.DEFAULT_CONFIG, **JOB_DEFAULTS, **_read_config()}
                return self.send(200, trouble.send(key, str(b.get("note") or "")[:1000], str(b.get("contact") or ""), cfg))
            trouble.dismiss(key)
            return self.send(200, {"ok": True})
        if u.path == "/api/feedback":
            import community
            cfg = {**rip_all.DEFAULT_CONFIG, **JOB_DEFAULTS, **_read_config()}
            return self.send(200, community.send_feedback(self.body(), cfg))
        if u.path == "/api/inbox":  # mark feedback done
            import community
            cfg = {**rip_all.DEFAULT_CONFIG, **JOB_DEFAULTS, **_read_config()}
            b = self.body()
            try:
                return self.send(200, community.admin("feedback", cfg, {"id": int(b.get("id") or 0), "done": bool(b.get("done", True))}))
            except Exception as exc:
                return self.send(200, {"error": str(exc)})
        if u.path == "/api/problem":
            try:
                dst = make_problem_report()
            except Exception as exc:
                return self.send(200, {"error": f"Couldn't make the report: {exc}"})
            if os.name == "nt":
                import subprocess
                subprocess.Popen(["explorer", "/select,", str(dst)])
            return self.send(200, {"path": str(dst), "issues": paths.APP_URL + "/issues/new"})
        if u.path == "/api/update":
            if not (UPDATE.get("installer") and UPDATE.get("can_install")):
                return self.send(200, {"error": "There's no update to install."})
            running = sorted(RIPPER.busy) + [k for k, j in review.JOBS.items() if j.get("state") == "running"]
            if running:
                return self.send(200, {"error": f"Still working on {', '.join(running)}. Update once that's done."})
            if UPDATE.get("step") not in ("downloading", "installing"):
                threading.Thread(target=install_update, daemon=True).start()
                UPDATE["step"] = "downloading"
            return self.send(200, UPDATE)
        if u.path not in ("/api/rip", "/api/settings", "/api/quit"):
            return super().do_POST()
        try:
            b = self.body()
            if u.path == "/api/rip" and b.get("action") == "dismiss_damaged":
                dismiss_damaged(str(b.get("label") or ""))
                return self.send(200, {"ok": True})
            if u.path == "/api/rip":
                act = b.get("action")
                if act == "stop_leftovers":
                    n = health.stop_leftovers()
                    _LEFT.update(t=0.0)
                    rip_all.log(f"Stopped {n} MakeMKV process(es) left over from before")
                elif act in ("auto_on", "auto_off"):
                    RIPPER.auto = act == "auto_on"
                    rip_all.log("Auto-rip on: insert discs any time" if RIPPER.auto else "Auto-rip off")
                elif act == "rip_now":
                    RIPPER.rip_once = True
                elif act == "cancel":
                    if not rip_all.cancel(str(b.get("device") or "")):
                        return self.send(200, {"error": "That drive isn't ripping."})
                elif act == "eject":
                    dev = str(b.get("device") or "")
                    if dev in RIPPER.busy:
                        return self.send(200, {"error": "That drive is ripping. Cancel it first."})
                    rip_all.eject(dev)
                elif act == "rip_again":  # rip anyway (already ripped, or already in Plex)
                    dev = str(b.get("device") or "")
                    if any(d["device"] == dev and d.get("filing") for d in RIPPER.state()["drives"]):
                        return self.send(200, {"error": "That disc is being filed. Wait until it's done.", **RIPPER.state()})
                    RIPPER.finished.pop(dev, None)
                    RIPPER.held.pop(dev, None)
                    RIPPER.force.add(dev)
                    RIPPER.rip_once = True
                RIPPER.wake.set()
                return self.send(200, {"ok": True, **RIPPER.state()})
            if u.path == "/api/settings":
                save_settings(b.get("values") or {})
                return self.send(200, {"ok": True, **settings_view()})
            if u.path == "/api/quit":
                running = sorted(RIPPER.busy) + [k for k, j in review.JOBS.items() if j.get("state") == "running"]
                if running and not b.get("force"):
                    return self.send(200, {"busy": running})
                self.send(200, {"ok": True})
                threading.Timer(0.5, lambda: quit_app(ask=False)).start()
                return
        except Exception as exc:
            return self.send(500, {"error": str(exc)})


class Server(ThreadingHTTPServer):
    # Python lets a server share a port that's already in use (SO_REUSEADDR). On Windows that means
    # Shelfrip could start on a port another program is using, and the browser would get that
    # program's answer instead (an "Unsupported method ('GET')" page). Take the port for ourselves.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _is_shelfrip(port: int) -> bool:
    """Is Shelfrip itself already answering on this port?"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=2) as r:
            info = json.loads(r.read().decode("utf-8"))
        return info.get("app") == paths.APP_NAME or ("current" in info and "app" not in info)
    except Exception:
        return False


def _port_taken(port: int) -> bool:
    """Is anything listening on this port, on 127.0.0.1 or ::1 ("localhost" can mean either)?"""
    for fam, host in ((socket.AF_INET, "127.0.0.1"), (getattr(socket, "AF_INET6", None), "::1")):
        if fam is None:
            continue
        try:
            with socket.socket(fam, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            pass
    return False


def open_server() -> tuple[Server | None, int]:
    """Start the web server on 8765, or the next free port if another program has it.
    Returns (None, port) when Shelfrip is already running on that port."""
    for port in range(review.PORT, review.PORT + 20):
        if _is_shelfrip(port):
            return None, port
        if _port_taken(port):
            print(f"Port {port} is used by another program; trying {port + 1}.")
            continue
        try:
            return Server(("127.0.0.1", port), Handler), port
        except OSError:
            continue
    raise SystemExit(f"Couldn't find a free port between {review.PORT} and {review.PORT + 19}.")


def main() -> None:
    global RIPPER
    if desktop.use_log_file():
        print(f"\n=== Shelfrip {paths.APP_VERSION} started {time.strftime('%Y-%m-%d %H:%M:%S')}")
    srv, port = open_server()
    review.PORT = port
    url = f"http://127.0.0.1:{port}/"
    cfg = _read_config()
    use_window = cfg.get("app_window", True)
    if srv is None:
        print("Shelfrip is already running. Opening it.")
        desktop.open_window(url, use_window)
        return
    if paths.MIGRATION_NOTE:
        print(paths.MIGRATION_NOTE)
    try:
        if health.find_leftovers():
            print(f"{len(health.LEFTOVER)} MakeMKV process(es) are still running from before.")
    except Exception:
        pass
    RIPPER = Ripper()
    threading.Thread(target=check_update, daemon=True).start()
    try:
        if review.RIP_ROOT.exists():
            print("Naming new rips...")
            print(f"  {review.plan_new_folders()} new disc(s) named.")
    except Exception as exc:
        print(f"  couldn't name new rips: {exc}")
    print(f"Shelfrip: {url}  (quit from the tray icon by the clock, or the Quit button on the page)")
    if cfg.get("tray_icon", True) or sys.stdout is None or not desktop.owns_console():
        start_tray(url, use_window)  # with no console window, the tray icon is the way to quit
    if "--background" not in sys.argv:  # started with Windows: don't pop the page open
        threading.Timer(0.8, lambda: desktop.open_window(url, use_window)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
