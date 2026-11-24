"""
identify.py - Step 2a of the Plex disc pipeline.

For every ripped disc folder in the rip folder that has a disc_info.json (made by
rip_all.py) and no plan yet, this script:

  1. works out whether the disc is a movie or TV and which TMDB entry it is
     (it checks your existing Plex library first, so a show you've already
     started keeps the same folder and TMDB id)
  2. sorts the titles into main feature / episodes / extras / junk
  3. proposes the exact Plex path for each file, matching your library's style
  4. writes plan.json into the disc folder

Nothing is moved or renamed. Step 3 (the review page) shows plan.json, lets you
fix anything, and does the moving.

Usage:
    python identify.py            plan every new disc folder
    python identify.py --redo     re-plan folders that already have a plan
    python identify.py FOLDER     plan one folder
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# next to the script, or next to Shelfrip.exe when built with PyInstaller
HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
import naming
import paths
CONFIG_PATH = paths.CONFIG_PATH

DEFAULTS = {
    # Both chosen in the setup step (Settings). plex_library_root is the media library
    # folder for any server; the name is kept so older settings files still work.
    "output_root": "",
    "plex_library_root": "",
    # The Movies and TV Shows folders inside it, as named in your library ("movies", "TVShows",
    # "Video\\Films"...). Empty = use the folder that's already there (any case or spacing), else
    # "Movies" / "TV Shows". See library_dirs().
    "movies_folder": "",
    "tv_folder": "",
    # plex, jellyfin, emby or kodi: how filed files are named (naming.py)
    "media_server": "plex",
    "extras_style": "folders",  # special features in Featurettes\, Trailers\... folders, or "suffix": Name-featurette.mkv
    # Put each format in its own folder (Movies\Blu-Ray\Title ...)? Off: Movies\Title ...
    "format_folders": False,
    # DVDCompare has no API; looking extras up there means reading its web pages. Off unless you turn it on.
    "use_dvdcompare": False,
    # The shared Shelfrip disc database (community.py): look discs up there, and share what you file.
    "use_community": True,
    "share_discs": True,
    "tmdb_api_key": "",
    # Optional second opinion on movies (free key: https://www.omdbapi.com/apikey.aspx)
    "omdb_api_key": "",
    # Format names (and folder names, with format_folders on), exactly as in your library
    "movie_format_folders": {"4k": "4K Blu-Ray", "bluray": "Blu-Ray", "dvd": "DVD"},
    "tv_format_folders": {"4k": "4K Blu Ray", "bluray": "Blu-Ray", "dvd": "DVD"},
    # Extras shorter than this are marked "skip" by default (logos, promos)
    "skip_extras_under_seconds": 45,
    "eject_after_filing": True,
    # MKVToolNix, used to split videos that hold several episodes
    "mkvtoolnix_dir": r"C:\Program Files\MKVToolNix",
    # Optional: after filing, ask Plex to scan and check its matches (see plex.py)
    "plex_url": "",
    "plex_token": "",
    # Music CDs (music.py): cyanrip does the ripping; empty = tools\cyanrip\cyanrip.exe
    "cyanrip_path": "",
    # Each drive's read offset for AccurateRip, e.g. "D:6, E:667" (look your drive up at
    # http://www.accuraterip.com/driveoffsets.htm). 0 if not set.
    "cd_offsets": "",
    # Where albums go; empty = <plex_library_root>\Music
    "music_library_root": "",
    "music_lyrics": True,
}

EXTRA_CATEGORIES = ["Featurettes", "Behind the Scenes", "Deleted Scenes", "Interviews",
                    "Scenes", "Shorts", "Trailers", "Other"]

# words to strip from disc labels / disc names before searching TMDB
# Tokens that are never part of a title: "'17" (a release year), "20THANNIV", "DUB", "BD2", "D1".
NOISE_PATTERNS = (r"'\d{2}", r"\d+(ST|ND|RD|TH)?ANNIV(ERSARY)?", r"ANNIV(ERSARY)?", r"DUB(BED)?", r"SUB(BED|S)?",
                  r"BD\d{1,2}", r"D\d{1,2}")
NOISE_WORDS = {
    "DISC", "DISK", "DVD", "BD", "BLURAY", "BLU", "RAY", "UHD", "4K", "HD", "WS",
    "WIDESCREEN", "FULLSCREEN", "FS", "EDITION", "SPECIAL", "COLLECTORS", "COLLECTOR'S",
    "DIAMOND", "PLATINUM", "ANNIVERSARY", "ULTIMATE", "DELUXE", "LIMITED", "REMASTERED",
    "EXTENDED", "THEATRICAL", "DIRECTORS", "CUT", "SEASON", "SERIES", "VOLUME", "VOL",
    "COMPLETE", "THE COMPLETE", "BONUS", "FEATURES", "NTSC", "PAL", "R1", "US", "UK",
}


# --------------------------------------------------------------------------- utils

PLANNER_VERSION = 9  # bump when naming improves, so untouched proposals get redone


# Settings you fill in yourself: added to config.json (empty) if they're missing, so you can see where they go
FILL_IN = {"plex_url": "", "plex_token": ""}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        mine = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        missing = {k: v for k, v in FILL_IN.items() if k not in mine}
        if "eject_after_filing" not in mine:  # 2026-11-05: discs stay in the drive until filed
            missing.update(eject_after_filing=True, eject_when_done=False)
        if missing:
            try:
                mine.update(missing)
                tmp = CONFIG_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(mine, indent=2), encoding="utf-8")
                tmp.replace(CONFIG_PATH)
            except OSError:
                pass
        cfg.update(mine)
    return cfg


def fs_safe(name: str) -> str:
    """Make a title safe for Windows, the way your library does it ("Dune: Part Two" -> "Dune - Part Two")."""
    name = re.sub(r"\s*:\s*", " - ", name)
    name = re.sub(r'[<>"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip(" .")


def norm(s: str) -> str:
    s = s.lower().replace("&", "and")
    s = re.sub(r"\{[^}]*\}|\(\d{4}\)", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def capital_case(name: str) -> str:
    """'the making of the lion king' -> 'The Making Of The Lion King'.

    Every word (and each part of a hyphenated word) starts with a capital. Words that
    already have capitals inside them (TV, Q&A, McQueen, 3D) are left as they are.
    """
    def word(w: str) -> str:
        if any(c.isupper() for c in w[1:]):
            return w
        m = re.search(r"[A-Za-z]", w)
        if not m or (m.start() and w[m.start() - 1].isdigit()):  # '1st', '3d' stay
            return w
        i = m.start()
        return w[:i] + w[i].upper() + w[i + 1:]
    return " ".join("-".join(word(part) for part in w.split("-")) for w in name.split(" "))


def mmss(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def parse_label(label: str) -> dict:
    """'TRANSFORMERS_G1_S2_D7' -> {'query': 'Transformers G1', 'season': 2, 'disc': 7}"""
    label = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", label)        # 'HaikyuDub' -> 'Haikyu Dub'
    label = re.sub(r"(?<=[A-Za-z])(?='\d)", " ", label)        # "Dub'17" -> "Dub '17"
    label = re.sub(r"(?<=\d)(?=(?:BD|DVD)(?:\b|[-_\s\d]))", " ", label, flags=re.I)  # "'17BD-3" -> "'17 BD-3"
    label = re.sub(r"(?<=[A-Za-z]{3})(?=\d{1,2}(?:\b|_))", " ", label)  # 'TRANSFORMERS2' -> 'TRANSFORMERS 2'
    up = label.upper().replace("_", " ").replace("-", " ").replace(".", " ")
    season = disc = None
    m = re.search(r"\b(?:S|SEASON|SERIES)\s?(\d{1,2})\b", up)
    if m:
        season = int(m.group(1))
    m = re.search(r"\b(?:D|DISC|DISK|DVD|BD)\s?(\d{1,2})\b", up)
    if m:
        disc = int(m.group(1))
    words, prev = [], ""
    for w in up.split():
        if w in NOISE_WORDS:
            prev = w
            continue
        if re.fullmatch(r"(S|D|V|VOL|DISC|DISK|SEASON|SERIES)\d{1,2}", w):
            continue
        if re.fullmatch(r"\d{1,2}", w) and prev in ("S", "D", "DISC", "DISK", "SEASON", "SERIES", "VOL", "VOLUME", "BD"):
            prev = w
            continue  # 'SEASON 4', 'DISC 1': the number belongs to the marker
        prev = w
        if any(re.fullmatch(p, w) for p in NOISE_PATTERNS):
            continue
        words.append(w)
    query = " ".join(words).title()
    query = re.sub(r"\bG1\b", "G1", query, flags=re.I)
    return {"query": query.strip(), "season": season, "disc": disc}


# --------------------------------------------------------------------------- TMDB

class TMDB:
    BASE = "https://api.themoviedb.org/3"

    def __init__(self, key: str):
        self.key = key.strip()
        self.cache: dict[str, dict] = {}

    def get(self, path: str, **params) -> dict:
        headers = {"Accept": "application/json"}
        if self.key.startswith("eyJ"):  # v4 "Read Access Token"
            headers["Authorization"] = f"Bearer {self.key}"
        else:
            params["api_key"] = self.key
        url = f"{self.BASE}{path}?{urllib.parse.urlencode(params)}"
        if url in self.cache:
            return self.cache[url]
        req = urllib.request.Request(url, headers=headers)
        for attempt in range(4):  # a dropped connection or a busy TMDB: wait a moment and try again
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = json.loads(r.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise
                wait = float(exc.headers.get("Retry-After") or 0) if exc.code == 429 else 0
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                if attempt == 3:
                    raise
                wait = 0
            time.sleep(max(wait, 2 ** attempt))
        self.cache[url] = data
        return data

    def search(self, kind: str, query: str) -> list[dict]:
        if not query:
            return []
        return self.get(f"/search/{kind}", query=query, include_adult="false").get("results", [])

    def movie(self, mid: int) -> dict:
        return self.get(f"/movie/{mid}")

    def tv(self, tid: int) -> dict:
        return self.get(f"/tv/{tid}")

    def season(self, tid: int, n: int) -> dict:
        try:
            return self.get(f"/tv/{tid}/season/{n}")
        except Exception:
            return {"episodes": []}


def year_of(date: str | None) -> str:
    return (date or "")[:4]


# --------------------------------------------------------------------------- library index

TAG_RE = re.compile(r"^(?P<title>.+?)\s*(?:\((?P<year>\d{4})\))?\s*(?:" + naming.TAG_PATTERN + r")?\s*$")
EP_RE = re.compile(r"s(\d{1,2})e(\d{1,3})", re.I)


# What existing top-level folders are recognised as, compared without case, spaces, dashes or underscores
LIB_DIR_NAMES = {"movies": ("Movies", ("movies", "movie", "films", "film")),
                 "tv": ("TV Shows", ("tvshows", "tvshow", "tv", "shows", "tvseries", "series", "television"))}


def _squash(name: str) -> str:
    return re.sub(r"[\s_\-.]+", "", name).lower()


def _split_abs(v: str) -> tuple[str, list[str]]:
    """A path as (root, folder names). The root is "D:\\", "\\\\NAS\\Media\\" or (off Windows) "/" for a
    full path, and "" for a path inside the library."""
    v = str(v or "").strip()
    anchor, rest = "", v
    m = re.match(r"^([A-Za-z]:)(?:[\\/]|$)", v)
    u = re.match(r"^[\\/]{2}([^\\/]+)[\\/]+([^\\/]+)(?:[\\/]|$)", v)  # \\server\\share
    if m:
        anchor, rest = m.group(1) + "\\", v[m.end():]
    elif u:
        anchor, rest = f"\\\\{u.group(1)}\\{u.group(2)}\\", v[u.end():]
    elif os.name != "nt" and v.startswith("/"):
        anchor, rest = "/", v
    return anchor, [x for x in re.split(r"[\\/]+", rest) if x.strip()]


def clean_lib_dir(value: str, root: str = "") -> str:
    """A Movies/TV folder setting, tidied ("" if it isn't a usable folder).

    A name ("movies") or a path inside the library ("Video\\Films") stays relative to it, and so
    does a full path inside the library folder (what the Browse button gives), case-insensitively.
    A full path anywhere else ("D:\\Movies", "\\\\NAS\\TV") is kept as a full path."""
    anchor, parts = _split_abs(value)
    if (not anchor and not parts) or any(x.strip() in (".", "..") for x in parts):
        return ""
    if not anchor:
        return "\\".join(parts)
    ra, rparts = _split_abs(root)
    if ra and ra.lower() == anchor.lower() and [x.lower() for x in parts[:len(rparts)]] == [x.lower() for x in rparts] \
            and len(parts) > len(rparts):
        return "\\".join(parts[len(rparts):])  # inside the library: keep it relative
    full = anchor + ("/" if anchor == "/" else "\\").join(parts)
    return full.rstrip("\\") if anchor.startswith("\\\\") else full


def is_full_path(p: str) -> bool:
    return bool(_split_abs(p)[0])


def lib_path(root: Path, rel: str) -> Path:
    """A library path from a plan ("Movies\\Title (Year)", or a full path for folders kept elsewhere),
    with each folder name made safe for Windows."""
    anchor, parts = _split_abs(rel)
    parts = [fs_safe(x) for x in parts]
    return Path(anchor).joinpath(*parts) if anchor else Path(root).joinpath(*parts)


def library_dirs(cfg: dict, root: Path | None = None) -> dict:
    """{"movies": ..., "tv": ...}: the Movies and TV Shows folders, as paths inside the library
    (or full paths, for folders kept somewhere else).

    A folder chosen in Settings wins. Otherwise an existing folder is used whatever its case or spacing
    ("movies", "TVShows", "TV_Shows"), so files land next to what's already there, and a new library
    gets "Movies" and "TV Shows"."""
    lib = str(cfg.get("plex_library_root") or "").strip()
    if root is None and lib:
        root = Path(lib)
    try:  # an unchosen or unreachable library has no folders yet
        existing = [d.name for d in root.iterdir() if d.is_dir()] if root is not None and root.is_dir() else []
    except OSError:
        existing = []
    out = {}
    for kind, key in (("movies", "movies_folder"), ("tv", "tv_folder")):
        default, names = LIB_DIR_NAMES[kind]
        chosen = clean_lib_dir(cfg.get(key, ""), lib)
        if chosen and is_full_path(chosen):
            out[kind] = chosen
            continue
        if chosen:
            # match the case of a folder that's already there, so "movies" typed for "Movies" still lines up
            first = chosen.split("\\")[0]
            same = next((d for d in existing if d.lower() == first.lower()), None)
            out[kind] = (same + chosen[len(first):]) if same else chosen
            continue
        found = None
        for want in names:
            found = next((d for d in sorted(existing) if _squash(d) == want), None)
            if found:
                break
        out[kind] = found or default
    return out


def index_library(root: Path, cfg: dict) -> dict:
    lib = {"movies": [], "shows": []}
    if not root.exists():
        print(f"  ! Plex library not reachable at {root}. Episode numbering will start at 1.")
        return lib
    tops = library_dirs(cfg, root)
    for kind, top, fmts in (("movies", tops["movies"], cfg["movie_format_folders"]),
                            ("shows", tops["tv"], cfg["tv_format_folders"])):
        base = lib_path(root, top)
        if not base.exists():
            continue
        fmt_names = {v.lower() for v in fmts.values()} | {"4k blu-ray", "4k blu ray", "blu-ray", "bluray", "dvd", "4k"}
        found = []  # (folder, format folder name or "")
        for d in base.iterdir():
            if not d.is_dir():
                continue
            if d.name.lower() in fmt_names:  # Movies\Blu-Ray\Title (Year)
                found += [(item, d.name) for item in d.iterdir() if item.is_dir()]
            else:                            # Movies\Title (Year)
                found.append((d, ""))
        for item, fmt_dir in found:
            m = TAG_RE.match(item.name)
            if not m:
                continue
            lib[kind].append({
                "folder": item, "folder_name": item.name, "format_folder": fmt_dir,
                "title": m.group("title"), "year": m.group("year") or "",
                "id_src": m.group("src") or m.group("src2") or "", "id": m.group("id") or m.group("id2") or "",
                "norm": norm(m.group("title")),
            })
    return lib


def library_show_match(lib: dict, query: str) -> dict | None:
    """Longest library show title that the disc label starts with (e.g. 'transformers g1' -> 'Transformers')."""
    q = norm(query)
    best = None
    for s in lib["shows"]:
        n = s["norm"]
        if n and (q == n or q.startswith(n + " ")):
            if best is None or len(n) > len(best["norm"]):
                best = s
    return best


# Extras tagged in the file name, next to the film (Plex/Jellyfin/Emby): "Making Of-featurette.mkv"
EXTRA_TAGGED = re.compile(r"-(?:behindthescenes|deleted|deletedscene|featurette|interview|scene|short|trailer|other|clip|extra)\d*$", re.I)


def episodes_in_library(lib: dict, tmdb_id: str, title_norm: str, season: int) -> set[int]:
    eps: set[int] = set()
    for s in lib["shows"]:
        if (tmdb_id and s["id"] == str(tmdb_id)) or (not tmdb_id and s["norm"] == title_norm):
            for f in s["folder"].rglob("*"):
                m = EP_RE.search(f.name)
                if m and int(m.group(1)) == season and f.suffix.lower() in (".mkv", ".mp4", ".avi", ".m4v"):
                    eps.add(int(m.group(2)))
    return eps


def episodes_claimed_by_plans(rip_root: Path, this_folder: Path, show_key: str, season: int) -> set[int]:
    eps: set[int] = set()
    for p in rip_root.glob("*/plan.json"):
        if p.parent == this_folder:
            continue
        try:
            plan = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if plan.get("status") == "filed" or plan.get("show_key") != show_key:
            continue
        for it in plan.get("items", []):
            if it.get("role") == "episode" and it.get("action") != "skip" and it.get("season") == season:
                for e in it.get("episodes", [it.get("episode")]):
                    if e:
                        eps.add(int(e))
    return eps


def episode_lengths_from_plans(rip_root: Path, this_folder: Path, show_key: str) -> list[int]:
    """Lengths of single episodes on other discs of the same show (waiting or already filed)."""
    out: list[int] = []
    for p in list(rip_root.glob("*/plan.json")) + list(rip_root.glob("_Filed/*/plan.json")):
        if p.parent == this_folder:
            continue
        try:
            plan = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if plan.get("show_key") != show_key:
            continue
        out += [int(it["duration_seconds"]) for it in plan.get("items", [])
                if it.get("role") == "episode" and it.get("action") != "skip"
                and not it.get("needs_split") and it.get("season") != 0 and it.get("duration_seconds")]
    return out


def quality_rank(fmt_folder: str, cfg: dict) -> int:
    """DVD 1, Blu-ray 2, 4K 3 (by the library's format folder names); 0 if unknown."""
    order = {"dvd": 1, "bluray": 2, "4k": 3}
    for maps in (cfg["movie_format_folders"], cfg["tv_format_folders"]):
        for k, name in maps.items():
            if name.lower() == (fmt_folder or "").lower():
                return order.get(k, 0)
    return 0


def file_rank(path: Path, fmt_folder: str, cfg: dict, movie: bool) -> int:
    """quality_rank by format folder; without one, guessed from the file name or, for a film, its size."""
    r = quality_rank(fmt_folder, cfg)
    if r:
        return r
    n = path.name.lower()
    if re.search(r"2160p|\b4k\b|uhd", n):
        return 3
    if re.search(r"1080p|blu-?ray|\bbd\b", n):
        return 2
    if re.search(r"480p|576p|\bdvd\b", n):
        return 1
    if movie:
        try:
            gb = path.stat().st_size / 1e9
        except OSError:
            return 0
        return 1 if gb < 12 else 2 if gb < 45 else 3
    return 0


FORMAT_OF_RANK = {1: "DVD", 2: "Blu-ray", 3: "4K"}
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".m2ts")


def library_copies(plan: dict, lib: dict, cfg: dict) -> dict[str, list[dict]]:
    """What the library already has of each movie / episode in this plan, in any format folder.

    Returns {item source: [{"path", "format", "rank"}]} for main features, other versions and
    episodes. Matches by TMDB id in the folder name, else by title (and year)."""
    tv = plan.get("kind") == "tv"
    tid = str(plan.get("tmdb_id") or "")
    t_norm, year = norm(plan.get("title") or ""), str(plan.get("year") or "")
    folders = [x for x in lib.get("shows" if tv else "movies", [])
               if (tid and x["id_src"] == "tmdb" and x["id"] == tid)
               or (x["norm"] == t_norm and (not year or not x["year"] or x["year"] == year))]
    out: dict[str, list[dict]] = {}
    if not folders:
        return out
    if not tv:
        have = []
        for f in folders:
            try:
                vids = [p for p in f["folder"].iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT
                        and not EXTRA_TAGGED.search(p.stem)]  # "Making Of-featurette.mkv" is an extra, not the film
            except OSError:
                continue
            for v in vids:
                rank = file_rank(v, f["format_folder"], cfg, True)
                have.append({"path": str(v), "format": f["format_folder"] or FORMAT_OF_RANK.get(rank, "unknown"),
                             "rank": rank, "name": v.stem})
        for it in plan["items"]:
            if it.get("role") == "main":
                # an edition file ({edition-...} or "- Extended") isn't the same as the main feature
                out[it["source"]] = [h for h in have if not naming.edition_of(h["name"])]
            elif it.get("role") == "edition":
                ed = naming.edition_of(it.get("name") or "").lower()
                out[it["source"]] = [h for h in have if ed and naming.edition_of(h["name"]).lower() == ed]
        return {k: v for k, v in out.items() if v}
    by_ep: dict[tuple[int, int], list[dict]] = {}
    for f in folders:
        try:
            files = [p for p in f["folder"].rglob("*") if p.suffix.lower() in VIDEO_EXT]
        except OSError:
            continue
        for v in files:
            for m in re.finditer(r"s(\d{1,2})e(\d{1,3})(?:-e(\d{1,3}))?", v.name, re.I):
                s_, a = int(m.group(1)), int(m.group(2))
                for e in range(a, int(m.group(3) or a) + 1):
                    rank = file_rank(v, f["format_folder"], cfg, False)
                    by_ep.setdefault((s_, e), []).append({"path": str(v), "ep": (s_, e), "rank": rank,
                                                          "format": f["format_folder"] or FORMAT_OF_RANK.get(rank, "unknown")})
                break
    for it in plan["items"]:
        if it.get("role") != "episode":
            continue
        eps = it.get("episodes") or [it.get("episode")]
        hits = [h for e in eps if e for h in by_ep.get((it.get("season") or 0, e), [])]
        if hits:
            out[it["source"]] = hits
    return out


def mark_library_copies(plan: dict, lib: dict, cfg: dict) -> None:
    """Skip titles the library already has in the same or better quality; note upgrades."""
    mine = quality_rank(plan.get("format") or "", cfg)
    copies = library_copies(plan, lib, cfg)
    skipped = upgrades = 0
    for it in plan["items"]:
        have = copies.get(it["source"])
        if not have:
            continue
        best = max(have, key=lambda h: h["rank"])
        sp = it.get("split") if it.get("needs_split") else None
        if sp and mine:
            # a video holding several episodes: skip just the episodes Plex already has
            eps = it.get("episodes") or []
            dup = {k for k, e in enumerate(eps) if any(
                h["rank"] >= mine and h.get("ep") == (it.get("season") or 0, e)
                for h in have)}
            if dup and len(dup) < len(eps):
                sp["skip"] = sorted(dup)
                it["note"] = "; ".join(x for x in (", ".join(f"e{eps[k]:02d}" for k in sorted(dup))
                                       + f" already in Plex ({best['format']}, same or better), so not cut out",
                                       it.get("note")) if x)
                skipped += len(dup)
                continue
        what = f"{best['format']} copy" + (f" ({Path(best['path']).name})" if len(have) == 1 else f"s ({len(have)} files)")
        if best["rank"] >= mine and mine:
            it["action"] = "skip"
            it["library_copy"] = {"format": best["format"], "better": best["rank"] > mine}
            it["note"] = "; ".join(x for x in (f"already in Plex as a {what}, "
                                   f"{'better' if best['rank'] > mine else 'same'} quality, so skipped. "
                                   f"Set Keep to add it anyway", it.get("note")) if x)
            skipped += 1
        else:
            it["library_copy"] = {"format": best["format"], "better": False, "upgrade": True}
            it["note"] = "; ".join(x for x in (f"upgrade: Plex has a {what}. Delete the old one after "
                                   f"filing if you don't want both", it.get("note")) if x)
            upgrades += 1
    main = next((it for it in plan["items"] if it.get("role") == "main"), None)
    if plan.get("kind") == "movie" and main and main.get("action") == "skip" and copies.get(main["source"]):
        # the movie stays as it is in Plex; send this disc's extras to that movie's folder
        best = max(copies[main["source"]], key=lambda h: h["rank"])
        try:
            parent = Path(best["path"]).parent
            try:
                rel = str(parent.relative_to(Path(cfg["plex_library_root"]))).replace("/", "\\")
            except ValueError:  # a Movies folder kept outside the media library
                rel = str(parent)
            plan["library_dir"] = rel
            for it in plan["items"]:
                it["dest_dir"] = rel
            if any(it.get("action") == "move" for it in plan["items"]):
                plan.setdefault("warnings", []).append(f"Extras will go into the movie's existing folder ({rel}).")
        except ValueError:
            pass
    if skipped:
        plan.setdefault("warnings", []).append(
            f"{skipped} title(s) are already in Plex in the same or better quality and are set to Skip.")
    if upgrades:
        plan.setdefault("warnings", []).append(
            f"{upgrades} title(s) are in Plex in lower quality. These rips are better, so they'll be added.")


# --------------------------------------------------------------------------- classification

def disc_format(disc: dict) -> str:
    width = 0
    for t in disc["titles"]:
        for s in t.get("streams", []):
            m = re.match(r"(\d+)x(\d+)", s.get("resolution", ""))
            if m:
                width = max(width, int(m.group(1)))
    if width >= 3000:
        return "4k"
    if width >= 1200:
        return "bluray"
    return "dvd"


def ripped_titles(folder: Path, disc: dict) -> list[dict]:
    out = []
    for t in disc["titles"]:
        f = t.get("output_file")
        if f and (folder / f).exists():
            out.append(t)
    return out


def mark_duplicates(titles: list[dict]) -> None:
    """Blu-rays often list the same clip twice (a playlist and its raw .m2ts). Keep the playlist."""
    seen: dict[int, dict] = {}
    def prefer(t: dict) -> tuple:
        is_raw_clip = str(t.get("source_file", "")).lower().endswith(".m2ts")
        return (is_raw_clip, t["title_id"])

    for t in sorted(titles, key=prefer):
        d, size = t.get("duration_seconds", 0), t.get("size_bytes", 0)
        twin = next((o for k, o in seen.items() if abs(k - d) <= 1 and
                     (not size or not o.get("size_bytes") or
                      abs(o["size_bytes"] - size) <= 0.02 * max(size, o["size_bytes"]))), None)
        if twin is not None and d >= 20:
            t["_duplicate_of"] = twin["output_file"]
        else:
            seen.setdefault(d, t)


def looks_like_tv(titles: list[dict], label_info: dict, lib_show: dict | None) -> bool:
    if label_info["season"] is not None or lib_show:
        return True
    longs = [t["duration_seconds"] for t in titles if t["duration_seconds"] >= 15 * 60]
    if not longs:
        return False
    if max(longs) >= 70 * 60 and len([d for d in longs if d >= 70 * 60]) <= 2:
        return False
    if len(longs) >= 3:
        med = statistics.median(longs)
        similar = [d for d in longs if abs(d - med) <= 0.25 * med]
        return len(similar) >= 3 and med <= 70 * 60
    return False


# --------------------------------------------------------------------------- planners

# Studios sometimes give each playlist a descriptive name that MakeMKV puts in the file name, e.g.
# "Special Features-SEG_01_BTS_07_Ithaca_t11.mkv" or "SF_02_Trls_03_Trailer1_NCR". Known tags say
# what kind of extra it is; the words left over are its name.
AUTHORED_KIND = {
    "bts": "Behind the Scenes", "behindthescenes": "Behind the Scenes", "making": "Behind the Scenes",
    "trl": "Trailers", "trls": "Trailers", "trailer": "Trailers", "trailers": "Trailers", "teaser": "Trailers",
    "del": "Deleted Scenes", "deleted": "Deleted Scenes", "delscn": "Deleted Scenes",
    "int": "Interviews", "interview": "Interviews", "interviews": "Interviews",
    "fea": "Featurettes", "feat": "Featurettes", "featurette": "Featurettes", "featurettes": "Featurettes",
    "musicvideo": "Other", "gag": "Other", "gagreel": "Other", "bloopers": "Other",
}
AUTHORED_NOISE = {"seg", "sf", "tmpl", "ncr", "uni", "extras", "extra", "bonus", "pl", "mpls", "hd", "uhd", "sdr", "hdr"}


def authored_name(t: dict, disc: dict | None = None) -> tuple[str, str] | None:
    """(name, category) from a playlist name the studio wrote, or None when the file name is just
    the disc's own name (MakeMKV's usual DISC_LABEL_t03.mkv) or says nothing useful."""
    stem = re.sub(r"_t\d+$", "", Path(str(t.get("output_file") or "")).stem)
    part = stem.split("-", 1)[1] if "-" in stem and " " in stem.split("-", 1)[0] else stem
    flat = lambda x: re.sub(r"[^a-z0-9]", "", str(x or "").lower())
    if not part or flat(part) in {flat((disc or {}).get("disc_label")), flat((disc or {}).get("name")), flat((disc or {}).get("volume_label"))}:
        return None
    toks = [x for x in re.split(r"[_\s.]+", part) if x]
    if len(toks) < 2:  # one word, like the disc's own name: no structure to read
        return None
    kind, words = "", []
    for tok in toks:
        low = tok.lower()
        if low in AUTHORED_KIND:
            kind = kind or AUTHORED_KIND[low]
            if low in ("teaser", "trailer", "musicvideo"):
                words.append(tok)
            continue
        if low in AUTHORED_NOISE or re.fullmatch(r"\d+|t\d+", low):
            continue
        words.append(tok)
    marked = any(x.lower() in ("seg", "sf", "extras", "bonus", "tmpl") for x in toks)  # a studio's bonus-menu naming
    if not words or not (kind or marked or any(re.search(r"playall|menu|screensaver", w, re.I) for w in words)):
        return None  # no tag we know: don't guess a name from it
    spaced = lambda w: re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)", " ", w)
    name = " ".join(spaced(w) for w in words)
    name = re.sub(r"\bPlay All\b|\bPlayall\b", "Play All", name, flags=re.I)
    name = re.sub(r"\bScreen Saver\b|\bScreensaver\b", "Screen Saver", name, flags=re.I)
    name = " ".join(w if w.isupper() and len(w) > 1 else w[:1].upper() + w[1:] for w in name.split())
    if kind == "Trailers" and not re.search(r"trailer|teaser", name, re.I):
        name += " Trailer"
    if not kind:
        import extras
        kind = extras.guess_category(name)
    return name, kind


