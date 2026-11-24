r"""
catalog.py - your own disc database (disc_catalog.db in the data folder, see paths.py).

Every disc you file from the review page is recorded here exactly as you confirmed it:
the disc (label, fingerprint, format, which movie or show/season) and every title on
it (playlist, length, byte size, and what it turned out to be). It's an ordinary
SQLite file, so any tool can read it.

It's used two ways:
  * naming checks it first, so a disc you've ripped before (or another copy of the
    same release) is recognised exactly;
  * `python catalog.py --export discs.json` writes everything out as JSON, ready to
    publish or share if you ever release this tool. Nothing personal is stored:
    no file paths, user names or keys.

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
import sys
from pathlib import Path

# next to the script, or next to Shelfrip.exe when built with PyInstaller
HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
import paths
DB_PATH = paths.DATA_DIR / "disc_catalog.db"
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS discs (
    id            INTEGER PRIMARY KEY,
    content_hash  TEXT,            -- TheDiscDB-compatible fingerprint (may be empty for old rips)
    disc_label    TEXT,
    disc_name     TEXT,            -- MakeMKV's disc name, e.g. 'The Lion King Diamond Edition'
    format        TEXT,            -- 'DVD', 'Blu-Ray', '4K Blu-Ray'
    kind          TEXT,            -- 'movie' or 'tv'
    title         TEXT,
    year          TEXT,
    tmdb_id       TEXT,
    imdb_id       TEXT,
    season        INTEGER,
    match_source  TEXT,            -- how it was identified before your review
    title_count   INTEGER,
    filed_at      TEXT,
    tool_version  INTEGER
);
CREATE INDEX IF NOT EXISTS discs_hash ON discs(content_hash);
CREATE INDEX IF NOT EXISTS discs_label ON discs(disc_label);
CREATE TABLE IF NOT EXISTS titles (
    disc_id       INTEGER REFERENCES discs(id) ON DELETE CASCADE,
    title_index   INTEGER,         -- MakeMKV title number
    source_file   TEXT,            -- playlist / title set, e.g. '00800.mpls'
    segments      TEXT,
    duration_s    INTEGER,
    size_bytes    INTEGER,
    chapters      INTEGER,
    kept          INTEGER,         -- 1 = filed into Plex, 0 = skipped
    role          TEXT,            -- main, edition, episode, extra, play_all
    category      TEXT,            -- extras folder (Featurettes, Trailers, ...)
    name          TEXT,            -- the name it was given in Plex (without folder)
    season        INTEGER,
    episode       INTEGER,
    episode_last  INTEGER,         -- for a title holding several episodes
    note          TEXT
);
CREATE INDEX IF NOT EXISTS titles_size ON titles(size_bytes);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    return con


# --------------------------------------------------------------------------- record

def record(plan: dict, disc: dict, path: Path = DB_PATH) -> int:
    """Store one filed disc. Re-filing the same rip folder replaces its earlier record."""
    by_file = {t.get("output_file"): t for t in disc.get("titles", [])}
    con = connect(path)
    with con:
        stamp = plan.get("filed_at") or time.strftime("%Y-%m-%dT%H:%M:%S")
        con.execute("DELETE FROM discs WHERE disc_label = ? AND filed_at = ?", (plan.get("disc_label"), stamp))
        cur = con.execute(
            "INSERT INTO discs (content_hash, disc_label, disc_name, format, kind, title, year, tmdb_id, "
            "imdb_id, season, match_source, title_count, filed_at, tool_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (disc.get("content_hash") or "", plan.get("disc_label"), disc.get("name") or "",
             plan.get("format"), plan.get("kind"), plan.get("title"), str(plan.get("year") or ""),
             str(plan.get("tmdb_id") or ""), plan.get("imdb_id") or "",
             plan.get("season") if plan.get("kind") == "tv" else None,
             plan.get("match_source") or "", len(plan.get("items", [])), stamp,
             plan.get("planner_version")))
        disc_id = cur.lastrowid
        for it in plan.get("items", []):
            t = by_file.get(it.get("source"), {})
            eps = it.get("episodes") or ([it["episode"]] if it.get("episode") else [])
            ep = it.get("role") == "episode"
            con.execute(
                "INSERT INTO titles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (disc_id, t.get("title_id"), t.get("source_file"), t.get("segments"),
                 it.get("duration_seconds") or t.get("duration_seconds"), t.get("size_bytes"),
                 t.get("chapters"), 1 if it.get("action") == "move" else 0, it.get("role"),
                 it.get("category") if it.get("role") == "extra" else None, it.get("name"),
                 it.get("season") if ep else None, eps[0] if ep and eps else None,
                 eps[-1] if ep and len(eps) > 1 else None, _note(it)))
    con.close()
    return disc_id


def _note(it: dict) -> str:
    note = it.get("note") or ""
    sp = it.get("split") if it.get("needs_split") else None
    if sp and sp.get("starts"):  # how it was cut, so the same disc can be split the same way next time
        how = "split at chapters " + ", ".join(str(x + 1) for x in sp["starts"])
        if sp.get("copy_opening") and sp.get("opening"):
            how += f"; opening ({sp['opening']} ch) added to each"
        if sp.get("copy_ending") and sp.get("ending"):
            how += f"; ending ({sp['ending']} ch) added to each"
        note = "; ".join(x for x in (note, how) if x)
    return note


# --------------------------------------------------------------------------- lookup

def lookup(disc: dict, titles: list[dict], path: Path = DB_PATH) -> dict | None:
    """Find a disc you've filed before: by fingerprint, else by matching title byte sizes.

    Returns the same shape as discdb.lookup(), so identify.py can treat both alike.
    """
    if not path.exists():
        return None
    con = connect(path)
    try:
        row = None
        h = disc.get("content_hash")
        if h:
            row = con.execute("SELECT * FROM discs WHERE content_hash = ? ORDER BY filed_at DESC LIMIT 1",
                              (h,)).fetchone()
        if row is None:
            sizes = sorted((int(t.get("size_bytes") or 0) for t in titles), reverse=True)
            sizes = [s for s in sizes if s > 50_000_000][:3]
            if len(sizes) >= 2 or (len(sizes) == 1 and len(titles) == 1):
                q = ("SELECT disc_id, COUNT(*) n FROM titles WHERE size_bytes IN (%s) GROUP BY disc_id "
                     "ORDER BY n DESC LIMIT 1") % ",".join("?" * len(sizes))
                hit = con.execute(q, sizes).fetchone()
                if hit and hit["n"] >= min(2, len(sizes)):
                    row = con.execute("SELECT * FROM discs WHERE id = ?", (hit["disc_id"],)).fetchone()
        if row is None:
            return None
        rows = [dict(r) for r in con.execute("SELECT * FROM titles WHERE disc_id = ?", (row["id"],))]
    finally:
        con.close()

    # pair our titles with the recorded ones: exact byte size, else length + playlist
    pairs: dict[str, dict] = {}
    used: set[int] = set()
    for t in titles:
        for i, r in enumerate(rows):
            if i in used:
                continue
            same_size = r["size_bytes"] and r["size_bytes"] == t.get("size_bytes")
            same_play = (r["source_file"] and r["source_file"] == t.get("source_file") and
                         abs((r["duration_s"] or 0) - t.get("duration_seconds", 0)) <= 1)
            if same_size or same_play:
                pairs[t["output_file"]] = _as_discdb_row(r)
                used.add(i)
                break
    if len(pairs) < max(1, len(titles) // 2):
        return None
    return {
        "kind": row["kind"], "title": row["title"], "year": row["year"], "tmdb_id": row["tmdb_id"],
        "imdb_id": row["imdb_id"], "release": "", "disc_name": row["disc_name"],
        "how": "your catalog", "pairs": pairs, "local": True,
        "source": f"your disc catalog ({row['title']}, filed {str(row['filed_at'])[:10]})",
    }


def _as_discdb_row(r: dict) -> dict:
    """Translate a catalog title into TheDiscDB's field names (used by identify.apply_discdb)."""
    role = r["role"]
    item_type = {"main": "MainMovie", "edition": "MainMovie", "episode": "Episode"}.get(role, "")
    if role == "extra" and r["kept"]:
        item_type = {"Deleted Scenes": "DeletedScene", "Trailers": "Trailer"}.get(r["category"] or "", "Extra")
    placeholder = re.match(r"^(Extra \d+ \(|Unused clip|Probably )", r["name"] or "")
    return {"itemType": item_type,
            # skipped extras keep their name too, so the next person sees what it is (it stays skipped)
            "description": r["name"] if role == "extra" and not placeholder else "",
            "season": str(r["season"]) if r["season"] is not None else "",
            "episode": str(r["episode"]) if r["episode"] is not None else "",
            "size": r["size_bytes"], "duration": r["duration_s"], "category": r["category"],
            "kept": bool(r["kept"])}


