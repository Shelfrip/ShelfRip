r"""
review.py - Step 3 of the Plex disc pipeline: review names, then file into Plex.

Double-click "Review Rips.bat". It:
  1. names any new rips in the rip folder (runs identify.py on folders without a plan.json)
  2. opens a review page in your browser (http://localhost:8765, only reachable from this PC)

On the page you can play any title, fix names and extras categories, switch the
TMDB match, and click "File into Plex". Filing moves each kept file into
the media library (copy, check the size, then delete the original). Once every kept file is
confirmed in the library, the rest of the disc folder (skipped clips included) is deleted to
save space; only its small record files (plan.json, disc_info.json, logs) go to <rip folder>\_Filed.
Existing files in the library are never overwritten.
"""

from __future__ import annotations

import json
import re
import os
import shutil
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import identify
import naming
import paths
import split

# next to the script, or next to Shelfrip.exe when built with PyInstaller
HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
PORT = 8765

def _folder(p: str) -> Path:
    """A folder from the settings. Not chosen yet: a path that doesn't exist, never the current folder."""
    return Path(p) if str(p or "").strip() else Path(paths.DATA_DIR) / "(folder not chosen yet)"


CFG = identify.load_config()
RIP_ROOT = _folder(CFG["output_root"])
LIB_ROOT = _folder(CFG["plex_library_root"])
FILED_DIR = RIP_ROOT / "_Filed"
CONTRIB_DIR = RIP_ROOT / "_Contribute"
KEY = CFG.get("tmdb_api_key", "")
TMDB = identify.TMDB(KEY) if KEY and "paste" not in KEY else None

def reload_config() -> None:
    """Re-read config.json (after the Settings page saves it)."""
    global CFG, RIP_ROOT, LIB_ROOT, FILED_DIR, CONTRIB_DIR, KEY, TMDB
    CFG = identify.load_config()
    RIP_ROOT = _folder(CFG["output_root"])
    LIB_ROOT = _folder(CFG["plex_library_root"])
    FILED_DIR = RIP_ROOT / "_Filed"
    CONTRIB_DIR = RIP_ROOT / "_Contribute"
    KEY = CFG.get("tmdb_api_key", "")
    TMDB = identify.TMDB(KEY) if KEY and "paste" not in KEY else None
    _LIB_CACHE.clear()


JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
_LIB_CACHE: dict = {}


def library(refresh: bool = False) -> dict:
    if refresh or not _LIB_CACHE:
        _LIB_CACHE.clear()
        _LIB_CACHE.update(identify.index_library(LIB_ROOT, CFG))
    return _LIB_CACHE


# --------------------------------------------------------------------------- planning

PLAN_LOCK = threading.Lock()


def save_plan(folder: Path, plan: dict) -> None:
    """Write plan.json safely: to a temporary file first, then swapped in, so a crash or a full
    disk mid-save can never leave a half-written plan (and lose your names)."""
    data = json.dumps({k: v for k, v in plan.items() if k not in ("job", "folder", "auto", "season_files")}, indent=2)
    tmp = folder / "plan.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, folder / "plan.json")


def library_reachable() -> str:
    """'' if the library folder can be reached, else a sentence saying it can't."""
    try:
        if LIB_ROOT.is_dir():
            next(LIB_ROOT.iterdir(), None)  # a dropped network drive can pass is_dir() but fail here
            return ""
    except OSError:
        pass
    return (f"Your library ({LIB_ROOT}) can't be reached right now. If it's on a NAS or network drive, check it's "
            "on and connected. Shelfrip tries again by itself.")


def plan_new_folders() -> int:
    with PLAN_LOCK:  # a rip finishing and the "Check for new rips" button can overlap
        return _plan_new_folders()


