r"""
discdb.py - recognise a disc before guessing (used by identify.py and review.py).

Two sources, tried in order:

  1. TheDiscDB (thediscdb.com). Community database that records, for a specific
     disc, which title is the movie, which are episodes (with season/episode
     numbers) and what each extra is. We look a disc up two ways:
       * by its fingerprint ("content hash": MD5 of the sizes of the files in
         BDMV\STREAM or VIDEO_TS), which rip_all.py records while the disc is in
         the drive, and
       * by the exact byte size of its biggest ripped titles (works for older rips
         that have no fingerprint).
  2. learned_labels.json (in the data folder, see paths.py). Every time you file a disc from
     the review page, its label (minus the disc number) is remembered with the
     TMDB match you confirmed, so disc 2, 3, 4 of the same set match themselves.

Only the Python standard library is used.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import sys
from pathlib import Path

# next to the script, or next to Shelfrip.exe when built with PyInstaller
HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
import paths
LEARNED = paths.DATA_DIR / "learned_labels.json"

THEDISCDB = "https://thediscdb.com/graphql"

_FIELDS = """
      title year type
      externalids { tmdb imdb tvdb }
      releases { title slug year
        discs { name format contentHash
          titles { index sourceFile segmentMap duration size itemType description season episode }
        }
      }"""

Q_HASH = "query($h: String!) { mediaItems(where: { releases: { some: { discs: { some: { contentHash: { eq: $h } } } } } }, first: 5) { nodes {" + _FIELDS + " } } }"
# Title filters only work through ReleaseDisc.disc on the live API (checked 2026-11-05)
def q_size(n: int) -> str:
    args = ", ".join(f"$s{i}: Long!" for i in range(n))
    ors = ", ".join(f"{{ size: {{ eq: $s{i} }} }}" for i in range(n))
    return ("query(" + args + ") { mediaItems(where: { releases: { some: { discs: { some: { disc: { titles: { some: { or: ["
            + ors + "] } } } } } } } }, first: 10) { nodes {" + _FIELDS + " } } }")


# --------------------------------------------------------------------------- fingerprint

def fingerprint(root: str) -> dict | None:
    """TheDiscDB content hash for a disc mounted at root (e.g. 'D:\\').

    MD5 over each file's size as an 8-byte little-endian number, files sorted by
    name: BDMV\\STREAM\\*.m2ts for Blu-ray/4K, VIDEO_TS .IFO/.BUP/.VOB files for DVD.
    Only reads file metadata. DVDs also return legacy_hash when the old all-files
    hash differs, allowing existing catalog and shared records to keep matching.
    """
    base = Path(root)
    for sub, ext, layout in ((("BDMV", "STREAM"), ".m2ts", "bluray"), (("VIDEO_TS",), (".ifo", ".bup", ".vob"), "dvd")):
        d = base.joinpath(*sub)
        try:
            if not d.is_dir():
                continue
            all_names = sorted(p.name for p in d.iterdir() if p.is_file())
            names = [n for n in all_names if ext is None or n.lower().endswith(ext)]
            if not names:
                continue
            sizes = {n: struct.pack("<Q", (d / n).stat().st_size) for n in (all_names if layout == "dvd" else names)}
            def hash_files(selected):
                return hashlib.md5(b"".join(sizes[n] for n in selected), usedforsecurity=False).hexdigest().upper()
            result = {"content_hash": hash_files(names), "layout": layout, "files": len(names)}
            if layout == "dvd":
                legacy = hash_files(all_names)
                if legacy != result["content_hash"]:
                    result["legacy_hash"] = legacy
            return result
        except OSError:
            return None
    return None


# --------------------------------------------------------------------------- TheDiscDB

def _query(query: str, variables: dict, cache_key: str) -> list[dict]:
    import extras  # shares its HTTP helper and .cache folder

    def fetch():
        raw = extras._http(THEDISCDB, json_body={"query": query, "variables": variables})
        data = json.loads(raw)
        if data.get("errors"):
            raise RuntimeError(data["errors"][0].get("message", "TheDiscDB query error"))
        items = data["data"]["mediaItems"]
        return items.get("nodes", items) if isinstance(items, dict) else items
    return extras._cached(cache_key, fetch, max_age_days=7)


def _seconds(v) -> int:
    m = re.match(r"^(?:(\d+)\.)?(\d+):(\d+):(\d+)", str(v or ""))
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _pair_titles(ours: list[dict], rows: list[dict]) -> dict[str, dict]:
    """output_file -> TheDiscDB title row. Exact byte size first, then length + playlist."""
    out: dict[str, dict] = {}
    used: set[int] = set()
    by_size = {}
    for i, r in enumerate(rows):
        by_size.setdefault(int(r.get("size") or 0), []).append(i)
    for t in ours:
        for i in by_size.get(int(t.get("size_bytes") or 0), []):
            if i not in used:
                out[t["output_file"]] = rows[i]
                used.add(i)
                break
    for t in ours:
        if t["output_file"] in out:
            continue
        sf = (t.get("source_file") or "").split("(")[0].lower()
        seg = (t.get("segments") or "").replace(" ", "")
        for i, r in enumerate(rows):
            if i in used or abs(_seconds(r.get("duration")) - t.get("duration_seconds", 0)) > 1:
                continue
            if (sf and sf == (r.get("sourceFile") or "").lower()) or (seg and seg == (r.get("segmentMap") or "")):
                out[t["output_file"]] = r
                used.add(i)
                break
    return out


def lookup(disc: dict, titles: list[dict], log=print) -> dict | None:
    """Find this exact disc on TheDiscDB. titles = ripped titles from disc_info.json."""
    candidates: list[tuple[dict, dict, dict, str]] = []  # (item, release, disc, how)
    h = disc.get("content_hash")
    try:
        if h:
            for item in _query(Q_HASH, {"h": h}, f"tdbhash:{h}"):
                for rel in item.get("releases") or []:
                    for d in rel.get("discs") or []:
                        if (d.get("contentHash") or "").upper() == h:
                            candidates.append((item, rel, d, "fingerprint"))
        if not candidates:
            sizes = sorted((int(t.get("size_bytes") or 0) for t in titles), reverse=True)
            sizes = [s for s in sizes if s > 50_000_000][:2]
            if sizes:
                for item in _query(q_size(len(sizes)), {f"s{i}": v for i, v in enumerate(sizes)},
                                   "tdbsize:" + ",".join(map(str, sizes))):
                    for rel in item.get("releases") or []:
                        for d in rel.get("discs") or []:
                            candidates.append((item, rel, d, "title sizes"))
    except Exception as exc:
        log(f"    (TheDiscDB lookup failed: {exc})")
        return None

    best = None
    for item, rel, d, how in candidates:
        pairs = _pair_titles(titles, d.get("titles") or [])
        exact = sum(1 for t in titles if t["output_file"] in pairs and
                    int(pairs[t["output_file"]].get("size") or 0) == int(t.get("size_bytes") or 0))
        # a size-only hit needs at least two titles with identical byte sizes
        if how == "title sizes" and exact < 2 and not (exact == 1 and len(titles) == 1):
            continue
        score = (how == "fingerprint", exact, len(pairs))
        if best is None or score > best[0]:
            best = (score, item, rel, d, pairs, how)
    if not best:
        return None
    _, item, rel, d, pairs, how = best
    ext = item.get("externalids") or {}
    kind = "tv" if (item.get("type") or "").lower() in ("series", "tv", "show") else "movie"
    return {
        "kind": kind, "title": item.get("title"), "year": item.get("year"),
        "tmdb_id": str(ext.get("tmdb") or ""), "imdb_id": ext.get("imdb") or "",
        "release": rel.get("title") or "", "disc_name": d.get("name") or d.get("format") or "",
        "how": how, "pairs": pairs,
        "source": f"TheDiscDB ({how}): {item.get('title')} - {rel.get('title')} "
                  f"({d.get('name') or d.get('format')})",
    }


def tdb_seasons(match: dict) -> list[int]:
    out = []
    for r in match["pairs"].values():
        if (r.get("itemType") or "").lower() == "episode" and str(r.get("season") or "").isdigit():
            out.append(int(r["season"]))
    return out


# --------------------------------------------------------------------------- learned labels

def label_key(label: str, keep_number: bool = False) -> str:
    """'TRANSFORMERS_G1_S2_D7' -> 'TRANSFORMERS G1 S2'  (disc / volume numbers removed).

    A trailing bare number is taken as a disc number ('FRIENDS_S1_2') unless keep_number:
    for a film it's usually part of the title ('THE_SANTA_CLAUSE_2')."""
    up = re.sub(r"[_\-.]+", " ", (label or "").upper()).strip()
    up = re.sub(r"\b(?:D|DISC|DISK|DVD|V|VOL|VOLUME|CD)\s?\d{1,2}\b", " ", up)
    if not keep_number:
        up = re.sub(r"\s\d{1,2}$", "", up)  # trailing bare disc number: 'FRIENDS S1 2'
    return re.sub(r"\s+", " ", up).strip()