# --------------------------------------------------------------------------- export

def export(out: Path, path: Path = DB_PATH) -> int:
    con = connect(path)
    discs = []
    for d in con.execute("SELECT * FROM discs ORDER BY filed_at"):
        dd = dict(d)
        dd["titles"] = [{k: v for k, v in dict(t).items() if k != "disc_id"}
                        for t in con.execute("SELECT * FROM titles WHERE disc_id = ? ORDER BY title_index", (d["id"],))]
        dd.pop("id")
        discs.append(dd)
    con.close()
    out.write_text(json.dumps({"schema_version": SCHEMA_VERSION, "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                               "discs": discs}, indent=2), encoding="utf-8")
    return len(discs)


def main() -> None:
    ap = argparse.ArgumentParser(description="Your disc catalog")
    ap.add_argument("--export", metavar="FILE", help="write every disc to a JSON file")
    args = ap.parse_args()
    if args.export:
        n = export(Path(args.export))
        print(f"Exported {n} disc(s) to {args.export}")
        return
    con = connect()
    n = con.execute("SELECT COUNT(*) FROM discs").fetchone()[0]
    print(f"{DB_PATH}: {n} disc(s)")
    for d in con.execute("SELECT title, year, format, disc_label, filed_at FROM discs ORDER BY filed_at DESC LIMIT 20"):
        print(f"  {d['filed_at'][:10]}  {d['title']} ({d['year']})  [{d['format']}]  {d['disc_label']}")


if __name__ == "__main__":
    main()