def extra_item(t: dict, idx: int, base_dir: str, cfg: dict, disc: dict | None = None) -> dict:
    d = t["duration_seconds"]
    item = {
        "source": t["output_file"], "duration": t.get("duration"), "duration_seconds": d,
        "role": "extra", "category": "Other",
        "name": f"Extra {idx:02d} ({mmss(d)})",
        "action": "move", "note": "",
    }
    if t.get("_duplicate_of"):
        item.update(action="skip", name=f"Unused clip ({mmss(d)}, t{t['title_id']:02d})",
                    note=f"same length as {t['_duplicate_of']} (duplicate)")
    elif d < cfg["skip_extras_under_seconds"]:
        item.update(action="skip", name=f"Unused clip ({mmss(d)}, t{t['title_id']:02d})",
                    note="very short: probably a logo, menu or promo")
    else:
        got = authored_name(t, disc)
        if got:
            item.update(name=got[0], category=got[1], note="named from the disc's own playlist name")
    item["dest_dir"] = base_dir
    return item


def name_editions(items: list[dict], base: str, tmdb_min: int, label: str, warnings: list[str],
                  cfg: dict | None = None) -> None:
    """Name the film's cuts for Plex ({edition-Theatrical}, {edition-Extended}, ...) when a disc
    holds more than one. The cut whose length matches TMDB's runtime is the theatrical one; the
    disc label can say what the other is (Director's Cut, Unrated, Extended)."""
    cuts = [it for it in items if it["role"] == "main"] + \
           [it for it in items if it["role"] == "edition" and it["action"] == "move"]
    if len(cuts) < 2:
        return
    up = label.upper().replace("_", " ")
    other = ("Director's Cut" if "DIRECTOR" in up else "Unrated" if "UNRATED" in up else
             "Extended" if "EXTENDED" in up else "Extended")
    mins = lambda it: it["duration_seconds"] / 60
    theatrical = None
    if tmdb_min:
        close = sorted(cuts, key=lambda it: abs(mins(it) - tmdb_min))
        if abs(mins(close[0]) - tmdb_min) <= 3:
            theatrical = close[0]
    sure = theatrical is not None and len(cuts) == 2 and theatrical is not cuts[0]
    if theatrical is None:  # no runtime to go by: the longest is the extended cut
        theatrical = min(cuts, key=mins)
    names = {}
    for it in cuts:
        if it is theatrical:
            names[id(it)] = "Theatrical"
        elif mins(it) > mins(theatrical) and other not in names.values():
            names[id(it)] = other
        else:
            names[id(it)] = "Alternate"
    for it in cuts:
        it["name"] = naming.edition_name(cfg or {}, base, names[id(it)])
        it["edition"] = names[id(it)]
        it["note"] = f"{names[id(it)]} cut ({hms(it['duration_seconds'])})" + (
            ", matches TMDB's runtime" if it is theatrical and sure else "")
    if sure:  # the theatrical cut matches TMDB, so the longer main feature isn't a wrong match
        warnings[:] = [w for w in warnings if not w.startswith("Runtime doesn't match")]
    else:
        warnings.append("Disc has " + " and ".join(f"{names[id(it)]} ({hms(it['duration_seconds'])})" for it in cuts)
                        + " cuts of the film, named by length. Check the edition names.")


def hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def plan_movie(folder: Path, disc: dict, titles: list[dict], label_info: dict,
               tmdb: TMDB | None, lib: dict, cfg: dict, warnings: list[str]) -> dict:
    main = max(titles, key=lambda t: t["duration_seconds"])
    main_min = main["duration_seconds"] / 60

    # --- identify on TMDB, preferring the candidate whose runtime matches the main feature
    queries = []
    disc_name = disc.get("name") or ""
    if disc_name and norm(disc_name) != norm(disc.get("disc_label", "")):
        queries.append(parse_label(disc_name)["query"])
    queries.append(label_info["query"])
    candidates: list[dict] = []
    if tmdb and label_info.get("force_id"):
        candidates = [tmdb.movie(int(label_info["force_id"]))]
    elif tmdb:
        for q in dict.fromkeys(q for q in queries if q):
            words, found = q.split(), []
            while words and not found:  # 'Transformers: Revenge Of The Fallen Bse' -> '... Fallen'
                found = tmdb.search("movie", " ".join(words))[:6]
                if found or len(words) <= 2 or candidates:  # shorten only while nothing at all is found
                    break
                words = words[:-1]
            for r in found:
                if all(c["id"] != r["id"] for c in candidates):
                    candidates.append(r)
        # A label with the spaces squeezed out ("RATRACE") finds the wrong film, or none. If nothing
        # found has the disc's length, also try it split into two words ("Rat Race").
        lone = re.sub(r"[^A-Za-z]", "", label_info.get("query") or "")
        if lone and " " not in (label_info.get("query") or "").strip() and 5 <= len(lone) <= 16 \
                and not any(c.get("id") == label_info.get("force_id") for c in candidates):
            have_len = False
            for r in candidates[:6]:
                try:
                    rt = tmdb.movie(r["id"]).get("runtime") or 0
                except Exception:
                    rt = 0
                r["_runtime"] = rt
                have_len = have_len or (rt and abs(rt - main_min) <= 6)
            if not have_len:
                for k in range(2, len(lone) - 1):
                    q = f"{lone[:k]} {lone[k:]}"
                    try:
                        found = tmdb.search("movie", q)[:3]
                    except Exception:
                        continue
                    for r in found:
                        if norm(r.get("title") or "").replace(" ", "") == lone.lower() and \
                                all(c["id"] != r["id"] for c in candidates):
                            candidates.insert(0, r)
        scored = []
        for r in candidates[:8]:
            if "_runtime" in r:
                runtime = r.pop("_runtime")
            else:
                try:
                    runtime = tmdb.movie(r["id"]).get("runtime") or 0
                except Exception:
                    runtime = 0
            diff = abs(runtime - main_min) if runtime else 999
            scored.append((diff, -r.get("popularity", 0), r, runtime))
        scored.sort(key=lambda x: (x[0] > 6, x[1] if x[0] <= 6 else x[0]))
        candidates = [dict(s[2], runtime=s[3]) for s in scored]
    match = candidates[0] if candidates else None
    if not match:
        warnings.append("No TMDB match found. Set the title on the review page.")
        title, year, tid = label_info["query"] or disc.get("disc_label", "Unknown"), "", ""
    else:
        title, year, tid = match["title"], year_of(match.get("release_date")), str(match["id"])
        if match.get("runtime") and abs(match["runtime"] - main_min) > 6:
            warnings.append(f"Possibly the wrong movie: the disc's movie is {main_min:.0f} min, but TMDB says "
                            f"{match['title']} ({year}) is {match['runtime']} min. Check the cover, or pick the right one.")
        elif not match.get("runtime") and not label_info.get("force_id"):
            warnings.append(f"TMDB doesn't list a length for {match['title']} ({year}), so Shelfrip couldn't check "
                            "it's the right movie. Check the cover.")

    # --- OMDb (IMDb data) as a second opinion on run time and year
    checked = ""
    if tid and cfg.get("omdb_api_key") and "paste" not in cfg.get("omdb_api_key", ""):
        try:
            import crosscheck
            imdb_id = (tmdb.movie(int(tid)).get("imdb_id") if tmdb else "") or ""
            om = crosscheck.omdb_movie(cfg["omdb_api_key"], imdb_id, title, year)
        except Exception as exc:
            om = None
            print(f"    (OMDb check skipped: {exc})")
        if om:
            checked = "TMDB, checked with OMDb"
            tmdb_warn = [w for w in warnings if w.startswith("Runtime doesn't match")]
            if om["runtime"]:
                fits = abs(om["runtime"] - main_min) <= 6
                if fits and tmdb_warn:
                    warnings[:] = [w for w in warnings if w not in tmdb_warn]
                    checked += f" (its {om['runtime']} min matches the disc)"
                elif not fits and tmdb_warn:
                    warnings[warnings.index(tmdb_warn[0])] += (
                        f" OMDb also says {om['runtime']} min, so this may be a different cut or the wrong movie.")
                elif not fits:
                    warnings.append(f"OMDb says {om['runtime']} min but the disc's movie is {main_min:.0f} min. "
                                    "Check it's the right movie or version.")
            if om["year"].isdigit() and str(year).isdigit() and abs(int(om["year"]) - int(year)) > 1:
                warnings.append(f"OMDb dates this film {om['year']}, TMDB says {year}. Check the match.")

    fmt_folder = cfg["movie_format_folders"][disc_format(disc)]
    base_name = fs_safe(f"{title} ({year})" if year else title)
    tag = (" " + naming.id_tag(cfg, tid)) if naming.id_tag(cfg, tid) else ""
    top = library_dirs(cfg)["movies"]
    movie_dir = (f"{top}\\{fmt_folder}\\" if cfg.get("format_folders") else f"{top}\\") + f"{base_name}{tag}"
    for m in lib["movies"]:
        if tid and m["id"] == tid:
            warnings.append("Already in library: " + "\\".join(x for x in (m["format_folder"], m["folder_name"]) if x))

    items = [{
        "source": main["output_file"], "duration": main.get("duration"),
        "duration_seconds": main["duration_seconds"], "role": "main",
        "dest_dir": movie_dir, "name": f"{base_name}{tag}", "action": "move", "note": "main feature",
    }]
    n = 0
    for t in sorted(titles, key=lambda t: t["title_id"]):
        if t is main:
            continue
        d = t["duration_seconds"]
        if d >= 0.9 * main["duration_seconds"]:
            same = abs(d - main["duration_seconds"]) <= 60
            items.append({
                "source": t["output_file"], "duration": t.get("duration"), "duration_seconds": d,
                "role": "edition", "dest_dir": movie_dir,
                "name": naming.edition_name(cfg, f"{base_name}{tag}", "Alternate"),
                "action": "skip" if same else "move",
                "note": ("almost identical length to the main feature: usually a different "
                         "language or sing-along version" if same else
                         "another cut of the film: set the edition name"),
            })
            continue
        item = extra_item(t, n + 1, movie_dir, cfg, disc)
        n += item["action"] != "skip"
        items.append(item)
    name_editions(items, f"{base_name}{tag}", (match or {}).get("runtime") or 0,
                  f"{disc.get('disc_label', '')} {disc.get('name', '')}", warnings, cfg)
    return {
        "kind": "movie", "title": title, "year": year, "tmdb_id": tid,
        "format": fmt_folder, "library_dir": movie_dir, "items": items,
        **({"match_source": checked} if checked else {}),
        "tmdb_candidates": [{"id": c["id"], "title": c.get("title"), "year": year_of(c.get("release_date")),
                             "runtime": c.get("runtime")} for c in candidates[:6]],
    }