def _load() -> dict:
    try:
        return json.loads(LEARNED.read_text(encoding="utf-8"))
    except Exception:
        return {}


def learned(label: str) -> dict | None:
    """What a disc with this label was filed as before, else None. A film's sequel isn't taken
    for the film: 'THE_SANTA_CLAUSE_2' only matches an earlier 'THE_SANTA_CLAUSE' if that was
    a TV set (where the 2 is a disc number)."""
    data = _load()
    full = label_key(label, keep_number=True)
    if full and full in data:
        return data[full]
    k = label_key(label)
    hit = data.get(k) if k else None
    if hit and k != full and hit.get("kind") != "tv":
        return None
    return hit


def remember(label: str, plan: dict) -> None:
    """Called when a disc is filed: remember label -> confirmed match. Films keep a trailing
    number in the key, so filing 'THE_SANTA_CLAUSE_2' doesn't overwrite 'THE_SANTA_CLAUSE'."""
    k = label_key(label, keep_number=plan.get("kind") != "tv")
    if not k or not plan.get("tmdb_id"):
        return
    data = _load()
    entry = {"kind": plan.get("kind"), "tmdb_id": str(plan["tmdb_id"]),
             "title": plan.get("title"), "year": plan.get("year")}
    if plan.get("kind") == "tv" and plan.get("season") is not None:
        entry["season"] = plan["season"]
        # where this disc's episodes ended, so the next disc of the set continues from there
        last = [e for it in plan.get("items", []) if it.get("role") == "episode" and it.get("action") == "move"
                and it.get("season") == plan["season"] for e in (it.get("episodes") or [it.get("episode")]) if e]
        prev = data.get(k) or {}
        if last:
            entry["last_episode"] = max([max(last)] + ([prev["last_episode"]] if prev.get("season") == plan["season"]
                                                       and prev.get("last_episode") else []))
    data[k] = entry
    tmp = LEARNED.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(LEARNED)
