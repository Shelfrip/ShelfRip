r"""
progress.py - the fun part: XP, collector rank, streak, daily goals, badges and your shelf.

Everything is worked out from the discs you've filed (the plan.json files kept in
<rip folder>\_Filed), so nothing extra is stored and nothing can drift out of step:
re-filing, discarding or deleting a rip changes the numbers the same way it changes the
library. The only extra file is shared.json (in your data folder), which remembers the
discs you were the first to add to the shared Shelfrip disc database.

XP for each filed disc:
  50  for filing it
  10  per movie, episode or edition kept
   5  per extra kept, +10 more when it has a real name (not "Extra 3 (0:42)")
  30  bonus for a 4K disc
   2  per track of a music CD
  plus the daily-goal bonuses below, for every day you met them.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
from pathlib import Path

import paths

PLACEHOLDER = re.compile(r"^(Extra \d+ \(|Unused clip|Probably )", re.I)  # see identify.PLACEHOLDER

# (level reached, rank name)
RANKS = [(1, "Rookie"), (3, "Collector"), (6, "Archivist"), (10, "Curator"), (15, "Librarian"),
         (21, "Vault Keeper"), (30, "Legend")]

# Daily goals: (key, label, target, bonus XP)
GOALS = [("discs", "File 3 discs", 3, 75),
         ("extras", "Keep 5 extras", 5, 50),
         ("minutes", "Add 2 hours to your shelf", 120, 50)]

SHARED_PATH = paths.DATA_DIR / "shared.json"
_LOCK = threading.Lock()
_CACHE: dict = {"t": 0.0, "key": None, "data": None}


def level_for(xp: int) -> tuple[int, int, int]:
    """(level, XP where this level started, XP where the next one starts). Level n starts at 50*n*(n-1)."""
    n = 1
    while 50 * (n + 1) * n <= xp:
        n += 1
    return n, 50 * n * (n - 1), 50 * (n + 1) * n


def rank_for(level: int) -> str:
    return [name for lv, name in RANKS if level >= lv][-1]


def disc_xp(plan: dict) -> dict:
    """XP for one filed disc, and what it adds to the counters."""
    kept = [it for it in plan.get("items", []) if it.get("action") == "move"]
    out = {"xp": 50, "extras": 0, "named": 0, "episodes": 0, "minutes": 0.0, "tracks": 0}
    if plan.get("kind") == "music":
        out["tracks"] = len(kept)
        out["xp"] += 2 * len(kept)
    for it in kept:
        role = it.get("role")
        if plan.get("kind") != "music":
            if role == "extra":
                out["extras"] += 1
                out["xp"] += 5
                if it.get("name") and not PLACEHOLDER.match(str(it.get("name"))):
                    out["named"] += 1
                    out["xp"] += 10
            elif role in ("main", "edition", "episode"):
                out["xp"] += 10
                if role == "episode":
                    out["episodes"] += max(1, len(it.get("episodes") or []) or 1)
        out["minutes"] += float(it.get("duration_seconds") or 0) / 60
    if "4k" in str(plan.get("format", "")).lower():
        out["xp"] += 30
    return out


def _shared() -> dict:
    try:
        return json.loads(SHARED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def note_shared(disc_key: str, first: bool) -> None:
    """community.share calls this after a disc reached the database."""
    with _LOCK:
        data = _shared()
        prev = data.get(disc_key) or {}
        data[disc_key] = {"first": bool(prev.get("first") or first),
                          "at": prev.get("at") or dt.datetime.now().isoformat(timespec="seconds")}
        try:
            SHARED_PATH.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError:
            pass


def _filed_plans(rip_root: Path) -> list[dict]:
    out = []
    for pj in (rip_root / "_Filed").glob("*/plan.json"):
        try:
            plan = json.loads(pj.read_text(encoding="utf-8"))
        except Exception:
            continue
        if plan.get("status") == "filed" and plan.get("filed_at"):
            plan["_folder"] = pj.parent.name
            out.append(plan)
    out.sort(key=lambda p: str(p.get("filed_at")))
    return out


def _badges(c: dict, streak_best: int, plans: list[dict], per_day: dict) -> list[dict]:
    fmt = [str(p.get("format", "")).lower() for p in plans]
    seasons: dict[tuple, set] = {}
    for p in plans:
        if p.get("kind") != "tv":
            continue
        for it in p.get("items", []):
            if it.get("action") == "move" and it.get("role") == "episode" and it.get("season"):
                key = (str(p.get("tmdb_id") or p.get("title")), int(it["season"]))
                seasons.setdefault(key, set()).update(int(e) for e in (it.get("episodes") or [it.get("episode")]) if e)
    run = max((len(eps) for eps in seasons.values() if eps and set(range(1, max(eps) + 1)) <= eps), default=0)
    best_day = max(per_day.values(), default=0)
    B = [
        ("first", "First Disc", "File your first disc", c["discs"], 1),
        ("ten", "Ten Down", "File 10 discs", c["discs"], 10),
        ("fifty", "Half Century", "File 50 discs", c["discs"], 50),
        ("hundred", "Centurion", "File 100 discs", c["discs"], 100),
        ("hattrick", "Hat Trick", "File 3 discs in one day", best_day, 3),
        ("extras", "Extras Hunter", "Keep 25 extras", c["extras"], 25),
        ("namer", "Name Dropper", "File 50 extras with real names", c["named"], 50),
        ("binge", "Binge Builder", "File 50 episodes", c["episodes"], 50),
        ("season", "Season Run", "File episodes 1 to 10 of a season", run, 10),
        ("bluray", "Blu-ray Buff", "File 10 Blu-rays", sum("blu" in f and "4k" not in f for f in fmt), 10),
        ("4k", "4K Club", "File a 4K disc", sum("4k" in f for f in fmt), 1),
        ("marathon", "Marathon", "100 hours on your shelf", int(c["minutes"] / 60), 100),
        ("music", "Mixtape", "File a music CD", c["music"], 1),
        ("streak3", "On a Roll", "A 3-day streak", streak_best, 3),
        ("streak7", "Week Warrior", "A 7-day streak", streak_best, 7),
        ("pioneer", "Pioneer", "Be the first to add a disc to the shared database", c["pioneer"], 1),
        ("complete", "Completionist", "Own every episode of a season", _complete_seasons(), 1),
    ]
    return [{"id": k, "name": n, "how": h, "have": min(v, t), "need": t, "earned": v >= t} for k, n, h, v, t in B]


def compute(rip_root: Path, goal_total: int = 0) -> dict:
    """Everything the page shows about your progress. Cached for 20 seconds."""
    def stamp(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    # a newly filed disc adds a folder to _Filed, which changes its time stamp
    key = (str(rip_root), goal_total, stamp(rip_root / "_Filed"), stamp(SHARED_PATH))
    if _CACHE["data"] and _CACHE["key"] == key and time.time() - _CACHE["t"] < 20:
        return _CACHE["data"]
    plans = _filed_plans(rip_root)
    shared = _shared()
    c = {"discs": 0, "movies": 0, "tv": 0, "music": 0, "extras": 0, "named": 0, "episodes": 0, "minutes": 0.0,
         "tracks": 0, "shared": len(shared), "pioneer": sum(1 for v in shared.values() if v.get("first"))}
    xp = 0
    days: dict[str, dict] = {}
    spines = []
    for p in plans:
        d = disc_xp(p)
        xp += d["xp"]
        c["discs"] += 1
        c[{"movie": "movies", "tv": "tv", "music": "music"}.get(p.get("kind"), "movies")] += 1
        for k in ("extras", "named", "episodes", "minutes", "tracks"):
            c[k] += d[k]
        day = str(p["filed_at"])[:10]
        dd = days.setdefault(day, {"discs": 0, "extras": 0, "minutes": 0.0, "xp": 0})
        dd["discs"] += 1
        dd["xp"] += d["xp"]
        dd["extras"] += d["extras"]
        dd["minutes"] += d["minutes"]
        title = p.get("album") if p.get("kind") == "music" else p.get("title")
        sub = (f"Season {p['season']}" if p.get("kind") == "tv" and p.get("season") else
               (p.get("album_artist") or "") if p.get("kind") == "music" else str(p.get("year") or ""))
        eps = sorted({int(e) for it in p.get("items", []) if it.get("action") == "move" and it.get("role") == "episode"
                      for e in (it.get("episodes") or [it.get("episode")]) if e})
        spines.append({"title": title or p.get("disc_label") or "Disc", "sub": sub, "kind": p.get("kind"),
                       "mbid": (p.get("release") or "") if p.get("kind") == "music" else "",
                       "episodes": [eps[0], eps[-1]] if eps else [], "extras": d["extras"], "label": p.get("disc_label") or "",
                       "folder": p.get("_folder") or "",
                       "format": p.get("format") or "", "tmdb_id": str(p.get("tmdb_id") or ""),
                       "season": p.get("season"), "filed_at": p["filed_at"], "xp": d["xp"],
                       "minutes": round(d["minutes"])})
    # daily-goal bonuses, for every day they were met
    for day, dd in days.items():
        for g, _, target, bonus in GOALS:
            if dd[g] >= target:
                xp += bonus
                dd["xp"] += bonus
    # streak: days in a row with at least one disc filed, ending today (or yesterday: still alive)
    today = dt.date.today()
    have = {dt.date.fromisoformat(d) for d in days if re.fullmatch(r"\d{4}-\d\d-\d\d", d)}
    streak, day = 0, today if today in have else today - dt.timedelta(days=1)
    while day in have:
        streak += 1
        day -= dt.timedelta(days=1)
    best, cur, prev = 0, 0, None
    for d in sorted(have):
        cur = cur + 1 if prev and (d - prev).days == 1 else 1
        best, prev = max(best, cur), d
    t = days.get(today.isoformat(), {"discs": 0, "extras": 0, "minutes": 0.0, "xp": 0})

    def week(start: dt.date) -> dict:
        span = [(start + dt.timedelta(days=i)).isoformat() for i in range(7)]
        w = [days[x] for x in span if x in days]
        return {"start": start.isoformat(), "discs": sum(x["discs"] for x in w), "xp": sum(x["xp"] for x in w),
                "hours": round(sum(x["minutes"] for x in w) / 60, 1), "extras": sum(x["extras"] for x in w),
                "days": sum(1 for x in w if x["discs"]),
                "titles": [s["title"] for s in spines if str(s["filed_at"])[:10] in span][-8:]}
    monday = today - dt.timedelta(days=today.weekday())
    recap = {"this": week(monday), "last": week(monday - dt.timedelta(days=7))}
    goals = [{"key": g, "label": label, "have": round(min(t[g], target)), "need": target, "bonus": bonus,
              "done": t[g] >= target} for g, label, target, bonus in GOALS]
    level, lo, hi = level_for(xp)
    per_day = {d: v["discs"] for d, v in days.items()}
    badges = _badges(c, best, plans, per_day)
    ranks = [{"level": lv, "name": n, "xp": 50 * lv * (lv - 1)} for lv, n in RANKS]
    data = {
        "xp": xp, "level": level, "rank": rank_for(level), "level_xp": lo, "next_xp": hi,
        "next_rank": next(({"name": n, "level": lv} for lv, n in RANKS if lv > level), None),
        "streak": streak, "streak_today": today in have, "best_streak": best,
        "goals": goals, "counts": {**c, "hours": round(c["minutes"] / 60, 1)},
        "total": goal_total, "badges": badges, "ranks": ranks, "spines": spines[-120:],
        "today": t["discs"], "week": recap,
    }
    _CACHE.update(t=time.time(), key=key, data=data)
    return data


def forget_cache() -> None:
    _CACHE["data"] = None


# --------------------------------------------------------------------------- box art

_POSTERS: dict = {}
POSTER_CACHE = paths.CACHE_DIR / "posters.json"
IMG = "https://image.tmdb.org/t/p/w342"


def poster(tmdb, kind: str, tmdb_id: str, season=None) -> dict:
    """TMDB box art (a season's own poster for TV when there is one), plus the title, year and
    blurb, so you can see at a glance whether the match is right. {} if unknown."""
    if not tmdb or not str(tmdb_id).isdigit() or kind not in ("movie", "tv"):
        return {}
    key = f"{kind}:{tmdb_id}:{season or ''}"
    if not _POSTERS:
        try:
            _POSTERS.update(json.loads(POSTER_CACHE.read_text(encoding="utf-8")))
        except Exception:
            pass
    hit = _POSTERS.get(key)
    if hit and time.time() - hit.get("t", 0) < 30 * 86400:
        return hit
    try:
        info = tmdb.movie(int(tmdb_id)) if kind == "movie" else tmdb.tv(int(tmdb_id))
    except Exception:
        return hit or {}
    path = info.get("poster_path") or ""
    overview = info.get("overview") or ""
    if kind == "tv" and season not in (None, ""):
        try:
            s = tmdb.season(int(tmdb_id), int(season))
            path = s.get("poster_path") or path
            overview = s.get("overview") or overview
        except Exception:
            pass
    date = info.get("release_date") or info.get("first_air_date") or ""
    out = {"url": IMG + path if path else "", "title": info.get("title") or info.get("name") or "",
           "year": date[:4], "overview": overview[:400], "t": time.time()}
    _POSTERS[key] = out
    try:
        POSTER_CACHE.parent.mkdir(parents=True, exist_ok=True)
        POSTER_CACHE.write_text(json.dumps(_POSTERS), encoding="utf-8")
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------- seasons

SEASON_CACHE = paths.CACHE_DIR / "seasons.json"
_SEASONS: dict = {}


def _season_cache() -> dict:
    if not _SEASONS:
        try:
            _SEASONS.update(json.loads(SEASON_CACHE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return _SEASONS


def _complete_seasons() -> int:
    return int(_season_cache().get("_complete") or 0)


def seasons(tmdb, rip_root: Path, lib_root: Path) -> dict:
    """Each TV season you've filed discs of: how many of its episodes you have (in your library,
    however they got there) out of how many TMDB lists. Newest first."""
    import identify
    import review
    cache = _season_cache()
    try:
        lib = review.library()
    except Exception:
        lib = {"movies": [], "shows": []}
    seen: dict[tuple, dict] = {}
    for p in reversed(_filed_plans(rip_root)):
        if p.get("kind") != "tv" or not p.get("tmdb_id"):
            continue
        for it in p.get("items", []):
            if it.get("action") != "move" or it.get("role") != "episode" or not it.get("season"):
                continue
            key = (str(p["tmdb_id"]), int(it["season"]))
            row = seen.setdefault(key, {"show": p.get("title") or "", "tmdb_id": key[0], "season": key[1],
                                        "filed": set(), "last": p["filed_at"]})
            row["filed"].update(int(e) for e in (it.get("episodes") or [it.get("episode")]) if e)
    out = []
    for (tid, sn), row in seen.items():
        ck = f"{tid}:{sn}"
        hit = cache.get(ck)
        if (not hit or time.time() - hit.get("t", 0) > 7 * 86400) and tmdb:
            try:
                eps = tmdb.season(int(tid), sn).get("episodes") or []
                today = dt.date.today().isoformat()
                aired = [e for e in eps if not e.get("air_date") or e["air_date"] <= today]
                hit = {"total": len(aired), "t": time.time()}
                cache[ck] = hit
            except Exception:
                pass
        total = (hit or {}).get("total") or 0
        have = set(row["filed"])
        try:
            have |= identify.episodes_in_library(lib, tid, identify.norm(row["show"]), sn)
        except Exception:
            pass
        n = len({e for e in have if not total or e <= total}) if total else len(have)
        out.append({"show": row["show"], "tmdb_id": tid, "season": sn, "have": n, "total": total,
                    "done": bool(total) and n >= total, "last": row["last"]})
    out.sort(key=lambda r: (r["done"], r["last"]), reverse=True)
    out.sort(key=lambda r: r["done"])  # still to finish first, then complete ones
    cache["_complete"] = sum(1 for r in out if r["done"])
    try:
        SEASON_CACHE.parent.mkdir(parents=True, exist_ok=True)
        SEASON_CACHE.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass
    return {"seasons": out}