def plan_tv(folder: Path, rip_root: Path, disc: dict, titles: list[dict], label_info: dict,
            tmdb: TMDB | None, lib: dict, lib_show: dict | None, cfg: dict,
            warnings: list[str]) -> dict:
    season = label_info["season"] or 1
    if label_info["season"] is None:
        warnings.append("Season number not in the disc label. Assumed Season 1.")

    # --- identify the show
    candidates: list[dict] = []
    if label_info.get("force_id"):
        fid = str(label_info["force_id"])
        lib_show = next((s for s in lib["shows"] if s["id_src"] == "tmdb" and s["id"] == fid), None)
        if not lib_show and tmdb:
            candidates = [tmdb.tv(int(fid))]
    if lib_show and lib_show["id_src"] == "tmdb":
        title, year, tid = lib_show["title"], lib_show["year"], lib_show["id"]
        show_dir_name = lib_show["folder_name"]
        source = f"existing library folder ({lib_show['format_folder']})"
    elif lib_show:
        title, year, tid = lib_show["title"], lib_show["year"], ""
        show_dir_name = lib_show["folder_name"]
        source = "existing library folder"
    else:
        if tmdb and not candidates:
            q = label_info["query"]
            words = q.split()
            while words and not candidates:  # 'Transformers G1' -> 'Transformers'
                candidates = tmdb.search("tv", " ".join(words))[:6]
                words = words[:-1]
        if candidates:
            c = candidates[0]
            title, year, tid = c["name"], year_of(c.get("first_air_date")), str(c["id"])
        else:
            title, year, tid = label_info["query"] or "Unknown Show", "", ""
            warnings.append("No TMDB match found. Set the show on the review page.")
        id_tag = naming.id_tag(cfg, tid)
        show_dir_name = fs_safe(f"{title} ({year})" if year else title) + (f" {id_tag}" if id_tag else "")
        source = "TMDB search"

    fmt_folder = cfg["tv_format_folders"][disc_format(disc)]
    top = library_dirs(cfg)["tv"]
    show_dir = (f"{top}\\{fmt_folder}\\" if cfg.get("format_folders") else f"{top}\\") + show_dir_name
    season_dir = f"{show_dir}\\Season {season}"
    file_prefix = fs_safe(f"{title} ({year})" if year else title)
    show_key = tid or norm(title)

    # --- TMDB episode list for names and runtimes
    tmdb_eps: dict[int, dict] = {}
    if tmdb and tid:
        for e in tmdb.season(int(tid), season).get("episodes", []):
            tmdb_eps[e["episode_number"]] = e

    # --- TVmaze as a second opinion on episode names, count and length
    maze_eps: dict[int, dict] = {}
    if cfg.get("use_tvmaze", True) and (tid or title):
        try:
            import crosscheck
            maze = crosscheck.tvmaze_season(tmdb, tid, title, year, season)
            maze_eps = (maze or {}).get("episodes", {})
        except Exception as exc:
            print(f"    (TVmaze check skipped: {exc})")
    if tmdb_eps and maze_eps and len(maze_eps) != len(tmdb_eps):
        warnings.append(f"TMDB lists {len(tmdb_eps)} episodes in season {season} but TVmaze lists "
                        f"{len(maze_eps)}. The two may number episodes differently, so check against the disc case.")

    # --- which titles are episodes?
    longs = [t for t in titles if t["duration_seconds"] >= 10 * 60]
    total_long = sum(t["duration_seconds"] for t in longs)
    play_all = set()
    for t in longs:
        others = total_long - t["duration_seconds"]
        if len(longs) > 2 and others and abs(t["duration_seconds"] - others) <= 0.03 * others:
            play_all.add(t["output_file"])
    singles = [t["duration_seconds"] for t in longs if t["output_file"] not in play_all]

    # Episode length, best source first: episodes on other discs of this show you've
    # ripped; TMDB's runtimes for this season (unless this disc clearly disagrees: old
    # shows often list the 30-minute slot instead of the real 22 minutes); this disc.
    tol = lambda base: max(0.15 * base, 180)
    peers = episode_lengths_from_plans(rip_root, folder, show_key)
    runtimes = sorted({e["runtime"] * 60 for e in tmdb_eps.values() if e.get("runtime")})
    if not runtimes and tmdb and tid:
        try:
            runtimes = sorted({m * 60 for m in tmdb.tv(int(tid)).get("episode_run_time") or [] if m})
        except Exception:
            pass
    db_src = "TMDB"
    if not runtimes:
        runtimes = sorted({e["runtime"] * 60 for e in maze_eps.values() if e.get("runtime")})
        db_src = "TVmaze"
    fits = lambda d, refs: any(abs(d - r) <= tol(r) for r in refs)
    if runtimes and len(singles) >= 3 and sum(fits(d, runtimes) for d in singles) * 2 < len(singles):
        runtimes = []
        warnings.append(f"Episode lengths on the disc don't match {db_src}'s runtimes, so the disc's own lengths were used.")
    length_src = db_src if runtimes else "the other episodes"
    if len(peers) >= 3:
        runtimes, length_src = [statistics.median(peers)], "other discs of this show"
    if not runtimes and singles:
        med = statistics.median(singles)
        cluster = [d for d in singles if abs(d - med) <= tol(med)]
        runtimes = [statistics.median(cluster)] if cluster else [med]
    typical = statistics.median(runtimes) if runtimes else 0

    episode_titles, items, notes, odd = [], [], {}, {}
    for t in sorted(titles, key=lambda t: t["title_id"]):
        d = t["duration_seconds"]
        if d < 10 * 60 or not typical:
            continue
        if t["output_file"] in play_all:
            notes[t["output_file"]] = "'Play All' title: same as the episodes combined"
            continue
        if fits(d, runtimes):
            episode_titles.append(t)
            continue
        k = round(d / typical)
        if k >= 2 and abs(d - k * typical) <= k * tol(typical):
            notes[t["output_file"]] = f"long video: probably {k} episodes in one"
            t["_multi"] = k
            episode_titles.append(t)
            continue
        odd[t["output_file"]] = (f"{mmss(d)} doesn't fit an episode (about {round(typical / 60)} min per "
                                 f"{length_src}), so it's treated as an extra. Change the Type if it is an episode.")

    have = episodes_in_library(lib, tid, norm(title), season)
    claimed = episodes_claimed_by_plans(rip_root, folder, show_key, season)
    next_ep = max(have | claimed | {0}) + 1
    if have:
        source += f"; library already has S{season:02d} up to e{max(have):02d}"
    if claimed:
        source += f"; other ripped discs claim up to e{max(claimed):02d}"

    if label_info.get("continue_from"):
        # another disc of this set was filed: carry on from its last episode
        next_ep = max([label_info["continue_from"] + 1] + [e + 1 for e in claimed if e > label_info["continue_from"]])
        source += f"; continues after e{label_info['continue_from']:02d} from an earlier disc of this set"
    # Box-set seasons (e.g. Funimation's "Season 2 Part 5") often don't match TMDB's seasons.
    # If this disc continues past the end of TMDB's season, move on to TMDB's next season.
    elif tmdb and tid and tmdb_eps and episode_titles and next_ep > max(tmdb_eps):
        nxt = {e["episode_number"]: e for e in tmdb.season(int(tid), season + 1).get("episodes", [])}
        if nxt:
            last = max(tmdb_eps)
            absolute = min(nxt) > 1          # e.g. One Piece: season 3 runs e78-e91
            start = next_ep if absolute else max(1, next_ep - last)
            claimed_new = episodes_claimed_by_plans(rip_root, folder, show_key, season + 1)
            if claimed_new:
                start = max(start, max(claimed_new) + 1)
            warnings.append(f"TMDB's season {season} ends at e{last:02d}, so the disc label's season numbering "
                            f"doesn't match TMDB's. Numbered as TMDB season {season + 1} from e{start:02d}: check the "
                            f"first episode on the disc case and set Season and First episode if it's wrong.")
            source += f"; moved to TMDB season {season + 1}"
            season, tmdb_eps, maze_eps, next_ep = season + 1, nxt, {}, start
            season_dir = f"{show_dir}\\Season {season}"

    for t in episode_titles:
        count = t.get("_multi", 1)
        eps = list(range(next_ep, next_ep + count))
        next_ep += count
        if count == 1:
            e = eps[0]
            t_name = tmdb_eps.get(e, {}).get("name", "")
            m_name = maze_eps.get(e, {}).get("name", "")
            generic = re.fullmatch(r"(episode|ep\.?)\s*\d+", t_name.strip(), re.I) is not None
            ep_title = fs_safe(t_name)  # TMDB is always the source for episode names and numbers
            name = f"{file_prefix} - s{season:02d}e{e:02d}" + (f" - {ep_title}" if ep_title else "")
            notes_e = []
            src_eps = tmdb_eps if length_src == "TMDB" else maze_eps if length_src == "TVmaze" else {}
            rt = src_eps.get(e, {}).get("runtime")
            if rt and abs(rt * 60 - t["duration_seconds"]) > tol(rt * 60):
                notes_e.append(f"length {mmss(t['duration_seconds'])} vs {length_src} {rt} min: check the episode number")
            if e not in tmdb_eps and tmdb_eps:
                notes_e.append(f"TMDB lists only {len(tmdb_eps)} episodes in season {season}")
            if e not in maze_eps and maze_eps:
                notes_e.append(f"TVmaze lists only {len(maze_eps)} episodes in season {season}")
            import crosscheck
            if m_name and (generic or not t_name or not crosscheck.same_name(t_name, m_name)):
                notes_e.append(f"TVmaze calls this episode \"{m_name}\"")
            note = "; ".join(notes_e)
        else:
            name = f"{file_prefix} - s{season:02d}e{eps[0]:02d}-e{eps[-1]:02d}"
            note = notes.get(t["output_file"], "")
        items.append({
            "source": t["output_file"], "duration": t.get("duration"),
            "duration_seconds": t["duration_seconds"], "role": "episode",
            "season": season, "episode": eps[0], "episodes": eps,
            "needs_split": count > 1, "dest_dir": season_dir, "name": name,
            "action": "move", "note": note,
        })

    ep_files = {i["source"] for i in items}
    n = 0
    for t in sorted(titles, key=lambda t: t["title_id"]):
        if t["output_file"] in ep_files:
            continue
        if t["output_file"] in notes:
            items.append({"source": t["output_file"], "duration": t.get("duration"),
                          "duration_seconds": t["duration_seconds"], "role": "play_all",
                          "dest_dir": season_dir, "name": "", "action": "skip",
                          "note": notes[t["output_file"]]})
            continue
        item = extra_item(t, n + 1, season_dir, cfg, disc)
        if t["output_file"] in odd:
            item["category"] = "Featurettes"
            item["note"] = odd[t["output_file"]]
        n += item["action"] != "skip"
        items.append(item)

    if not episode_titles:
        warnings.append("No episode-length titles found. This may be a bonus disc.")
    if any(i.get("needs_split") for i in items):
        warnings.append("Contains a long video holding several episodes. Check where each episode starts "
                        "(under that row) before filing; it is cut into separate files while filing.")

    return {
        "kind": "tv", "title": title, "year": year, "tmdb_id": tid, "show_key": show_key,
        "season": season, "format": fmt_folder, "library_dir": show_dir, "items": items,
        "match_source": source,
        "tmdb_candidates": [{"id": c["id"], "title": c.get("name"), "year": year_of(c.get("first_air_date"))}
                            for c in candidates[:6]],
    }