def _plan_new_folders() -> int:
    lib = library(refresh=True)
    n = 0
    if not RIP_ROOT.is_dir():
        return 0
    for folder in sorted(p for p in RIP_ROOT.iterdir() if p.is_dir() and not p.name.startswith("_")):
        # disc_info.json is written when a rip finishes, so folders still ripping are skipped
        if not (folder / "disc_info.json").exists():
            continue
        pj = folder / "plan.json"
        if pj.exists():
            # redo proposals from an older version of the namer, unless you've edited them
            try:
                old = json.loads(pj.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (old.get("status") != "proposed" or old.get("edited")
                    or old.get("planner_version", 1) >= identify.PLANNER_VERSION):
                continue
            shutil.copy2(pj, folder / "plan.old.json")
        try:
            if _is_music(folder):
                import music
                plan = music.plan_folder(folder, CFG, identify.PLANNER_VERSION)
            else:
                plan = identify.plan_folder(folder, RIP_ROOT, TMDB, lib, CFG)
        except Exception as exc:
            print(f"{folder.name}: could not plan ({exc})")
            continue
        if plan:
            plan["planned_at"] = time.time()
            save_plan(folder, plan)
            n += 1
    return n


def unnamed_folders() -> list[str]:
    """Finished rips that have no names yet (naming failed, e.g. no internet at the time)."""
    if not RIP_ROOT.is_dir():
        return []
    return [p.name for p in RIP_ROOT.iterdir() if p.is_dir() and not p.name.startswith("_")
            and (p / "disc_info.json").exists() and not (p / "plan.json").exists()]


def _is_music(folder: Path) -> bool:
    try:
        return json.loads((folder / "disc_info.json").read_text(encoding="utf-8")).get("kind") == "music"
    except Exception:
        return False


def list_plans() -> list[dict]:
    out = []
    for p in sorted(RIP_ROOT.glob("*/plan.json"), key=lambda p: p.parent.name):
        try:
            plan = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if plan.get("status") == "filed":
            continue
        plan["folder"] = p.parent.name
        job = JOBS.get(p.parent.name)
        if job:
            plan["job"] = job
        if plan.get("kind") == "tv":  # episodes already in the library, for "belongs to episode"
            plan["season_files"] = season_files(plan)
        if CFG.get("auto_file", True):
            plan["auto"] = {"blocked": confidence(plan),
                            "at": (plan.get("planned_at") or p.stat().st_mtime) + 60 * float(CFG.get("auto_file_minutes", 15))}
        out.append(plan)
    return out


def season_files(plan: dict) -> list[str]:
    """Names (without .mkv) of the episode files already in this season's library folder."""
    try:
        d = identify.lib_path(LIB_ROOT, f"{plan['library_dir']}\\Season {plan.get('season')}")
        return sorted(f.stem for f in d.glob("*.mkv") if re.search(r" - s\d+e\d+", f.stem, re.I)
                      and not re.search(r"-(?:" + "|".join(EXTRA_SUFFIX.values()) + r")\d*$", f.stem))
    except (OSError, KeyError):
        return []


# --------------------------------------------------------------------------- auto-filing

def confidence(plan: dict) -> list[str]:
    """Why this disc needs your review before filing. Empty = everything was matched with
    confidence, so it can be filed automatically."""
    why = []
    if plan.get("hold"):
        why.append("you chose to review it")
    if plan.get("edited"):
        why.append("you've edited it")
    why += [w for w in plan.get("warnings") or [] if "still have placeholder names" not in w]
    items = [it for it in plan.get("items", []) if it.get("action") == "move"]
    if not items:
        why.append("nothing to file")
    if plan.get("kind") == "music":
        if not plan.get("release"):
            why.append("not found on MusicBrainz")
        return list(dict.fromkeys(why))
    if not plan.get("tmdb_id"):
        why.append("no TMDB match")
    if plan.get("kind") == "tv" and (plan.get("match_source") or "TMDB search") == "TMDB search":
        why.append("first disc of this show: matched by name only")
    if plan.get("kind") == "movie":
        main = next((it for it in plan["items"] if it.get("role") == "main"), None)
        mins = (main or {}).get("duration_seconds", 0) / 60
        rivals = [c for c in plan.get("tmdb_candidates") or [] if str(c.get("id")) != str(plan.get("tmdb_id"))
                  and identify.norm(c.get("title") or "") == identify.norm(plan.get("title") or "")
                  and c.get("runtime") and abs(c["runtime"] - mins) <= 6]
        if rivals:
            why.append("another film with the same name and length is on TMDB (e.g. a remake)")
    for it in items:
        if it.get("needs_split"):
            why.append("has a video that needs cutting into episodes")
        if it.get("role") == "extra" and it.get("attach") != "episode" and str(it.get("name", "")).startswith("Extra "):
            why.append("some extras have no name yet")
        if str(it.get("note", "")).startswith("could be"):
            why.append("an extra could be one of several names")
    return list(dict.fromkeys(why))


def refresh_warnings(plan: dict) -> None:
    """Recount placeholder extras from the names as they are now (you may have renamed them)."""
    ws = [w for w in plan.get("warnings") or [] if "still have placeholder names" not in w]
    n = sum(1 for it in plan.get("items", []) if it.get("role") == "extra" and it.get("action") == "move"
            and it.get("attach") != "episode" and re.match(r"Extra \d+ \(", str(it.get("name", ""))))
    if n:
        ws.append(f"{n} extra(s) still have placeholder names.")
    plan["warnings"] = ws


def start_filing(folder: Path, plan: dict) -> list[str]:
    """Check and start filing a disc. Returns the problems if it can't be filed."""
    plan.pop("job", None)
    plan.pop("folder", None)
    plan.pop("auto", None)
    refresh_warnings(plan)
    save_plan(folder, plan)
    problems = check_plan(folder, plan)
    if problems:
        save_plan(folder, plan)
        return problems
    with LOCK:
        if JOBS.get(folder.name, {}).get("state") == "running":
            return ["Already filing this disc."]
        JOBS[folder.name] = {"state": "running", "done_bytes": 0, "total_bytes": 0, "current": "starting",
                             "label": plan.get("disc_label") or ""}
    f = plan.get("filing") or {}
    plan["filing"] = {"started": f.get("started") or time.strftime("%Y-%m-%dT%H:%M:%S"),
                      "tries": int(f.get("tries") or 0) + 1, "at": time.time()}
    save_plan(folder, plan)  # marks it as being filed, so an interrupted filing is finished next time
    threading.Thread(target=file_job, args=(folder, plan), daemon=True).start()
    return []


def filing_status(plan: dict) -> dict | None:
    """Is this disc being filed (or about to pick up a filing that was cut off)? For the drive cards
    on the home page, which hide "Rip again" and the like until it's done. None = not filing."""
    job = JOBS.get(plan.get("folder") or "") or {}
    if job.get("state") == "running":
        total = job.get("total_bytes") or 0
        return {"state": "running", "pct": min(100, round(100 * job.get("done_bytes", 0) / total)) if total else 0,
                "current": job.get("current") or "starting"}
    f = plan.get("filing")
    if not f:
        return None
    tries = int(f.get("tries") or 0)
    if tries >= 6 or (job.get("state") == "error" and tries >= 3):
        return None  # resume_tick won't try again: it's back on the Review tab for you
    return {"state": "waiting", "pct": 0, "current": job.get("current") if job.get("state") == "error" else ""}


def filed_label(label: str) -> bool:
    """Was the disc with this label filed in this session? (Its plan is gone from the rip folder.)"""
    return bool(label) and any(j.get("label") == label and j.get("state") == "done" for j in JOBS.values())


def resume_tick(log=print) -> list[str]:
    """Finish filings that were cut off: by Shelfrip closing (picked up straight away) or by an
    error such as the library dropping off the network (tried again every 5 minutes, 3 times)."""
    started = []
    if any(j.get("state") == "running" for j in JOBS.values()):
        return started
    for plan in list_plans():
        f, name = plan.get("filing"), plan["folder"]
        if not f:
            continue
        job = JOBS.get(name) or {}
        if job.get("state") == "running" or int(f.get("tries") or 0) >= 6:
            continue
        if job.get("state") == "error" and (int(f.get("tries") or 0) >= 3 or time.time() - float(f.get("at") or 0) < 300):
            continue
        if library_reachable():
            continue  # still away: try again later
        folder = folder_path(name)
        problems = start_filing(folder, plan)
        title = plan.get("album") if plan.get("kind") == "music" else plan.get("title")
        if problems:
            log(f"{title or name}: couldn't finish filing ({problems[0]})")
            plan = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
            plan.pop("filing", None)  # needs you now: it's back on the Review tab
            save_plan(folder, plan)
        else:
            log(f"{title or name}: finishing the filing that was interrupted")
            started.append(title or name)
        break  # one at a time
    return started


def refile_movie(name: str, tmdb_id: str, log=print) -> dict:
    """Move a filed movie to the right TMDB match: renames its library folder and main file(s),
    then updates its record, your disc database, the learned label and (if sharing is on) the
    shared disc database, so the corrected answer replaces the wrong one. Returns {ok} or {error}."""
    rec = (FILED_DIR / name).resolve()
    if rec.parent != FILED_DIR.resolve() or not (rec / "plan.json").exists():
        return {"error": "Couldn't find that disc's record."}
    plan = json.loads((rec / "plan.json").read_text(encoding="utf-8"))
    if plan.get("kind") != "movie":
        return {"error": "Only movies can be re-matched after filing, for now."}
    if not TMDB:
        return {"error": "Add your TMDB key in Settings first."}
    if not tmdb_id.isdigit():
        return {"error": "Pick a movie from the search results."}
    away = library_reachable()
    if away:
        return {"error": away}
    m = TMDB.movie(int(tmdb_id))
    title, year = m.get("title") or "", identify.year_of(m.get("release_date")) or ""
    old_rel = str(plan.get("library_dir") or "")
    old_base = old_rel.replace("/", "\\").rstrip("\\").split("\\")[-1]
    tag = naming.id_tag(CFG, tmdb_id)
    new_base = identify.fs_safe(f"{title} ({year})" if year else title) + (f" {tag}" if tag else "")
    if not old_base or not title:
        return {"error": "This disc's record doesn't say where it was filed."}
    if new_base == old_base:
        return {"error": "It's already filed as that movie."}
    new_rel = old_rel[: len(old_rel) - len(old_base)] + new_base

    def where(rel):
        return identify.lib_path(LIB_ROOT, rel)
    old_dir, new_dir = where(old_rel), where(new_rel)
    if new_dir.exists():
        return {"error": f"{new_dir} already exists. Is that movie already in your library?"}
    if not old_dir.is_dir():
        return {"error": f"Couldn't find {old_dir} in your library. Was it moved or renamed?"}
    os.rename(old_dir, new_dir)
    for f in list(new_dir.iterdir()):
        if f.is_file() and old_base in f.name:
            f.rename(new_dir / f.name.replace(old_base, new_base))

    def fix(v):
        if isinstance(v, list):
            return [fix(x) for x in v]
        return v.replace(old_base, new_base) if isinstance(v, str) else v
    was = {k: plan.get(k) for k in ("title", "year", "tmdb_id", "library_dir")}
    for it in plan.get("items", []):
        for k in ("dest_dir", "name", "filed_to"):
            if k in it:
                it[k] = fix(it[k])
    plan.update(title=title, year=year, tmdb_id=tmdb_id, library_dir=new_rel,
                refiled={"from": was, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    plan["warnings"] = [w for w in plan.get("warnings", []) if "wrong movie" not in w and "Runtime doesn't match" not in w]
    save_plan(rec, plan)
    _after_refile(rec, plan, name, log)
    log(f"Re-matched {was['title']} ({was['year']}) as {title} ({year})")
    return {"ok": True, "title": title, "year": year, "folder": str(new_dir)}


def _after_refile(rec: Path, plan: dict, name: str, log=print) -> None:
    """Pass a corrected match on to your disc database, learned labels, the shared database and Plex."""
    info = rec / "disc_info.json"
    disc = json.loads(info.read_text(encoding="utf-8")) if info.exists() else {}
    for step in ("catalog", "label", "share", "plex"):
        try:
            if step == "catalog":
                import catalog
                catalog.record(plan, disc)
            elif step == "label":
                import discdb
                discdb.remember(plan.get("disc_label") or name.split("__")[0], plan)
            elif step == "share":
                import community
                community.share(plan, disc, CFG, log=log)
            else:
                import plex
                threading.Thread(target=plex.scan_and_check, args=(CFG, plan, {}), daemon=True).start()
        except Exception as exc:
            log(f"re-matching {name}: couldn't update the {step} ({exc})")
    _forget_progress()
    library(refresh=True)


def refile_tv(name: str, tmdb_id: str, season, shift=0, log=print) -> dict:
    """Move a filed TV disc to the right show and/or season (and shift its episode numbers by
    `shift`): every file it put in the library is moved and renamed with TMDB's episode names,
    then the records are corrected as in refile_movie. Nothing moves unless every file can."""
    rec = (FILED_DIR / name).resolve()
    if rec.parent != FILED_DIR.resolve() or not (rec / "plan.json").exists():
        return {"error": "Couldn't find that disc's record."}
    plan = json.loads((rec / "plan.json").read_text(encoding="utf-8"))
    if plan.get("kind") != "tv":
        return {"error": "That disc isn't a TV disc."}
    if not TMDB:
        return {"error": "Add your TMDB key in Settings first."}
    try:
        season, shift = int(season), int(shift or 0)
    except (TypeError, ValueError):
        return {"error": "Pick a season."}
    if not str(tmdb_id).isdigit() or season < 0:
        return {"error": "Pick a show from the search results."}
    away = library_reachable()
    if away:
        return {"error": away}
    show = TMDB.tv(int(tmdb_id))
    title, year = show.get("name") or "", identify.year_of(show.get("first_air_date")) or ""
    names = {x["episode_number"]: x.get("name") or "" for x in TMDB.season(int(tmdb_id), season).get("episodes", [])}
    old_rel = str(plan.get("library_dir") or "").replace("/", "\\").rstrip("\\")
    if not old_rel or not title:
        return {"error": "This disc's record doesn't say where it was filed."}
    tag = naming.id_tag(CFG, tmdb_id)
    new_prefix = identify.fs_safe(f"{title} ({year})" if year else title)
    new_rel = old_rel.rsplit("\\", 1)[0] + "\\" + new_prefix + (f" {tag}" if tag else "") if "\\" in old_rel \
        else new_prefix + (f" {tag}" if tag else "")
    old_season = plan.get("season")
    if new_rel == old_rel and season == old_season and not shift:
        return {"error": "It's already filed as that show and season."}
    eps_items = [it for it in plan.get("items", []) if it.get("role") == "episode" and it.get("action") != "skip"]
    if len({it.get("season") for it in eps_items}) > 1:
        return {"error": "This disc has episodes from more than one season, so it can't be moved in one go."}
    if any(n + shift < 1 for it in eps_items for n in (it.get("episodes") or [it.get("episode") or 1])):
        return {"error": "That would number an episode below 1."}

    def where(rel):
        return identify.lib_path(LIB_ROOT, rel)
    old_show, new_show = where(old_rel), where(new_rel)
    sdir = "Specials" if season == 0 else f"Season {season}"

    def ep_name(nums):
        t = identify.fs_safe(names.get(nums[0], ""))
        return f"{new_prefix} - s{season:02d}e{nums[0]:02d}" + (f"-e{nums[-1]:02d}" if len(nums) > 1 else "") + (f" - {t}" if t else "")
    stems: dict[str, str] = {}  # old file name (no extension) -> new one
    changes = []  # (item, new fields)
    for it in eps_items:
        nums = [n + shift for n in (it.get("episodes") or [it["episode"]])]
        new = {"season": season, "episode": nums[0], "episodes": nums, "name": ep_name(nums)}
        stems[it.get("name") or ""] = new["name"]
        sp = it.get("split")
        if sp and sp.get("names"):
            new["split_names"] = [ep_name([n]) for n in nums][:len(sp["names"])]
            stems.update(zip(sp["names"], new["split_names"]))
        changes.append((it, new))
    old_prefix = identify.fs_safe(f"{plan.get('title')} ({plan.get('year')})" if plan.get("year") else plan.get("title") or "")

    def new_stem(stem):
        for o in sorted(stems, key=len, reverse=True):
            if o and (stem == o or stem.startswith(o + "-")):
                return stems[o] + stem[len(o):]
        return stem.replace(old_prefix, new_prefix, 1) if old_prefix else stem

    def new_path(old: Path) -> Path:
        try:
            parts = list(old.relative_to(old_show).parts)
        except ValueError:
            return old
        if parts and old_season is not None and parts[0] == ("Specials" if old_season == 0 else f"Season {old_season}"):
            parts[0] = sdir
        parts[-1] = new_stem(Path(parts[-1]).stem) + Path(parts[-1]).suffix
        return new_show.joinpath(*parts)
    moves = []
    for it in plan.get("items", []):
        ft = it.get("filed_to")
        for f in ([ft] if isinstance(ft, str) else ft or []):
            src = Path(f)
            moves.append((it, f, src, new_path(src)))
    missing = [str(m[2]) for m in moves if not m[2].exists()]
    if missing:
        return {"error": f"Couldn't find {missing[0]} in your library{f' (and {len(missing) - 1} more)' if len(missing) > 1 else ''}. Was it moved or renamed?"}
    taken = [str(m[3]) for m in moves if m[3] != m[2] and m[3].exists()]
    if taken:
        return {"error": f"{taken[0]} already exists. Is that episode already in your library?"}
    done = []
    try:
        for it, f, src, dst in moves:
            if dst != src:
                paths.make_folder(dst.parent, "the library folder")
                os.rename(src, dst)
                done.append((src, dst))
    except OSError as exc:
        for src, dst in reversed(done):  # put back what already moved
            try:
                os.rename(dst, src)
            except OSError:
                pass
        return {"error": f"Couldn't move the files ({exc}). Nothing was changed."}
    for d in sorted({m[2].parent for m in moves}, key=lambda p: len(p.parts), reverse=True):
        while d != old_show.parent and d.is_dir() and not any(d.iterdir()):  # tidy emptied folders
            d.rmdir()
            d = d.parent
    newf = {f: str(dst) for it, f, src, dst in moves}
    old_dest = old_rel + ("\\" + ("Specials" if old_season == 0 else f"Season {old_season}") if old_season is not None else "")
    for it in plan.get("items", []):
        if isinstance(it.get("filed_to"), str):
            it["filed_to"] = newf.get(it["filed_to"], it["filed_to"])
        elif it.get("filed_to"):
            it["filed_to"] = [newf.get(f, f) for f in it["filed_to"]]
        dd = str(it.get("dest_dir") or "")
        if dd.startswith(old_dest) and old_season is not None:
            it["dest_dir"] = new_rel + "\\" + sdir + dd[len(old_dest):]
        elif dd.startswith(old_rel):
            it["dest_dir"] = new_rel + dd[len(old_rel):]
        if it.get("attach_to"):
            it["attach_to"] = new_stem(it["attach_to"])
    for it, new in changes:
        if "split_names" in new:
            it["split"]["names"] = new.pop("split_names")
        it.update(new)
    was = {k: plan.get(k) for k in ("title", "year", "tmdb_id", "season", "library_dir")}
    plan.update(title=title, year=year, tmdb_id=str(tmdb_id), show_key=str(tmdb_id), season=season, library_dir=new_rel,
                refiled={"from": was, "shift": shift, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    save_plan(rec, plan)
    _after_refile(rec, plan, name, log)
    log(f"Re-matched {was['title']} season {was['season']} as {title} season {season}" + (f" (episodes moved by {shift:+d})" if shift else ""))
    return {"ok": True, "title": title, "year": year, "season": season, "folder": str(new_show / sdir)}


def auto_file_tick(log=print) -> list[str]:
    """File every disc that's confident and has waited long enough. Returns their names."""
    if not CFG.get("auto_file", True):
        return []
    started = []
    for plan in list_plans():
        a = plan.get("auto") or {}
        name = plan["folder"]
        if a.get("blocked") or time.time() < a.get("at", 0) or JOBS.get(name):
            continue
        if any(j.get("state") == "running" for j in JOBS.values()):
            break  # one at a time, so the library drive isn't hammered
        folder = folder_path(name)
        problems = start_filing(folder, plan)
        if problems:  # e.g. already in the library now: leave it for you
            plan["hold"] = True
            plan["auto_problem"] = problems[:3]
            plan.setdefault("warnings", []).append("Couldn't file automatically: " + " ".join(problems[:3]))
            save_plan(folder, plan)
            log(f"{plan.get('title') or name}: not filed automatically ({problems[0]})")
        else:
            JOBS[name]["auto"] = True
            log(f"{plan.get('title') or name}: filing automatically (everything matched)")
            started.append(plan.get("title") or name)
            break
    return started


RESERVED = {"_filed", "_contribute"}  # Shelfrip's own folders in the rip folder, never a disc


def folder_path(name: str) -> Path:
    p = (RIP_ROOT / name).resolve()
    if p.parent != RIP_ROOT.resolve() or p.name.lower() in RESERVED:
        raise ValueError("bad folder")
    return p


# Inline names for episode extras ("<episode file>-deleted.mkv"), per extras category
EXTRA_SUFFIX = naming.EXTRA_SUFFIX


def dest_for(item: dict, plan: dict | None = None) -> Path:
    """Where a file goes in the library. TV extras can belong to the whole show
    (Show\\Featurettes\\...), a season (Show\\Season 1\\Featurettes\\..., the default) or one episode
    (Show\\Season 1\\<episode file name>-featurette.mkv), as in Plex's guide to TV extras."""
    name = item["name"]
    parts = [item["dest_dir"]]
    if item.get("role") == "extra":
        cat = item.get("category") or "Other"
        attach = item.get("attach") or "season"
        folder = naming.extras_folder(CFG, cat)
        if plan and plan.get("kind") == "tv" and attach == "show" and plan.get("library_dir"):
            parts = [plan["library_dir"], folder]
        elif (plan and plan.get("kind") == "tv" and attach == "episode" and item.get("attach_to")
              and naming.episode_extras_inline(CFG)):
            same = [o for o in plan.get("items", []) if o.get("role") == "extra" and o.get("action") == "move"
                    and o.get("attach") == "episode" and o.get("attach_to") == item["attach_to"]
                    and (o.get("category") or "Other") == cat]
            idx = str(next((k for k, o in enumerate(same, 1) if o is item), 1)) if len(same) > 1 else ""
            name = f"{item['attach_to']}-{EXTRA_SUFFIX.get(cat, 'other')}{idx}"
        elif naming.extras_suffixed(CFG):  # "Making Of-featurette.mkv" next to the film or in the season folder
            name = f"{name}-{EXTRA_SUFFIX.get(cat, 'other')}"
        else:
            parts.append(folder)
    return identify.lib_path(LIB_ROOT, "\\".join(parts)) / (identify.fs_safe(name) + ".mkv")


def split_ok(it: dict) -> str:
    """'' if the item's split is usable, else what's wrong with it."""
    sp = it.get("split") or {}
    starts, names, n = sp.get("starts") or [], sp.get("names") or [], len(sp.get("chapters") or [])
    if not starts:
        return "holds several episodes but has no split. Set it to Skip, or change its Type"
    if len(starts) != len(names):
        return "has a different number of episode starts and names"
    if any(b <= a for a, b in zip(starts, starts[1:])) or starts[0] < 0 or starts[-1] >= n:
        return "has episode starts out of order"
    return ""


def pieces(it: dict) -> list[dict]:
    """The files an item becomes in the library: itself, or one per episode when it's split."""
    if not it.get("needs_split"):
        return [it]
    sp = it["split"]
    out = []
    for k, nm in enumerate(sp["names"]):
        if k in (sp.get("skip") or []):
            continue
        e = (it.get("episodes") or [])[k] if k < len(it.get("episodes") or []) else None
        out.append(dict(it, name=nm, episode=e, episodes=[e] if e else [], needs_split=False, split_index=k))
    return out


def item_done(it: dict) -> bool:
    """Already copied into the library by an earlier filing of this disc that didn't finish."""
    to = it.get("filed_to")
    files = to if isinstance(to, list) else [to] if to else []
    return bool(files) and all(Path(f).exists() for f in files)


def check_plan(folder: Path, plan: dict) -> list[str]:
    if plan.get("kind") == "music":
        import music
        return music.check_plan(folder, plan, CFG)
    problems, seen = [], set()
    away = library_reachable()
    if away:
        return [away]
    if plan.get("filing"):  # finishing an interrupted filing
        for it in plan["items"]:
            if (it.get("action") == "move" and not it.get("needs_split") and not item_done(it)
                    and not (folder / it["source"]).exists() and dest_for(it, plan).exists()):
                it["filed_to"] = str(dest_for(it, plan))  # moved just before Shelfrip closed, before it could note it
    # Has the library gained a copy since this disc was named? Skip those, same as the namer does.
    try:
        mine = identify.quality_rank(plan.get("format") or "", CFG)
        copies = identify.library_copies(plan, library(refresh=True), CFG)
        newly = 0
        for it in plan["items"]:
            have = copies.get(it["source"])
            if it.get("action") != "move" or not have or it.get("library_copy") or item_done(it):
                continue
            best = max(have, key=lambda h: h["rank"])
            it["library_copy"] = {"format": best["format"], "better": best["rank"] > mine}
            if mine and best["rank"] >= mine:
                it["action"] = "skip"
                it["note"] = "; ".join(x for x in (f"already in Plex as a {best['format']} copy, so skipped. "
                                       f"Set Keep to add it anyway", it.get("note")) if x)
                newly += 1
        if newly:
            problems.append(f"{newly} title(s) are already in Plex in the same or better quality, so they're now "
                            f"set to Skip. Check them, then file again.")
    except Exception as exc:
        print(f"couldn't check the library for copies: {exc}")
    try:  # room in the library for it?
        need = sum((folder / it["source"]).stat().st_size for it in plan["items"]
                   if it.get("action") == "move" and not item_done(it) and (folder / it["source"]).exists())
        where = identify.lib_path(LIB_ROOT, plan.get("library_dir") or "")  # Movies/TV can be on another drive
        while not where.exists() and where.parent != where:
            where = where.parent
        free = shutil.disk_usage(where).free
        if need + 2e9 > free:
            problems.append(f"Not enough space in {where}: needs {need / 1e9:.1f} GB, {free / 1e9:.1f} GB free.")
    except OSError:
        pass
    for it in plan["items"]:
        if it.get("action") != "move" or item_done(it):  # done = copied by a filing that was interrupted
            continue
        if it.get("needs_split"):
            bad = split_ok(it)
            if bad:
                problems.append(f"{it['source']} {bad}.")
                continue
            if not split.available(CFG):
                problems.append(f"{it['source']} needs MKVToolNix to split it. Install it, or set mkvtoolnix_dir in config.json.")
                continue
        if not (folder / it["source"]).exists():
            problems.append(f"{it['source']} is missing from the disc folder.")
        if it.get("role") == "extra" and it.get("attach") == "episode" and not it.get("attach_to"):
            problems.append(f"{it['source']} belongs to an episode: pick which one.")
            continue
        for pc in pieces(it):
            if not identify.fs_safe(pc.get("name", "")):
                problems.append(f"{it['source']} has no name" + (f" for episode {pc['split_index'] + 1}." if "split_index" in pc else "."))
                continue
            d = dest_for(pc, plan)
            if d in seen:
                problems.append(f"Two files would both be named {d.name}.")
            seen.add(d)
            if d.exists():
                problems.append(f"Already exists in the library: {d}")
    return problems


def prepare_contribution(folder: Path, plan: dict) -> str:
    """Write an upload-ready package for TheDiscDB (and a TMDB fix list). Nothing is sent anywhere."""
    notes = []
    fixes = [f"{plan.get('title')} ({plan.get('year')}) tmdb-{plan.get('tmdb_id')}  {it.get('name')}: {it['note']}"
             for it in plan["items"] if it.get("role") == "episode" and it.get("note") and
             any(k in it["note"] for k in ("TVmaze calls", "TMDB lists only", "TMDB has no"))]
    if fixes:
        CONTRIB_DIR.mkdir(exist_ok=True)
        with open(CONTRIB_DIR / "tmdb_fixes.txt", "a", encoding="utf-8") as fh:
            fh.write(f"# {time.strftime('%Y-%m-%d')}  {folder.name}\n" + "\n".join(fixes) + "\n\n")
        notes.append(f"{len(fixes)} TMDB episode question(s) added to _Contribute\\tmdb_fixes.txt")
    if plan.get("discdb_matched") or not CFG.get("prepare_contributions", True):
        return "; ".join(notes)

    out = CONTRIB_DIR / folder.name
    out.mkdir(parents=True, exist_ok=True)
    for f in ("disc.odm.json", "makemkv_info.txt", "rip.log"):
        if (folder / f).exists():
            shutil.copy2(folder / f, out / f)
    try:
        disc = json.loads((folder / "disc_info.json").read_text(encoding="utf-8"))
    except Exception:
        disc = {"titles": []}
    by_file = {t.get("output_file"): t for t in disc.get("titles", [])}
    type_of = {"main": "MainMovie", "edition": "MainMovie", "episode": "Episode", "play_all": "Extra"}
    cat_type = {"Deleted Scenes": "DeletedScene", "Trailers": "Trailer", "Featurettes": "Featurette",
                "Behind the Scenes": "Featurette", "Interviews": "Interview", "Shorts": "Short"}
    rows = []
    for it in plan["items"]:
        t = by_file.get(it["source"], {})
        kind = type_of.get(it.get("role"), cat_type.get(it.get("category"), "Extra"))
        label = it.get("name", "")
        if it.get("role") == "episode":
            label = (label.split(f"e{it.get('episode', 0):02d}", 1)[-1]).lstrip(" -") or label
        rows.append({
            "title_index": t.get("title_id"), "file": it["source"], "source_file": t.get("source_file"),
            "segments": t.get("segments"), "duration": it.get("duration"), "size_bytes": t.get("size_bytes"),
            "kept": it.get("action") == "move", "type": kind, "name": label,
            "season": it.get("season") if it.get("role") == "episode" else None,
            "episode": it.get("episode") if it.get("role") == "episode" else None,
        })
    info = {"title": plan.get("title"), "year": plan.get("year"), "kind": plan.get("kind"),
            "tmdb_id": plan.get("tmdb_id"), "format": plan.get("format"), "disc_label": plan.get("disc_label"),
            "content_hash": disc.get("content_hash"), "titles": rows}
    (out / "contribution.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    lines = [f"{plan.get('title')} ({plan.get('year')})  TMDB {plan.get('tmdb_id')}  [{plan.get('format')}]",
             f"Disc label: {plan.get('disc_label')}   Fingerprint: {disc.get('content_hash') or 'not recorded'}", "",
             "How to submit: sign in at https://thediscdb.com/contribute, upload disc.odm.json on the",
             "Disc Manifest tab (or follow the site's steps with makemkv_info.txt), then label each title",
             "as listed below. Titles marked 'not kept' were skipped on the review page.", ""]
    for r in rows:
        se = f"  S{r['season']:02d}E{r['episode']:02d}" if r["season"] is not None and r["episode"] is not None else ""
        lines.append(f"{str(r['title_index']):>3}  {r['file']:<40} {str(r['source_file'] or ''):<14} "
                     f"{r['duration'] or '':>8}  {r['type']:<12}{se}  {r['name']}" + ("" if r["kept"] else "   (not kept)"))
    if not (out / "disc.odm.json").exists():
        lines += ["", "No disc.odm.json: install TheDiscDB's scan tool (see README) and new rips will include one."]
    (out / "titles.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    notes.append("TheDiscDB upload package saved in _Contribute")
    return "; ".join(notes)


# --------------------------------------------------------------------------- filing

def same_volume(src: Path, dst_dir: Path) -> bool:
    try:
        return src.stat().st_dev == dst_dir.stat().st_dev
    except OSError:
        return False


def _forget_progress() -> None:
    try:
        import progress
        progress.forget_cache()
    except Exception:
        pass


def _length(path: Path, mkvmerge: str) -> float:
    import subprocess
    r = subprocess.run([mkvmerge, "-J", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return (json.loads(r.stdout or "{}").get("container", {}).get("properties", {}).get("duration") or 0) / 1e9


def verify_copy(src: Path, copy: Path) -> str:
    """'' if the copy plays as long as the original (MKVToolNix reads both), else what's wrong.
    The sizes already match; this catches a copy that was garbled on the way (a flaky network)."""
    if src.suffix.lower() != ".mkv":
        return ""
    try:
        mkvmerge = split.tool(CFG, "mkvmerge")
    except Exception:
        mkvmerge = None
    if not mkvmerge:
        return ""
    try:
        a, b = _length(src, mkvmerge), _length(copy, mkvmerge)
    except Exception as exc:
        return f"couldn't read it back: {exc}"
    if a and abs(a - b) > 1:
        return f"it plays {b:.0f} s instead of {a:.0f} s"
    return ""


def copy_with_progress(src: Path, dst: Path, job: dict) -> None:
    paths.make_folder(dst.parent, "the library folder")
    if same_volume(src, dst.parent):
        os.replace(src, dst)
        job["done_bytes"] += src.stat().st_size if src.exists() else dst.stat().st_size
        return
    tmp = dst.with_name(dst.name + ".partial")
    size = src.stat().st_size
    with open(src, "rb") as fi, open(tmp, "wb") as fo:
        while True:
            buf = fi.read(8 * 1024 * 1024)
            if not buf:
                break
            fo.write(buf)
            job["done_bytes"] += len(buf)
    if tmp.stat().st_size != size:
        tmp.unlink(missing_ok=True)
        raise IOError(f"size mismatch copying {src.name}")
    bad = verify_copy(src, tmp)
    if bad:  # keep the original: it's only deleted once the copy is known to be good
        tmp.unlink(missing_ok=True)
        raise IOError(f"the copy of {src.name} didn't check out ({bad}), so the original was kept")
    os.replace(tmp, dst)
    shutil.copystat(src, dst)
    src.unlink()


def eject_disc(disc_info: dict) -> str:
    """Eject the disc this rip came from, if it's still in its drive and not being ripped."""
    import rip_all
    dev, label = disc_info.get("drive") or "", disc_info.get("disc_label") or ""
    if not dev or not label:
        return ""
    try:
        cfg = dict(rip_all.DEFAULT_CONFIG, **CFG)
        drives = rip_all.scan_drives(cfg)
        d = next((d for d in drives if d["device"].upper() == dev.upper()), None)
        if d and d["loaded"] and d["disc_label"] == label and not rip_all.drive_locked(cfg, dev):
            rip_all.eject(dev)
            return f"ejected {dev}"
    except Exception as exc:
        print(f"couldn't eject {dev}: {exc}")
    return ""


def read_disc_info(folder: Path) -> dict:
    for p in (folder / "disc_info.json", FILED_DIR / folder.name / "disc_info.json"):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    return {}


def file_music_job(folder: Path, plan: dict) -> None:
    """A music CD: tag and copy each track into the music library, then clean up like a disc."""
    import music
    job = JOBS[folder.name]
    disc_info = read_disc_info(folder)
    try:
        filed = music.file_job(folder, plan, job, CFG)
        plan["status"] = "filed"
        plan["filed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        plan.pop("filing", None)
        save_plan(folder, plan)
        if all(Path(f).exists() for f in filed):
            record = FILED_DIR / folder.name
            record.mkdir(parents=True, exist_ok=True)
            for f in folder.iterdir():
                if f.is_file() and f.suffix.lower() in (".json", ".txt", ".log"):
                    shutil.move(str(f), str(record / f.name))
            for f in (folder / "rip").glob("*.log"):
                shutil.move(str(f), str(record / f.name))
            shutil.rmtree(folder, ignore_errors=True)
            job["note"] = "; ".join(x for x in ("Rip folder cleared", job.get("note")) if x)
        if CFG.get("eject_after_filing", True):
            ej = eject_disc(disc_info)
            if ej:
                job["note"] = "; ".join(x for x in (job.get("note"), f"Disc {ej[8:]} ejected") if x)
        job.update(state="done", current=f"Filed {len(filed)} tracks")
        _forget_progress()
        job["plex"] = {"state": "starting", "summary": "Asking Plex to scan…"}
        threading.Thread(target=music.plex_scan, args=(CFG, music.album_dir(plan, CFG), job["plex"]),
                         daemon=True).start()
    except Exception as exc:
        traceback.print_exc()
        save_plan(folder, plan)
        job.update(state="error", current=str(exc))
        _offer_report("Filing didn't finish", folder.name.rsplit("__", 1)[0], str(exc))


def _offer_report(what: str, label: str, error: str) -> None:
    try:
        import trouble
        trouble.record(what, label, error, with_trace=True)
    except Exception:
        pass


def file_job(folder: Path, plan: dict) -> None:
    if plan.get("kind") == "music":
        return file_music_job(folder, plan)
    name = folder.name
    job = JOBS[name]
    disc_info = read_disc_info(folder)
    moves = [it for it in plan["items"] if it.get("action") == "move"]
    try:
        job["total_bytes"] = sum((folder / it["source"]).stat().st_size for it in moves
                                 if not item_done(it) and (folder / it["source"]).exists())
        for i, it in enumerate(moves, 1):
            if item_done(it) and not (folder / it["source"]).exists():
                continue  # copied by the earlier, interrupted filing
            if it.get("needs_split"):
                # cut each episode out (no re-encoding) next to the rip, then move it into the library
                src, filed = folder / it["source"], []
                for pc in pieces(it):
                    k = pc["split_index"]
                    dst = dest_for(pc, plan)
                    job["current"] = f"{i}/{len(moves)}: cutting episode {k + 1} of {len(it['split']['names'])}: {dst.name}"
                    part = split.cut(src, it["split"], k, folder / "_split" / f"{k + 1:02d}.mkv", CFG,
                                     title=dst.stem)
                    why = "; ".join(x for x in (split.tag_audio(part, it.get("audio"), CFG),
                                                split.set_default_tracks(part, CFG)) if x)
                    if why:
                        job["note"] = "; ".join(x for x in (job.get("note"), f"{dst.stem}: {why}") if x)
                    job["current"] = f"{i}/{len(moves)}: {dst.name}"
                    copy_with_progress(part, dst, job)
                    filed.append(str(dst))
                it["filed_to"] = filed
                save_plan(folder, plan)  # progress, in case filing is interrupted
                shutil.rmtree(folder / "_split", ignore_errors=True)
                continue
            dst = dest_for(it, plan)
            if not (folder / it["source"]).exists() and dst.exists():
                it["filed_to"] = str(dst)  # moved just before Shelfrip closed, before it could note it
                save_plan(folder, plan)
                continue
            job["current"] = f"{i}/{len(moves)}: {dst.name}"
            why = "; ".join(x for x in (split.tag_audio(folder / it["source"], it.get("audio"), CFG),  # name commentary tracks
                                        split.set_default_tracks(folder / it["source"], CFG)) if x)
            if why:
                job["note"] = "; ".join(x for x in (job.get("note"), f"{dst.stem}: {why}") if x)
            copy_with_progress(folder / it["source"], dst, job)
            it["filed_to"] = str(dst)
            save_plan(folder, plan)  # progress, in case filing is interrupted
        plan["status"] = "filed"
        plan["filed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        plan.pop("filing", None)
        save_plan(folder, plan)
        # Every kept file is now in the library (copied and size-checked). Keep only the small
        # record files in _Filed (they help name later discs of the same show) and delete the
        # rest of the disc folder, including skipped clips, to free up space.
        try:  # your own disc database (catalog.py), recorded exactly as filed
            import catalog
            info = folder / "disc_info.json"
            disc_info = json.loads(info.read_text(encoding="utf-8")) if info.exists() else {}
            catalog.record(plan, disc_info)
        except Exception as exc:
            disc_info = {}
            print(f"couldn't add {name} to the disc catalog: {exc}")
        try:  # the shared Shelfrip disc database (only if sharing is on; runs in the background)
            import community
            community.share(plan, disc_info, CFG)
        except Exception as exc:
            print(f"couldn't share {name} with the Shelfrip disc database: {exc}")
        try:
            contrib_note = prepare_contribution(folder, plan)
        except Exception as exc:
            contrib_note = f"couldn't prepare the TheDiscDB package ({exc})"
        filed = [f for it in moves for f in (it["filed_to"] if isinstance(it["filed_to"], list) else [it["filed_to"]])]
        missing = [f for f in filed if not Path(f).exists()]
        if missing:
            job["note"] = "; ".join(x for x in (f"Filed, but {len(missing)} file(s) aren't showing in the library yet, so the rip folder was kept.", job.get("note")) if x)
        else:
            record = FILED_DIR / name
            try:
                record.mkdir(parents=True, exist_ok=True)
                for f in folder.iterdir():
                    if f.is_file() and f.suffix.lower() in (".json", ".txt", ".log"):
                        shutil.move(str(f), str(record / f.name))
                shutil.rmtree(folder)
                job["note"] = "; ".join(x for x in ("Rip folder cleared to save space", job.get("note")) if x)
            except Exception as exc:
                job["note"] = "; ".join(x for x in (f"Filed, but couldn't clear the rip folder ({exc}).", job.get("note")) if x)
        try:  # so later discs of the same set match themselves
            import discdb
            discdb.remember(plan.get("disc_label") or name.split("__")[0], plan)
        except Exception as exc:
            print(f"couldn't remember label for {name}: {exc}")
        if contrib_note:
            job["note"] = "; ".join(x for x in (job.get("note"), contrib_note) if x)
        if CFG.get("eject_after_filing", True):  # the disc stayed in the drive while you reviewed it
            ej = eject_disc(disc_info)
            if ej:
                job["note"] = "; ".join(x for x in (job.get("note"), f"Disc {ej[8:]} ejected") if x)
        job.update(state="done", current=f"Filed {len(filed)} files")
        _forget_progress()
        library(refresh=True)
        # ask Plex to scan the new files and check it matched them to the right TMDB entry
        import plex
        job["plex"] = {"state": "starting", "summary": "Asking Plex to scan…"}
        threading.Thread(target=plex.scan_and_check, args=(CFG, plan, job["plex"]), daemon=True).start()
    except Exception as exc:
        traceback.print_exc()
        save_plan(folder, plan)
        job.update(state="error", current=str(exc))
        _offer_report("Filing didn't finish", folder.name.rsplit("__", 1)[0], str(exc))


# --------------------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the console quiet
        pass

    def parse_request(self) -> bool:
        """Only answer requests addressed to this computer's own Shelfrip. Stops a web page from
        reaching it through a look-alike address that points at 127.0.0.1 (DNS rebinding)."""
        if not super().parse_request():
            return False
        if (self.headers.get("Host") or "").lower() not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            self.send_error(403, "Forbidden")
            return False
        return True

    def send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        try:
            if u.path == "/":
                page = HERE / "review.html"
                if not page.exists():  # inside Shelfrip.exe
                    page = Path(getattr(sys, "_MEIPASS", HERE)) / "review.html"
                return self.send(200, page.read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/discs":
                return self.send(200, {"discs": list_plans(), "library": str(LIB_ROOT), "ripped": str(RIP_ROOT), **naming.page_info(CFG),
                                       "tmdb": bool(TMDB), "jobs": JOBS})
            if u.path == "/api/poster":  # box art, so you can see the match is the right one
                import progress
                return self.send(200, progress.poster(TMDB, q.get("kind", ""), q.get("id", ""), q.get("season") or None))
            if u.path == "/api/showinfo":  # a show's seasons, for "Wrong show or season?"
                if not TMDB:
                    return self.send(400, {"error": "No TMDB key in config.json"})
                t = TMDB.tv(int(q.get("id", "0")))
                return self.send(200, {"seasons": [{"n": x.get("season_number"), "name": x.get("name") or "",
                                                    "episodes": x.get("episode_count") or 0} for x in t.get("seasons", [])]})
            if u.path == "/api/search":
                if not TMDB:
                    return self.send(400, {"error": "No TMDB key in config.json"})
                kind = "tv" if q.get("kind") == "tv" else "movie"
                res = TMDB.search(kind, q.get("q", ""))[:10]
                return self.send(200, {"results": [{
                    "id": r["id"], "title": r.get("title") or r.get("name"),
                    "year": identify.year_of(r.get("release_date") or r.get("first_air_date")),
                    "overview": (r.get("overview") or "")[:160]} for r in res]})
        except Exception as exc:
            return self.send(500, {"error": str(exc)})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.headers.get("Origin") not in (None, f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"):
            return self.send(403, {"error": "forbidden"})
        u = urllib.parse.urlparse(self.path)
        try:
            b = self.body()
            if u.path == "/api/scan":
                return self.send(200, {"planned": plan_new_folders()})
            if u.path == "/api/refile":  # a filed movie turned out to be the wrong match
                if b.get("kind") == "tv":
                    return self.send(200, refile_tv(str(b.get("folder") or ""), str(b.get("tmdb_id") or ""),
                                                    b.get("season"), b.get("shift") or 0))
                return self.send(200, refile_movie(str(b.get("folder") or ""), str(b.get("tmdb_id") or "")))
            folder = folder_path(b["folder"])
            if u.path == "/api/play":
                src = folder / Path(b["source"]).name
                if b["source"].startswith("rip/"):  # a music CD's track
                    src = folder / "rip" / Path(b["source"]).name
                if hasattr(os, "startfile"):
                    os.startfile(str(src))  # opens in your default video player
                return self.send(200, {"ok": True})
            if u.path == "/api/save":
                plan = b["plan"]
                plan.pop("job", None)
                plan.pop("folder", None)
                plan["edited"] = True
                refresh_warnings(plan)
                save_plan(folder, plan)
                return self.send(200, {"ok": True})
            if u.path == "/api/discard":  # delete a rip you don't want (e.g. already in Plex)
                if JOBS.get(folder.name, {}).get("state") == "running":
                    return self.send(200, {"error": "This disc is being filed."})
                info = read_disc_info(folder)
                shutil.rmtree(folder)
                note = eject_disc(info) if CFG.get("eject_after_filing", True) else ""
                JOBS[folder.name] = {"state": "done", "current": "Discarded",
                                     "note": "Rip deleted" + (f"; disc {note[8:]} ejected" if note else "")}
                return self.send(200, {"ok": True})
            if u.path == "/api/rematch":
                try:  # keep what the shared database said, so filing can report it was wrong
                    was = json.loads((folder / "plan.json").read_text(encoding="utf-8")).get("community_answer")
                except (OSError, ValueError):
                    was = None
                plan = identify.plan_folder(folder, RIP_ROOT, TMDB, library(), CFG, override={
                    "kind": b.get("kind"), "tmdb_id": b.get("tmdb_id"), "season": b.get("season")})
                if was and not plan.get("community_answer"):
                    plan["community_answer"] = was
                save_plan(folder, plan)
                plan["folder"] = folder.name
                return self.send(200, {"plan": plan})
            if u.path == "/api/release":  # music CD: the user picked another MusicBrainz release
                import music
                plan = b["plan"]
                plan.pop("job", None)
                plan.pop("folder", None)
                music.change_release(folder, plan, b["release"])
                save_plan(folder, plan)
                plan["folder"] = folder.name
                return self.send(200, {"plan": plan})
            if u.path == "/api/edition":  # the user picked which release (edition) the disc is
                import extras
                plan = b["plan"]
                plan.pop("job", None)
                plan.pop("folder", None)
                extras.rename_from_release(plan, b["edition"])
                for it in plan["items"]:
                    if it.get("role") == "extra" and it.get("name"):
                        it["name"] = identify.capital_case(it["name"])
                try:
                    identify.attach_audio(plan, folder, read_disc_info(folder))
                except Exception as exc:
                    print(f"    (couldn't re-read the audio tracks: {exc})")
                save_plan(folder, plan)
                plan["folder"] = folder.name
                return self.send(200, {"plan": plan})
            if u.path == "/api/epnames":  # TMDB names for renumbered episodes
                season = int(b.get("season") or 0)
                eps = [int(e) for e in b.get("episodes") or []]
                names = identify.split_names(b.get("plan") or {}, season, eps, TMDB)
                return self.send(200, {"names": dict(zip(map(str, eps), names))})
            if u.path == "/api/resplit":  # re-guess the cuts for a different number of episodes
                it, plan = b["item"], b["plan"]
                count = max(2, int(b.get("count") or 2))
                chapters, total = split.read_chapters(folder / Path(it["source"]).name, CFG)
                if len(chapters) < count:
                    return self.send(200, {"error": f"This video only has {len(chapters)} chapters."})
                sp = split.propose(chapters, total or float(it.get("duration_seconds") or 0), count)
                first = int(it.get("episode") or 1)
                eps = list(range(first, first + count))
                sp["names"] = identify.split_names(plan, int(it.get("season") or 0), eps, TMDB)
                return self.send(200, {"split": sp, "episodes": eps})
            if u.path == "/api/file":
                plan = b["plan"]
                plan["edited"] = True
                problems = start_filing(folder, plan)
                if problems:
                    plan["folder"] = folder.name
                    return self.send(200, {"problems": problems, "plan": plan})
                return self.send(200, {"ok": True})
            if u.path == "/api/hold":  # you started reviewing: don't file it automatically
                pj = folder / "plan.json"
                plan = json.loads(pj.read_text(encoding="utf-8"))
                plan["hold"] = bool(b.get("hold", True))
                save_plan(folder, plan)
                return self.send(200, {"ok": True})
        except Exception as exc:
            traceback.print_exc()
            return self.send(500, {"error": str(exc)})
        self.send(404, {"error": "not found"})


def main() -> None:
    if not RIP_ROOT.exists():
        sys.exit(f"{RIP_ROOT} doesn't exist")
    print("Naming new rips...")
    n = plan_new_folders()
    print(f"  {n} new disc(s) named.")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}/"
    print(f"Review page: {url}  (keep this window open; close it when you're done)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
