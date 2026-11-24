"""
extras.py - name a disc's extras automatically (used by identify.py).

Two sources, tried in order:
  1. TheDiscDB (thediscdb.com, free GraphQL API, no key). It records which playlist
     on a specific disc is which extra, so a match there is exact.
  2. DVDCompare (dvdcompare.net). It lists every release's extras with run times;
     we match each ripped title by length (within a couple of seconds).

Anything that can't be matched with confidence keeps its "Extra NN" placeholder
for the review page. Only the Python standard library is used. Lookups are cached
in .cache/ in the data folder (paths.py), so a disc is only fetched once.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
import urllib.parse
import urllib.request
import sys
from pathlib import Path

# next to the script, or next to Shelfrip.exe when built with PyInstaller
HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
import paths
CACHE = paths.CACHE_DIR
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " + paths.USER_AGENT

DVDCOMPARE = "https://www.dvdcompare.net/comparisons"
THEDISCDB = "https://thediscdb.com/graphql"

TOLERANCE = 2          # seconds either way for a run-time match
MIN_UNIQUE_SECONDS = 90  # shorter clips only count if exactly one feature has that length


# --------------------------------------------------------------------------- http

def _cached(key: str, fetch, max_age_days: int = 30):
    CACHE.mkdir(exist_ok=True)
    path = CACHE / (hashlib.sha1(key.encode()).hexdigest() + ".json")
    if path.exists() and time.time() - path.stat().st_mtime < max_age_days * 86400:
        return json.loads(path.read_text(encoding="utf-8"))
    data = fetch()
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def _http(url: str, data: dict | None = None, json_body: dict | None = None,
          referer: str | None = None) -> str:
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"
    elif data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        m = re.search(rb'charset=["\']?([\w-]+)', raw[:3000])
        enc = (m.group(1).decode() if m else None) or r.headers.get_content_charset() or "utf-8"
        return raw.decode(enc, errors="replace")


# --------------------------------------------------------------------------- categories

CATEGORY_RULES = [
    (r"deleted|extended scene|alternate (scene|ending|opening)|cut scene", "Deleted Scenes"),
    (r"trailer|teaser|tv spot|promo", "Trailers"),
    (r"behind[- ]the[- ]scenes|making[- ]of|the making|on set|production diar", "Behind the Scenes"),
    (r"interview|conversation with|q ?& ?a|in conversation", "Interviews"),
    (r"\bshort\b|cartoon", "Shorts"),
    (r"\bscene\b|sequence|clip", "Scenes"),
    (r"blooper|gag reel|outtake|music video|sing[- ]along|easter egg|gallery|game", "Other"),
    (r"featurette|documentary|retrospective|look at|inside|story|history|legacy|tribute|memoir", "Featurettes"),
]


def guess_category(title: str, ftype: str = "") -> str:
    text = f"{title} {ftype}".lower()
    for pattern, cat in CATEGORY_RULES:
        if re.search(pattern, text):
            return cat
    return "Featurettes"


THEDISCDB_TYPES = {
    "extra": None, "deletedscene": "Deleted Scenes", "deleted scenes": "Deleted Scenes",
    "trailer": "Trailers", "behindthescenes": "Behind the Scenes", "featurette": "Featurettes",
    "interview": "Interviews", "short": "Shorts", "scene": "Scenes",
}


# --------------------------------------------------------------------------- DVDCompare

def _strip_tags(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


def _runtime(s: str) -> int:
    s = s.strip()
    m = re.fullmatch(r"(\d+)\s*mins?", s)
    if m:
        return int(m.group(1)) * 60
    parts = [int(p) for p in s.split(":")]
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def dvdcompare_search(title: str) -> list[dict]:
    def fetch():
        page = _http(f"{DVDCOMPARE}/search.php", data={"param": title, "searchtype": "text"},
                     referer=f"{DVDCOMPARE}/")
        out, seen = [], set()
        for m in re.finditer(r'<a[^>]+href="[^"]*film\.php\?fid=(\d+)[^"]*"[^>]*>(.*?)</a>', page, re.I | re.S):
            fid, text = int(m.group(1)), _strip_tags(m.group(2)).strip()
            if text and fid not in seen:
                seen.add(fid)
                out.append({"fid": fid, "text": text})
        if not out:  # a single hit redirects with JavaScript
            m = re.search(r'location\.href\s*=\s*"film\.php\?fid=(\d+)"', page)
            if m:
                out.append({"fid": int(m.group(1)), "text": title})
        for r in out:
            ym = re.search(r"\((\d{4})\)\s*$", r["text"])
            r["year"] = int(ym.group(1)) if ym else None
            fm = re.search(r"\((Blu-ray 4K|Blu-ray|DVD|HD DVD)\)", r["text"], re.I)
            r["format"] = fm.group(1) if fm else ""
            r["title"] = re.sub(r"\s*\([^)]*\)", "", r["text"]).strip()
        return out
    return _cached(f"dvdc-search:{title.lower()}", fetch)


def _parse_features(extras_html: str) -> list[dict]:
    """Flatten an Extras block into features.

    Each feature: {disc, title, type, seconds, play_all, group, parents}. Lines nest with
    leading dashes ("-", "--", "---"); `parents` is the chain of headings above a line,
    so "Deleted Scenes" or "Bonus trailers" can decide the category.
    """
    text = _strip_tags(extras_html)
    words = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5, "SIX": 6,
             "SEVEN": 7, "EIGHT": 8, "NINE": 9, "TEN": 10}
    feats, disc, stack = [], 1, []  # stack[i] = heading text at depth i
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        dm = re.match(r"^DISCS?\s+([A-Z0-9-]+)\b", line, re.I)
        if dm:
            w = dm.group(1).upper()
            disc = words.get(w, int(w) if w.isdigit() else disc + 1)
            stack = []
            continue
        if line.startswith("*"):  # "* The Film"
            continue
        dashes = re.match(r"^(-{1,4})\s*", line)
        depth = len(dashes.group(1)) if dashes else 0
        line = line[dashes.end():] if dashes else line
        line = re.sub(r"\s*\[[^\]]*\]", "", line)  # technical notes
        rm = re.search(r"\((?:(?:with )?(Play All)(?: option)? - )?(\d{1,3}:\d{2}(?::\d{2})?|\d+\s*mins?)(\s+in total)?\)", line)
        seconds, play_all = None, False
        if rm:
            seconds = _runtime(rm.group(2))
            play_all = bool(rm.group(1) or rm.group(3))
            line = (line[:rm.start()] + line[rm.end():]).strip()
        heading = line.rstrip(":").strip()
        quoted = re.findall(r'["\u201c]([^"\u201d]+)["\u201d]', heading)
        if len(quoted) > 1:
            title, ftype = ", ".join(q.strip() for q in quoted), ""
        elif quoted:
            title = quoted[0].strip()
            ftype = re.sub(r'["\u201c][^"\u201d]+["\u201d]', "", heading).strip(" ,-")
            ftype = re.sub(r"^\d{4}\s*", "", ftype)
        else:
            title, ftype = heading, ""
        title = re.sub(r"\s*\((?:with|includes|introduced by|in )[^)]*\)", "", title)
        title = re.sub(r"\s+", " ", title).strip()
        parents = [p for p in stack[:depth] if p]
        feats.append({"disc": disc, "title": title, "type": ftype, "seconds": seconds,
                      "play_all": play_all, "group": parents[-1] if parents else None,
                      "parents": parents})
        stack = stack[:depth] + [heading]
    return feats


def dvdcompare_film(fid: int) -> list[dict]:
    """[{name, year, features:[...]}] for each release on the film page."""
    def fetch():
        page = _http(f"{DVDCOMPARE}/film.php?fid={fid}", referer=f"{DVDCOMPARE}/search.php")
        releases = []
        for ul in re.finditer(r'<ul[^>]*class="dvd"[^>]*>(.*?)</ul>', page, re.I | re.S):
            block = ul.group(1)
            h3 = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.I | re.S)
            name = re.sub(r"\s+", " ", _strip_tags(h3.group(1))).strip() if h3 else ""
            ym = re.search(r"\[(\d{4})", name)
            em = re.search(r'<div[^>]*class="label"[^>]*>\s*Extras:\s*</div>\s*<div[^>]*class="description"[^>]*>(.*?)</div>\s*</li>',
                           block, re.I | re.S)
            if not em:
                em = re.search(r"Extras:\s*</div>.*?<div[^>]*class=\"description\"[^>]*>(.*?)</div>", block, re.I | re.S)
            feats = _parse_features(em.group(1)) if em else []
            releases.append({"name": name, "year": int(ym.group(1)) if ym else None, "features": feats})
        return releases
    return _cached(f"dvdc-film:{fid}", fetch)


def _online_only(f: dict) -> bool:
    text = f"{f['title']} {f.get('type', '')} {' '.join(f.get('parents') or [])}".lower()
    return "bd-live" in text or "bd live" in text or "online" in text


def _fmt_matches(want: str, have: str) -> bool:
    want, have = want.lower(), have.lower()
    if want == "4k":
        return "4k" in have
    if want == "bluray":
        return "blu-ray" in have and "4k" not in have
    return have.startswith("dvd")


def dvdcompare_candidates(movie_title: str, year: str, fmt: str, durations: list[int],
                          edition_hint: str = "") -> list[dict]:
    """Every DVDCompare release whose extras fit these run times, best first.

    Only pages for the disc's own format are used when DVDCompare has one
    (a DVD's extras differ from the Blu-ray's). Matches on long extras count
    more than short ones, and a release whose name mentions the disc's
    edition (e.g. "Diamond Edition") or America gets a bonus. Ties go to the
    release whose own extras list is covered best by what's on the disc.
    """
    results = dvdcompare_search(movie_title)
    yr = int(year) if year and str(year).isdigit() else None
    films = [r for r in results if yr is None or r["year"] in (yr, yr - 1, yr + 1)]
    same_fmt = [r for r in films if _fmt_matches(fmt, r["format"])]
    films = same_fmt or films
    films.sort(key=lambda r: r["year"] != yr)
    hint_words = [w for w in re.findall(r"[a-z]{4,}", edition_hint.lower())
                  if w not in ("edition", "the", "disc", "blu", "bluray", "with", "and")]
    cands = []
    for film in films[:3]:
        for idx, rel in enumerate(dvdcompare_film(film["fid"])):
            feats = [f for f in rel["features"] if f["seconds"] and not _online_only(f)]
            if not feats:
                continue
            score = 0.0
            for d in durations:
                if any(abs(f["seconds"] - d) <= TOLERANCE for f in feats):
                    score += min(d / 60.0, 10.0) if d >= 60 else 0.3
            name = rel["name"].lower()
            hinted = bool(hint_words and any(w in name for w in hint_words))
            if hinted:
                score += 3
            if re.search(r"america|\bus\b|region a|\br1\b", name):
                score += 1
            # a film page can list other formats' releases too: a DVD's extras come from a DVD release
            other_fmt = ("blu-ray" in name or "4k" in name) if fmt == "dvd" else name.startswith("dvd")
            if other_fmt:
                score -= 2
            longs = [f for f in feats if f["seconds"] >= 60]
            hit = [f for f in longs if any(abs(f["seconds"] - d) <= TOLERANCE for d in durations)]
            coverage = len(hit) / len(longs) if longs else 0.0
            if score > 1.5:
                cands.append({"id": f"{film['fid']}:{idx}", "score": round(score, 1), "coverage": coverage, "hinted": hinted,
                              "film": film, "rel": rel, "feats": feats})
    cands.sort(key=lambda c: (c["score"], c["coverage"]), reverse=True)
    return cands


def _release_result(c: dict) -> dict:
    film, rel = c["film"], c["rel"]
    # other releases of the same film, used only for clear (long, unique) leftovers
    others = [f for r in dvdcompare_film(film["fid"]) if r is not rel
              for f in r["features"] if f["seconds"] and f["seconds"] >= 120 and not _online_only(f)
              and guess_category(f["title"] + " " + " ".join(f.get("parents") or []), f.get("type", "")) != "Trailers"]
    return {"id": c["id"], "name": rel["name"], "source": f"DVDCompare: {rel['name']}",
            "url": f"{DVDCOMPARE}/film.php?fid={film['fid']}",
            "features": c["feats"], "other_features": others, "commentaries": commentaries(rel["features"])}


def match_dvdcompare(movie_title: str, year: str, fmt: str, durations: list[int],
                     edition_hint: str = "") -> dict | None:
    """The best-fitting release (see dvdcompare_candidates), or None."""
    cands = dvdcompare_candidates(movie_title, year, fmt, durations, edition_hint)
    return _release_result(cands[0]) if cands else None


def dvdcompare_release(rid: str) -> dict | None:
    """A release picked on the review page, by its id ("<film id>:<index>")."""
    try:
        fid, idx = (int(x) for x in str(rid).split(":"))
        rel = dvdcompare_film(fid)[idx]
    except (ValueError, IndexError):
        return None
    feats = [f for f in rel["features"] if f["seconds"] and not _online_only(f)]
    return _release_result({"id": rid, "film": {"fid": fid}, "rel": rel, "feats": feats})


def commentaries(features: list[dict]) -> list[str]:
    """Names of the audio commentaries a release lists, e.g. 'Commentary by director Jon Watts'."""
    out = []
    for f in features:
        text = f"{f.get('type') or ''} {f.get('title') or ''}"
        if "commentar" not in text.lower() or "video commentary" in text.lower():
            continue
        t = f["title"] if "commentar" in (f.get("title") or "").lower() else f"{f.get('type')} {f.get('title')}"
        t = re.sub(r"(?i)^(?:feature[- ]length\s+|optional\s+|scene[- ]specific\s+)?(?:audio\s+)?commentar(?:y|ies)\b",
                   "Commentary", t.strip())
        t = re.sub(r"\s+", " ", t).strip(" :-")[:90]
        if t and t not in out:
            out.append(t)
    return out


# --------------------------------------------------------------------------- TheDiscDB

TDB_QUERY = """
query($q: String!) {
  mediaItems(where: { title: { contains: $q } }, first: 25) {
    nodes {
      title year type
      releases {
        title slug year
        discs {
          name format contentHash
          titles { index sourceFile duration size itemType description season episode }
        }
      }
    }
  }
}"""


def _tdb_seconds(v) -> int:
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v)
    m = re.match(r"^(?:(\d+)\.)?(\d+):(\d+):(\d+)", s)  # .NET TimeSpan "01:28:23" or "1.02:03:04"
    if m:
        d, h, mi, se = (int(x or 0) for x in m.groups())
        return d * 86400 + h * 3600 + mi * 60 + se
    return 0


def thediscdb_lookup(title: str) -> list[dict]:
    def fetch():
        raw = _http(THEDISCDB, json_body={"query": TDB_QUERY, "variables": {"q": title}})
        data = json.loads(raw)
        if data.get("errors"):
            raise RuntimeError(data["errors"][0].get("message", "TheDiscDB query error"))
        items = data["data"]["mediaItems"]
        return items.get("nodes", items) if isinstance(items, dict) else items
    return _cached(f"tdb:{title.lower()}", fetch, max_age_days=7)


def match_thediscdb(title: str, ripped: list[dict], year: str = "") -> dict | None:
    """Find the disc on TheDiscDB whose playlists match ours. ripped = [{source_file, seconds}]"""
    ours = {(t["source_file"] or "").split("(")[0].lower(): t["seconds"] for t in ripped if t.get("source_file")}
    if not ours:
        return None
    best = None
    for item in thediscdb_lookup(title):
        if year and str(item.get("year") or "") not in ("", str(year)):
            continue
        for rel in item.get("releases") or []:
            for disc in rel.get("discs") or []:
                rows = disc.get("titles") or []
                hits = 0
                for r in rows:
                    sf = (r.get("sourceFile") or "").lower()
                    if sf in ours and abs(_tdb_seconds(r.get("duration")) - ours[sf]) <= TOLERANCE:
                        hits += 1
                labelled = [r for r in rows if (r.get("description") or "").strip() and
                            (r.get("itemType") or "").lower() not in ("", "mainmovie", "episode")]
                if hits >= 2 and labelled and (best is None or hits > best[0]):
                    best = (hits, item, rel, disc)
    if not best:
        return None
    _, item, rel, disc = best
    return {"source": f"TheDiscDB: {item.get('title')} - {rel.get('title')} ({disc.get('name') or disc.get('format')})",
            "titles": disc.get("titles") or []}


# --------------------------------------------------------------------------- apply to a plan

def _trial(todo: list[dict], rel: dict) -> list[tuple]:
    """What naming with this release would give, without touching the plan."""
    copies = [dict(it) for it in todo]
    _assign(copies, rel)
    return [(it["name"], it["action"]) for it in copies]


def _assign(todo: list[dict], dvdc: dict) -> int:
    """Name extras from one DVDCompare release by run time, in place. Returns how many were named."""
    feats = [f for f in dvdc["features"] if f["seconds"] and not _online_only(f)]
    used: dict[int, int] = {}
    used_by: dict[int, str] = {}
    names_taken: dict[str, int] = {}
    n = 0
    for it in todo:
        d = it["duration_seconds"]
        cands = [f for f in feats if abs(f["seconds"] - d) <= TOLERANCE]
        if not cands and d >= MIN_UNIQUE_SECONDS:
            alt = [f for f in dvdc.get("other_features", []) if abs(f["seconds"] - d) <= 1]
            if len({f["title"].lower() for f in alt}) == 1:
                cands = alt[:1]
        if not cands:
            continue
        # prefer the closest run time; the same extra listed twice (e.g. as a
        # start-up trailer and again under Bonus trailers) counts as one candidate
        best_delta = min(abs(f["seconds"] - d) for f in cands)
        closest = [f for f in cands if abs(f["seconds"] - d) == best_delta]
        if len({f["title"].lower() for f in closest}) == 1:
            cands = [next((f for f in closest if id(f) not in used), closest[0])]
        if len(cands) > 1:
            names = "; ".join(dict.fromkeys(f["title"] for f in cands))
            it["note"] = f"could be: {names}"[:300]
            continue
        f = cands[0]
        unsure = d < MIN_UNIQUE_SECONDS and any(abs(f2["seconds"] - d) <= 8 for f2 in feats if f2 is not f)
        name = f["title"]
        if f.get("play_all") and not f.get("group"):
            name = f"{name} (All)"
        key = id(f)
        if key in used:  # a second title matching the same extra is almost always a copy
            copy = f"Probably a Copy of {name}"  # skipped, but still says what it is
            it.update(action="skip", name=copy, dvdc_name=copy, note=f"looks like a copy of {used_by[key]} ({name})")
            continue
        used[key] = 1
        used_by[key] = it["source"]
        n_same = names_taken.get(name.lower(), 0) + 1
        names_taken[name.lower()] = n_same
        if n_same > 1:
            name = f"{name} ({n_same})"
        it["name"] = re.sub(r"\s+", " ", name).strip()
        it["category"] = guess_category(f["title"] + " " + " ".join(f.get("parents") or []), f.get("type", ""))
        it["note"] = ("named by DVDCompare run time (short clip, worth a quick check)"
                      if unsure else "named by DVDCompare run time")
        n += 1
    return n


def apply_release(plan: dict, todo: list[dict], dvdc: dict) -> int:
    """Name `todo` extras from this release and record it on the plan."""
    n = _assign(todo, dvdc)
    for it in todo:
        if str(it.get("note", "")).startswith("named by DVDCompare"):
            it["dvdc_name"] = it["name"]
    plan["commentaries"] = dvdc.get("commentaries") or []
    plan["extras_sources"] = [s for s in plan.get("extras_sources", []) if not s.startswith("DVDCompare")]
    if n:
        plan["extras_sources"].append(dvdc["source"])
        plan["dvdcompare_url"] = dvdc["url"]
    return n


def rename_from_release(plan: dict, rid: str) -> dict:
    """Review page: the user picked another release. Undo the old release's names (unless
    the user typed their own) and name the extras from the new one."""
    dvdc = dvdcompare_release(rid)
    if not dvdc:
        raise ValueError("DVDCompare release not found")
    todo = []
    for it in plan.get("items", []):
        if it.get("role") != "extra" or "auto_name" not in it:
            continue
        note = str(it.get("note", ""))
        mine = note.startswith(("named by DVDCompare", "could be:", "looks like a copy of")) or not note
        typed = it["name"] != (it.get("dvdc_name") or it["auto_name"])
        if not mine or typed:
            continue
        it.update(name=it["auto_name"], action=it.get("auto_action", "move"), category="Other", note="")
        it.pop("dvdc_name", None)
        todo.append(it)
    apply_release(plan, todo, dvdc)
    plan["edition"] = rid
    plan["edition_sure"] = True
    plan["warnings"] = [w for w in plan.get("warnings", []) if "releases that fit these extras" not in w
                        and "still have placeholder names" not in w]
    left = sum(1 for it in plan["items"] if it.get("role") == "extra" and it.get("action") != "skip"
               and not str(it.get("note", "")).startswith("named by"))
    if left:
        plan["warnings"].append(f"{left} extra(s) still have placeholder names.")
    return plan


def name_extras(plan: dict, disc: dict, warnings: list[str], log=print, use_dvdcompare: bool = True) -> None:
    """Fill in names and categories for the plan's extras, in place."""
    extras = [it for it in plan["items"] if it.get("role") == "extra"]
    if not extras:
        return
    by_file = {t.get("output_file"): t for t in disc.get("titles", [])}
    title = plan.get("title") or ""
    named = 0

    # 1. TheDiscDB: exact by playlist (skipped when discdb.py already recognised the disc)
    try:
        if plan.get("discdb_matched"):
            raise LookupError("already matched")
        ripped = [{"source_file": by_file.get(it["source"], {}).get("source_file"),
                   "seconds": it["duration_seconds"]} for it in plan["items"]]
        tdb = match_thediscdb(title, ripped, plan.get("year", ""))
    except LookupError:
        tdb = None
    except Exception as exc:
        tdb = None
        log(f"    (TheDiscDB lookup failed: {exc})")
    if tdb:
        rows = {(r.get("sourceFile") or "").lower(): r for r in tdb["titles"]}
        for it in extras:
            sf = (by_file.get(it["source"], {}).get("source_file") or "").split("(")[0].lower()
            r = rows.get(sf)
            if not r or not (r.get("description") or "").strip():
                continue
            if (r.get("itemType") or "").lower() in ("mainmovie", "main movie", "movie", "episode"):
                continue
            it["name"] = r["description"].strip()
            cat = THEDISCDB_TYPES.get((r.get("itemType") or "").lower().replace(" ", ""))
            it["category"] = cat or guess_category(it["name"])
            it["note"] = "named by TheDiscDB"
            named += 1
        if named:
            plan.setdefault("extras_sources", []).append(tdb["source"])

    # 2. DVDCompare: by run time, for whatever is still unnamed. A film often has several
    #    releases (US, UK, special editions...). Pick the one whose extras fit the disc; if
    #    another release fits almost as well and would name things differently, say so and
    #    let the review page offer the choice.
    todo = [it for it in extras if not str(it.get("note", "")).startswith("named by") and it.get("action") != "skip"]
    if todo and plan.get("kind") == "movie" and use_dvdcompare:
        try:
            fmt_key = {"4k blu-ray": "4k", "4k blu ray": "4k", "4k": "4k", "blu-ray": "bluray", "bluray": "bluray"}.get(
                str(plan.get("format") or "").lower(), "dvd")
            cands = dvdcompare_candidates(title, plan.get("year", ""), fmt_key,
                                          [it["duration_seconds"] for it in todo],
                                          edition_hint=disc.get("name", ""))
        except Exception as exc:
            cands = []
            log(f"    (DVDCompare lookup failed: {exc})")
        if cands:
            for it in todo:
                it["auto_name"], it["auto_action"] = it["name"], it["action"]
            best = _release_result(cands[0])
            outcome = _trial(todo, best)
            rivals = []
            for c in cands[1:12]:
                if c["score"] < 0.8 * cands[0]["score"]:
                    break
                if cands[0].get("hinted") and not c.get("hinted"):
                    continue  # the disc label names the best one's edition
                if _trial(todo, _release_result(c)) != outcome:
                    rivals.append(c)
            plan["editions"] = [{"id": c["id"], "name": c["rel"]["name"], "score": c["score"]}
                                for c in cands[:12]]
            plan["edition"] = best["id"]
            plan["edition_sure"] = not rivals
            if rivals:
                warnings.append(f"This film has {len(rivals) + 1} releases that fit these extras. "
                                f"Using \"{best['name']}\"; pick another under Release if the names look wrong.")
            named += apply_release(plan, todo, best)

    if extras:
        left = sum(1 for it in extras if it.get("action") != "skip" and not it["note"].startswith("named by"))
        if named:
            log(f"    named {named} extra(s) automatically; {left} still need a name")
        if left:
            warnings.append(f"{left} extra(s) still have placeholder names.")