# What people skip, learned from real filings: menus, warnings and logos; clips too short to be
# anything that nobody could name; and "Play All" titles whose pieces are kept anyway.
CLUTTER = re.compile(r"\b(menu|copyright|fbi|piracy|warning|(disc|bonus features?) credits|easter egg|digital copy|"
                     r"sneak peeks?|previews|promo|disneyparks(\.com)?|logo|screen ?saver)\b", re.I)
PLACEHOLDER = re.compile(r"^(Extra \d+|Unused clip) \(", re.I)
PLAY_ALL = re.compile(r"play all|\((all|play all)\)|\s-\s+all\b", re.I)


def _sums_others(it: dict, extras: list[dict]) -> bool:
    """A run of 3+ neighbouring extras adds up to this one's length: it plays them all."""
    d = int(it.get("duration_seconds") or 0)
    lens = [int(o.get("duration_seconds") or 0) for o in extras if o is not it]
    slack = 2  # seconds
    for i in range(len(lens)):
        total = 0
        for j in range(i, len(lens)):
            total += lens[j]
            if total > d + slack:
                break
            if j > i + 1 and abs(total - d) <= slack:
                return True
    return False


OWN_TRAILER = re.compile(r"\b(trailers?|teasers?|tv spots?|spots?|promos?)\b", re.I)
TITLE_STOP = {"the", "and", "movie", "film", "season", "part", "with", "from", "edition", "collection"}


def _title_words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", re.sub(r"[-']", "", title.lower())) if len(w) >= 4 and w not in TITLE_STOP}


def _same_length_run(it, extras) -> bool:
    """3 or more unnamed clips of the same length (within a second), 30 s to 3 min: a reel of trailers."""
    d = int(it.get("duration_seconds") or 0)
    if not 30 <= d <= 180:
        return False
    same = [o for o in extras if PLACEHOLDER.match(str(o.get("name") or "")) and abs(int(o.get("duration_seconds") or 0) - d) <= 1]
    return len(same) >= 3


def skip_clutter(items: list[dict], title: str = "") -> None:
    extras = [it for it in items if it.get("role") == "extra" and it.get("action") == "move"]
    own = _title_words(title)
    for it in extras:
        name, d = str(it.get("name") or ""), int(it.get("duration_seconds") or 0)
        why = ""
        if CLUTTER.search(name):
            why = "looks like a menu, warning or promo"
        elif PLACEHOLDER.match(name) and d < 45:
            why = "short, and nothing could name it"
        elif PLACEHOLDER.match(name) and _sums_others(it, extras):
            why = "about as long as the other extras together: probably a Play All"
        elif PLACEHOLDER.match(name) and _same_length_run(it, extras):
            why = "one of several unnamed clips of the same length: probably trailers for other titles"
        elif (it.get("category") == "Trailers" and own and not OWN_TRAILER.search(name)
              and not PLACEHOLDER.match(name) and not (_title_words(name) & own)):
            why = "looks like a trailer for another title"
        elif PLAY_ALL.search(name):
            parts = sum(int(o.get("duration_seconds") or 0) for o in extras
                        if o is not it and o.get("action") == "move" and not PLAY_ALL.search(str(o.get("name") or ""))
                        and (o.get("category") or "") == (it.get("category") or "") and (o.get("duration_seconds") or 0) < d)
            if parts >= 0.7 * d:
                why = "its pieces are kept separately"
        if why:
            it["action"] = "skip"
            it["note"] = "; ".join(x for x in (f"skipped: {why}. Set Keep to add it anyway", it.get("note")) if x)


def skip_scenes(plan: dict, titles: list[dict]) -> None:
    """Movie discs that also list every scene as its own title: set those unnamed scene titles to Skip.
    Everything in them is in the feature being filed. Same rules as dupes.scene_pieces (used before ripping)."""
    if plan.get("kind") != "movie":
        return
    import dupes
    by_file = {t.get("output_file"): t for t in titles}
    feats, trusted = [], set()
    for it in plan["items"]:
        t = by_file.get(it.get("source"))
        if t is None or it.get("action") == "skip" or t.get("duration_seconds", 0) < dupes.FEATURE_MIN_SECONDS:
            continue
        feats.append(t)
        if it.get("role") in ("main", "edition"):
            trusted.add(t["title_id"])
    if not feats:
        return
    open_extras = {it["source"]: it for it in plan["items"] if it.get("role") == "extra" and it.get("action") == "move"
                   and PLACEHOLDER.match(str(it.get("name") or ""))}
    # titles you'd keep under a real name are never scenes (and end a run of DVD scenes)
    kept = {it["source"] for it in plan["items"] if it.get("action") != "skip" and it["source"] not in open_extras}
    named = {t["title_id"] for t in titles if t.get("output_file") in kept}
    pieces = dupes.scene_pieces(titles, feats, trusted, keep=named - {f["title_id"] for f in feats})
    names = {t["title_id"]: next((it.get("name") for it in plan["items"] if it.get("source") == t["output_file"]), "")
             for t in feats}
    for tid, (fid, _why) in pieces.items():
        it = open_extras.get(next((t["output_file"] for t in titles if t["title_id"] == tid), None))
        if it is None:
            continue
        it["action"] = "skip"
        it["note"] = "; ".join(x for x in (f"skipped: a scene from {names.get(fid) or f'title {fid}'}, which has all of it. "
                                           "Set Keep to add it anyway", it.get("note")) if x)


def plan_folder(folder: Path, rip_root: Path, tmdb: TMDB | None, lib: dict, cfg: dict,
                override: dict | None = None) -> dict | None:
    """override (from the review page): {"kind": "movie"|"tv", "tmdb_id": "123", "season": 2}"""
    info_path = folder / "disc_info.json"
    if not info_path.exists():
        return None
    disc = json.loads(info_path.read_text(encoding="utf-8"))
    titles = ripped_titles(folder, disc)
    if not titles:
        return None
    mark_duplicates(titles)
    label = disc.get("disc_label") or folder.name.split("__")[0]
    label_info = parse_label(label)
    lib_show = library_show_match(lib, label_info["query"])
    warnings: list[str] = []
    override = override or {}
    if override.get("tmdb_id"):
        label_info["force_id"] = str(override["tmdb_id"])
    if override.get("season"):
        label_info["season"] = int(override["season"])

    # --- recognise the disc before guessing: TheDiscDB, then labels you've filed before
    known, known_source = None, ""
    if not override.get("kind") and not override.get("tmdb_id"):
        import discdb
        try:
            import catalog
            known = catalog.lookup(disc, titles)
        except Exception as exc:
            print(f"    (disc catalog lookup failed: {exc})")
        if not known:
            try:
                import community
                known = community.lookup(disc, titles, cfg)
            except Exception as exc:
                print(f"    (Shelfrip disc database lookup failed: {exc})")
        if not known and cfg.get("use_thediscdb", True):
            known = discdb.lookup(disc, titles)
        if known and known["tmdb_id"]:
            known_source = known["source"]
            label_info["force_id"] = known["tmdb_id"]
            override = {"kind": known["kind"]}
            seasons = discdb.tdb_seasons(known)
            if known["kind"] == "tv" and seasons and label_info["season"] is None:
                label_info["season"] = min(seasons)
        else:
            known = None
            prev = discdb.learned(label)
            if prev and prev.get("tmdb_id"):
                label_info["force_id"] = str(prev["tmdb_id"])
                override = {"kind": prev.get("kind") or "movie"}
                if prev.get("season") is not None and label_info["season"] is None:
                    label_info["season"] = int(prev["season"])
                if prev.get("last_episode"):
                    label_info["continue_from"] = int(prev["last_episode"])
                known_source = (f"an earlier disc with the same label ({prev.get('title')}"
                                f"{' ' + str(prev['year']) if prev.get('year') else ''})")
                if prev.get("kind") == "tv":
                    lib_show = None  # use the remembered show, not a fuzzy library match

    is_tv = override["kind"] == "tv" if override.get("kind") else looks_like_tv(titles, label_info, lib_show)
    if is_tv:
        plan = plan_tv(folder, rip_root, disc, titles, label_info, tmdb, lib, lib_show, cfg, warnings)
    else:
        plan = plan_movie(folder, disc, titles, label_info, tmdb, lib, cfg, warnings)
    plan.update({
        "status": "proposed", "disc_label": label, "disc_folder": str(folder),
        "planner_version": PLANNER_VERSION,
        "warnings": warnings, "search_query": label_info["query"],
    })
    if known_source:
        plan["match_source"] = known_source + (f"; {plan['match_source']}" if plan.get("match_source") else "")
    if known:
        apply_discdb(plan, known, tmdb)
        if known.get("community"):  # remembered, so a correction can be reported back
            plan["community_answer"] = {"kind": known["kind"], "tmdb_id": known["tmdb_id"], "season": known.get("season"),
                                        "title": known.get("title")}
        if known.get("community") and not known.get("confirmed"):
            plan["warnings"].append("Only one person has shared this disc with the Shelfrip disc database so far: "
                            "check the names before filing.")
    if plan["kind"] == "tv":
        attach_splits(plan, folder, tmdb, cfg)
    try:
        mark_library_copies(plan, lib, cfg)
    except Exception as exc:
        print(f"    (couldn't check the library for copies: {exc})")
    if tmdb and cfg.get("name_extras", True):
        try:
            import extras
            extras.name_extras(plan, disc, warnings, use_dvdcompare=bool(cfg.get("use_dvdcompare")))
        except Exception as exc:
            print(f"    (couldn't look up extras names: {exc})")
    for it in plan["items"]:
        if it.get("role") == "extra" and it.get("name"):
            it["name"] = capital_case(it["name"])
    if cfg.get("skip_clutter", True):
        skip_clutter(plan["items"], plan.get("title") or "")
        try:
            skip_scenes(plan, titles)
        except Exception as exc:
            print(f"    (couldn't check for scene-by-scene titles: {exc})")
    try:
        attach_audio(plan, folder, disc)
    except Exception as exc:
        print(f"    (couldn't read the audio tracks: {exc})")
    if disc.get("skipped_duplicates"):
        n = disc["skipped_duplicates"]
        plan["warnings"].append(f"{n} extra cop{'y' if n == 1 else 'ies'} of the main feature {'was' if n == 1 else 'were'} "
                                "left out before ripping (Settings, Ripping: Rip the main feature only once). "
                                "Use Rip again with that setting off if something is missing.")
    if disc.get("skipped_scenes"):
        n = disc["skipped_scenes"]
        plan["warnings"].append(f"{n} scene-by-scene title{'' if n == 1 else 's'} (the movie cut into separate scenes) "
                                f"{'was' if n == 1 else 'were'} left out before ripping, as the main feature has all of "
                                "it (Settings, Ripping: Rip the main feature only once).")
    if disc.get("read_errors") or disc.get("titles_failed"):
        bits = [f"{disc['read_errors']} read error(s)" if disc.get("read_errors") else "",
                f"{disc['titles_failed']} title(s) failed" if disc.get("titles_failed") else ""]
        plan["warnings"].append("The disc had " + " and ".join(b for b in bits if b) +
                                " (scratched or dirty?). Play the files to check them before filing.")
    if plan.get("community_answer"):
        import community
        community.note_shown(plan, disc)  # so filing it unchanged counts as half a vote
    return plan


def split_names(plan: dict, season: int, eps: list[int], tmdb: TMDB | None) -> list[str]:
    """Plex names for episodes `eps` of `season`, names from TMDB."""
    prefix = fs_safe(f"{plan['title']} ({plan['year']})" if plan.get("year") else plan["title"])
    names = {}
    if tmdb and plan.get("tmdb_id"):
        names = {x["episode_number"]: x.get("name") or "" for x in
                 tmdb.season(int(plan["tmdb_id"]), season).get("episodes", [])}
    out = []
    for e in eps:
        t = fs_safe(names.get(e, ""))
        out.append(f"{prefix} - s{season:02d}e{e:02d}" + (f" - {t}" if t else ""))
    return out


def _shift_later_episodes(plan: dict, after: dict, season: int, from_ep: int, delta: int) -> None:
    """Renumber episodes that come after `after` when its episode count changed."""
    seen = False
    for it in plan["items"]:
        if it is after:
            seen = True
            continue
        if not seen or it.get("role") != "episode" or (it.get("season") or 0) != season:
            continue
        eps = it.get("episodes") or [it.get("episode")]
        if not eps or not eps[0] or eps[0] < from_ep:
            continue
        new = [e + delta for e in eps]
        it["episodes"], it["episode"] = new, new[0]
        code = f"s{season:02d}e{new[0]:02d}" + (f"-e{new[-1]:02d}" if len(new) > 1 else "")
        it["name"] = re.sub(r"s\d+e\d+(?:-e\d+)?", code, it.get("name") or "", count=1, flags=re.I)


def attach_splits(plan: dict, folder: Path, tmdb: TMDB | None, cfg: dict) -> None:
    """For each video holding several episodes, read its chapters and propose the cuts."""
    import split
    try:
        segs = {t.get("output_file"): t.get("segments") or "" for t in
                json.loads((folder / "disc_info.json").read_text(encoding="utf-8")).get("titles", [])}
    except Exception:
        segs = {}
    for it in plan["items"]:
        eps = it.get("episodes") or []
        if not it.get("needs_split") or len(eps) < 2:
            continue
        if not split.available(cfg):
            it["note"] = "; ".join(x for x in (it.get("note"), "install MKVToolNix to split this into episodes "
                                   "(or set mkvtoolnix_dir in config.json)") if x)
            continue
        try:
            chapters, total = split.read_chapters(folder / it["source"], cfg)
        except Exception as exc:
            it["note"] = "; ".join(x for x in (it.get("note"), f"couldn't read its chapters ({exc})") if x)
            continue
        if not total:
            total = float(it.get("duration_seconds") or 0)
        if len(chapters) < len(eps):
            it["note"] = "; ".join(x for x in (it.get("note"), f"only {len(chapters)} chapter(s), so it can't be "
                                   f"split by chapter. Set it to Skip, or change the Type") if x)
            continue
        # DVDs often store each episode as its own run of chapters: MakeMKV's segment map shows it
        sp = split.propose_from_segments(chapters, total, segs.get(it["source"], ""))
        if sp and len(sp["starts"]) != len(eps):
            season, old = it.get("season") or 0, len(eps)
            eps = list(range(eps[0], eps[0] + len(sp["starts"])))
            _shift_later_episodes(plan, it, season, eps[0] + old, len(eps) - old)
            it["episodes"], it["episode"] = eps, eps[0]
            it["name"] = re.sub(r"s\d+e\d+(?:-e\d+)?", f"s{season:02d}e{eps[0]:02d}-e{eps[-1]:02d}",
                                it.get("name") or "", count=1, flags=re.I)
        layout = bool(sp)
        if not sp:
            sp = split.propose(chapters, total, len(eps))
        sp["names"] = split_names(plan, it.get("season") or 0, eps, tmdb)
        it["split"] = sp
        how = {"high": "the cuts look even", "medium": "check the cuts", "low": "check the cuts carefully"}
        it["note"] = (f"{len(eps)} episodes in one video, split where the disc itself divides them"
                      if layout else f"{len(eps)} episodes in one video, split by chapter ({how[sp['confidence']]})")
        if tmdb and plan.get("tmdb_id") and not any(" - " in n.split(f"e{e:02d}", 1)[-1] for n, e in zip(sp["names"], eps)):
            it["note"] += f"; TMDB has no names for s{(it.get('season') or 0):02d}e{eps[0]:02d}-e{eps[-1]:02d}, so check the season"


def attach_audio(plan: dict, folder: Path, disc: dict) -> None:
    """List each kept feature's audio tracks and spot commentary tracks, so they can be named.

    Sure: the disc marks the track as director's comments, or its name says commentary.
    Guessed: when DVDCompare lists commentaries, extra stereo tracks in the film's language
    after the main surround track. Every track and subtitle is kept either way."""
    by_file = {t.get("output_file"): t for t in disc.get("titles", [])}
    if any(t.get("streams") and not any("desc" in s for s in t["streams"]) for t in by_file.values()):
        try:  # older rips: re-read MakeMKV's scan for the track details
            import rip_all
            fresh = rip_all.parse_disc_info((folder / "makemkv_info.txt").read_text(encoding="utf-8", errors="replace"))
            by_id = {t["title_id"]: t for t in fresh["titles"]}
            for t in by_file.values():
                if t.get("title_id") in by_id:
                    t["streams"] = by_id[t["title_id"]].get("streams", [])
        except Exception:
            pass
    names = list(plan.get("commentaries") or [])
    for it in plan["items"]:
        shared = it.pop("shared_audio", None) or []  # from the Shelfrip disc database (apply_discdb)
        if it.get("role") not in ("main", "edition", "episode"):
            continue
        streams = [s for s in (by_file.get(it["source"]) or {}).get("streams", []) if s.get("type") == "Audio"]
        if not streams:
            continue
        audio = []
        for k, st in enumerate(streams, 1):
            label = st.get("desc") or " ".join(x for x in (st.get("lang_name"), st.get("name")) if x) or f"Track {k}"
            flags = int(st.get("flags") or 0)
            sure = bool(flags & 3) or "comment" in f"{st.get('name', '')} {st.get('desc', '')}".lower()
            audio.append({"i": k, "desc": label, "lang": st.get("lang3") or "", "channels": st.get("channels") or 0,
                          "derived": bool(flags & 2048), "commentary": sure, "guessed": False, "name": ""})
        want = len(names) if it.get("role") in ("main", "edition") else 0
        have = sum(a["commentary"] for a in audio)
        first = audio[0]
        if want > have and first["channels"] > 2:
            for a in audio[1:]:
                if have >= want:
                    break
                if (not a["commentary"] and not a["derived"] and a["lang"] == first["lang"]
                        and 0 < a["channels"] <= 2):
                    a.update(commentary=True, guessed=True)
                    have += 1
        same_layout = len(shared) == len(audio) and all(
            str(x.get("lang") or "") == a["lang"] and int(x.get("ch") or 0) == a["channels"] for x, a in zip(shared, audio))
        n = 0
        for a in audio:
            if a["commentary"]:
                a["name"] = names[n] if n < len(names) else ("Commentary" if have == 1 else f"Commentary {n + 1}")
                n += 1
        from_db = False
        if same_layout:  # the same tracks as people who filed this disc: go with what they said
            for x, a in zip(shared, audio):
                if x.get("commentary") and x.get("name"):
                    a.update(commentary=True, guessed=False, name=str(x["name"]))
                    from_db = True
                elif not x.get("commentary") and a["guessed"]:
                    a.update(commentary=False, guessed=False, name="")
            n = sum(a["commentary"] for a in audio)
        it["audio"] = audio
        if n:
            g = any(a["guessed"] for a in audio)
            note = f"{n} commentary track(s) will be named" + (" (guessed from DVDCompare: check under Audio)" if g else
                                                               " (names from the Shelfrip disc database)" if from_db else "")
            it["note"] = "; ".join(x for x in (it.get("note"), note) if x)


def apply_discdb(plan: dict, known: dict, tmdb: TMDB | None) -> None:
    """Use per-title info from TheDiscDB or your own catalog: which title is the movie,
    episode numbers, extras' names (and, from your catalog, what you skipped last time)."""
    import extras
    pairs = known["pairs"]
    local = bool(known.get("local"))
    src = "your catalog" if local else known.get("src_name") or "TheDiscDB"
    plan["discdb_matched"] = True
    typ = lambda it: (pairs.get(it["source"], {}).get("itemType") or "").lower().replace(" ", "")

    if plan["kind"] == "movie":
        # TheDiscDB may know the main feature better than "longest title" (e.g. 3D vs 2D)
        main = next((it for it in plan["items"] if it["role"] == "main"), None)
        real = next((it for it in plan["items"] if typ(it) == "mainmovie" and it["role"] != "main"), None)
        if main and real and typ(main) != "mainmovie":
            for k in ("role", "name", "note", "action"):
                main[k], real[k] = real[k], main[k]
            real.pop("category", None)
            real["note"] = f"main feature (per {src})"
            if main["role"] == "extra":
                main["category"] = "Other"

    prefix = fs_safe(f"{plan['title']} ({plan['year']})" if plan.get("year") else plan["title"])
    ep_names: dict[int, dict] = {}
    for it in plan["items"]:
        r = pairs.get(it["source"])
        if not r:
            continue
        if known.get("community"):
            if r.get("confidence") is not None:
                it["shared_confidence"] = int(r["confidence"])  # shown on the review page
            if isinstance(r.get("tracks"), dict):
                it["shared_audio"] = r["tracks"].get("audio") or []  # commentary names, used by attach_audio
        t = typ(it)
        desc = (r.get("description") or "").strip()
        if plan["kind"] == "tv" and t == "episode" and str(r.get("season")).isdigit() and str(r.get("episode")).isdigit():
            s, e = int(r["season"]), int(r["episode"])
            if s not in ep_names:
                ep_names[s] = {}
                if tmdb and plan.get("tmdb_id"):
                    ep_names[s] = {x["episode_number"]: x for x in
                                   tmdb.season(int(plan["tmdb_id"]), s).get("episodes", [])}
            # TMDB stays the source for episode names; the database only says which title is which
            ep_title = fs_safe(ep_names[s].get(e, {}).get("name", ""))
            note = f"numbered by {src}"
            if ep_names[s] and e not in ep_names[s]:
                note += f"; TMDB has no s{s:02d}e{e:02d}, so check the number" + (f" ({src} calls it \"{desc}\")" if desc else "")
            last = int(r["episode_last"]) if str(r.get("episode_last") or "").isdigit() else e
            eps = list(range(e, max(e, last) + 1))
            if len(eps) > 1:  # one title holding two episodes, e.g. a double-length pilot
                ep_title = fs_safe(ep_names[s].get(e, {}).get("name", "")) if ep_names[s] else ep_title
            it.update(role="episode", season=s, episode=e, episodes=eps, needs_split=False,
                      dest_dir=plan["library_dir"] + ("\\Specials" if s == 0 else f"\\Season {s}"),
                      name=f"{prefix} - s{s:02d}e{e:02d}" + (f"-e{eps[-1]:02d}" if len(eps) > 1 else "")
                           + (f" - {ep_title}" if ep_title else ""),
                      action="move", note=note, db_numbered=True)
            it.pop("category", None)
        elif it["role"] in ("extra", "play_all") and desc and t not in ("", "mainmovie", "episode"):
            dup = it.get("note", "").startswith("same length as")
            it.update(role="extra", name=desc,
                      category=r.get("category") or extras.THEDISCDB_TYPES.get(t) or extras.guess_category(desc),
                      action="skip" if dup else "move", note=f"named by {src}")
        elif (local or known.get("community")) and r.get("kept") is False and it["role"] in ("extra", "play_all", "edition"):
            it.update(action="skip", note="you skipped this title last time (your catalog)" if local
                      else f"skipped by people who filed this disc ({src})")
    numbered = [i for i in plan["items"] if i.get("db_numbered")]
    seen_eps: set = set()
    for i in numbered:
        key = (i["season"], i["episode"])
        if key in seen_eps:
            i.update(action="skip", note=f"another copy of s{key[0]:02d}e{key[1]:02d} (per {src})")
        seen_eps.add(key)
    for i in plan["items"]:
        if i["role"] == "episode" and not i.get("db_numbered") and \
                any(abs(i["duration_seconds"] - n["duration_seconds"]) <= 1 for n in numbered):
            i.update(action="skip", note=f"same length as an episode {src} identified: probably a copy")
    if plan["kind"] == "tv" and numbered:
        plan["warnings"] = [w for w in plan["warnings"] if "Assumed Season 1" not in w]


# --------------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description="Propose Plex names for ripped discs.")
    ap.add_argument("folders", nargs="*", help="specific disc folders (default: all new ones)")
    ap.add_argument("--redo", action="store_true", help="re-plan folders that already have plan.json")
    args = ap.parse_args()

    cfg = load_config()
    rip_root = Path(cfg["output_root"])
    key = cfg.get("tmdb_api_key", "")
    tmdb = TMDB(key) if key and "paste" not in key else None
    if not tmdb:
        print("! No TMDB key in config.json. Titles will come from disc labels only.")
    print(f"Reading Plex library at {cfg['plex_library_root']} ...")
    lib = index_library(Path(cfg["plex_library_root"]), cfg)
    print(f"  {len(lib['movies'])} movies, {len(lib['shows'])} shows")

    folders = [Path(f) for f in args.folders] or sorted(
        (p for p in rip_root.iterdir() if p.is_dir()), key=lambda p: p.name)
    done = 0
    for folder in folders:
        plan_path = folder / "plan.json"
        if plan_path.exists() and not args.redo:
            continue
        if plan_path.exists():
            old = json.loads(plan_path.read_text(encoding="utf-8"))
            if old.get("status") == "filed":
                continue
        try:
            plan = plan_folder(folder, rip_root, tmdb, lib, cfg)
        except Exception as exc:
            print(f"\n{folder.name}: ERROR {exc}")
            continue
        if not plan:
            continue
        plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        done += 1
        print_plan(folder, plan)
    print(f"\nPlanned {done} disc(s). Nothing has been moved yet.")


def print_plan(folder: Path, plan: dict) -> None:
    kind = "TV" if plan["kind"] == "tv" else "Movie"
    print(f"\n=== {folder.name}  ->  {kind}: {plan['title']} ({plan['year']}) "
          f"{{tmdb-{plan['tmdb_id']}}}  [{plan['format']}]")
    if plan.get("match_source"):
        print(f"    matched via {plan['match_source']}")
    for w in plan["warnings"]:
        print(f"    ! {w}")
    for it in plan["items"]:
        dest = f"{it['dest_dir']}\\" + (f"{it['category']}\\" if it.get("category") else "") + f"{it['name']}.mkv"
        flag = "SKIP" if it["action"] == "skip" else "    "
        print(f"  {flag} {it['source']:<42} {it['duration'] or '':>8}  {dest if it['action'] != 'skip' else ''}")
        if it.get("note"):
            print(f"         {'':42}           ({it['note']})")


if __name__ == "__main__":
    main()
