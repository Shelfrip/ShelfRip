r"""
community.py - the shared Shelfrip disc database.

When you file a disc (and "share_discs" is on), what you filed is sent to the database:
the disc's fingerprint, its volume label and the name written on it, its format, what it is
(the TMDB/IMDb id, title, year and season), the Shelfrip version, and for each title its
playlist, length, size and what it turned out to be (the movie, s01e03, the featurette
"Making Of", a clip you skipped). For the titles that matter (kept ones, and every movie or
episode candidate) its tracks go too: the video codec and size, each audio track's language,
codec, channels and commentary name, and each subtitle's language and whether it's forced.
Nothing else: no file paths, no folder names, no library details, no keys, no names of people
using Shelfrip. Your install is identified only by a random
id (install_id in config.json), so repeat filings of the same disc replace each other instead of
counting twice.

When anyone rips a disc, identify.py asks the database before TheDiscDB, and uses the answer
most people agreed on. Answers only one person has given yet are used too, but the review
page says so and the disc isn't filed automatically.

If the review page showed you the database's answer and you filed a title exactly as it was
shown, that title is marked "accepted": the database counts it as half a vote, so clicking
through can't confirm an answer on its own. Anything you changed, or identified without the
database, counts fully. Each title's answer comes back with a confidence (0-100) built from
those weighted votes, less any disagreement and "this disc is wrong" reports.

The server is in server/ (a Cloudflare Worker); its address is paths.COMMUNITY_URL, or
"community_url" in config.json.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

import paths

TIMEOUT = 8
PLACEHOLDER = re.compile(r"^(Extra \d+ \(|Unused clip|Probably )", re.I)  # see identify.PLACEHOLDER
GENERIC_COMMENTARY = re.compile(r"^commentary( \d+)?$", re.I)
SOURCE = "the Shelfrip disc database"
MAX_AUDIO, MAX_SUBS = 24, 48            # per title; the server keeps no more than this either
FORCED_SUBS, DERIVED = 4096, 2048        # MakeMKV stream flags


def base_url(cfg: dict) -> str:
    return str(cfg.get("community_url") or paths.COMMUNITY_URL or "").rstrip("/")


def install_id(cfg: dict) -> str:
    """This install's random id, made (and saved in config.json) the first time it's needed."""
    iid = str(cfg.get("install_id") or "")
    if re.fullmatch(r"[0-9a-f-]{16,64}", iid):
        return iid
    iid = str(uuid.uuid4())
    try:
        with paths.CONFIG_LOCK:
            raw = json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8-sig")) if paths.CONFIG_PATH.exists() else {}
            raw["install_id"] = iid
            paths.write_config(raw)
    except (OSError, ValueError):
        pass  # unreadable settings: use the id for this run only rather than overwrite them
    cfg["install_id"] = iid
    return iid


def signature(disc: dict) -> str:
    """A fingerprint from the titles MakeMKV lists (length and size of each), for discs without a content hash."""
    parts = sorted(f"{int(t.get('duration_seconds') or 0)}:{int(t.get('size_bytes') or 0)}"
                   for t in disc.get("titles", []) if int(t.get("size_bytes") or 0) > 0)
    if not parts:
        return ""
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def _http(url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"User-Agent": paths.USER_AGENT, "Content-Type": "application/json",
                                          "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


# --------------------------------------------------------------------------- look up

def lookup(disc: dict, titles: list[dict], cfg: dict, log=print) -> dict | None:
    """The agreed answer for this disc, in the shape identify.apply_discdb takes, or None."""
    base = base_url(cfg)
    if not base or not cfg.get("use_community", True):
        return None
    h, s = str(disc.get("content_hash") or "").lower(), signature(disc)
    if not h and not s:
        return None
    try:
        ans = _http(f"{base}/v1/disc?" + urllib.parse.urlencode({k: v for k, v in (("h", h), ("s", s)) if v}))
    except Exception as exc:
        log(f"    (Shelfrip disc database lookup failed: {exc})")
        return None
    if not ans.get("found") or not ans.get("kind"):
        return None
    # Answers come from other people's filings: only use one that's well formed, so a bad value
    # can't break planning this disc (it's planned the usual way instead).
    if (ans.get("kind") not in ("movie", "tv") or not re.fullmatch(r"\d{1,9}", str(ans.get("tmdb_id") or ""))
            or (ans.get("season") is not None and not re.fullmatch(r"\d{1,3}", str(ans.get("season"))))):
        log("    (the Shelfrip disc database's answer for this disc looks broken: not using it)")
        return None
    conf = ans.get("confidence")
    ans["confidence"] = max(0, min(100, int(conf))) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else None
    if ans.get("disputed"):
        log(f"    (the Shelfrip disc database says {ans.get('title')}, but people reported that as wrong: not using it)")
        return None

    import catalog
    rows = ans.get("titles") if isinstance(ans.get("titles"), list) else []
    rows = [r for r in rows if isinstance(r, dict)]
    try:
        pairs = _pair_titles(rows, titles, ans, catalog)
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        log(f"    (the Shelfrip disc database's answer for this disc looks broken ({exc}): not using it)")
        return None
    if len(pairs) < max(1, len(titles) // 2):
        return None
    who = ("checked by hand" if ans.get("locked") else
           f"{ans.get('agree')} of {ans.get('submitters')} people agree" if ans.get("submitters", 0) > 1 else
           "shared by 1 person so far")
    return {
        "kind": ans["kind"], "title": ans.get("title") or "", "year": ans.get("year") or "",
        "tmdb_id": str(ans.get("tmdb_id") or ""), "imdb_id": ans.get("imdb_id") or "", "release": "",
        "disc_name": ans.get("disc_name") or "", "how": "community", "pairs": pairs, "local": False,
        "community": True, "confirmed": bool(ans.get("confirmed") or ans.get("locked")),
        "season": ans.get("season"), "confidence": 100 if ans.get("locked") else ans.get("confidence"),
        "source": f"{SOURCE} ({ans.get('title')}; {who})", "src_name": "the Shelfrip disc database",
    }


def _pair_titles(rows: list[dict], titles: list[dict], ans: dict, catalog) -> dict[str, dict]:
    """Match the database's titles to this rip's files (same size, or same playlist and length),
    as rows apply_discdb takes, keyed by output file."""
    pairs: dict[str, dict] = {}
    used: set[int] = set()
    for t in titles:
        for i, r in enumerate(rows):
            if i in used:
                continue
            same_size = r.get("size_bytes") and r.get("size_bytes") == t.get("size_bytes")
            same_play = (r.get("source_file") and r.get("source_file") == t.get("source_file") and
                         abs((r.get("duration_s") or 0) - t.get("duration_seconds", 0)) <= 2)
            if same_size or same_play:
                if r.get("role") not in ("main", "edition", "episode", "extra", "play_all"):
                    raise ValueError(f"unknown role {r.get('role')!r}")
                row = catalog._as_discdb_row({
                    "role": r.get("role"), "kept": r.get("kept"), "category": r.get("category"),
                    "name": r.get("name") or "", "season": r.get("season"), "episode": r.get("episode"),
                    "size_bytes": r.get("size_bytes"), "duration_s": r.get("duration_s")})
                row["votes"], row["confirmed"] = r.get("votes", 0), bool(r.get("confirmed"))
                row["episode_last"] = str(r["episode_last"]) if str(r.get("episode_last") or "").isdigit() else ""
                c = r.get("confidence")
                row["confidence"] = 100 if ans.get("locked") else (  # 0-100; anything else is dropped
                    max(0, min(100, int(c))) if isinstance(c, (int, float)) and not isinstance(c, bool) else None)
                if isinstance(r.get("tracks"), dict):
                    row["tracks"] = r["tracks"]
                pairs[t["output_file"]] = row
                used.add(i)
                break
    return pairs


# --------------------------------------------------------------------------- share

def _title_row(it: dict, t: dict) -> dict | None:
    """One title as the database stores it (without tracks), or None if it can't be described."""
    import naming
    role = it.get("role")
    row = {"source_file": t.get("source_file") or "", "duration_s": int(it.get("duration_seconds") or
                                                                          t.get("duration_seconds") or 0),
           "size_bytes": t.get("size_bytes"), "chapters": t.get("chapters"), "role": role,
           "kept": it.get("action") == "move"}
    if role == "episode":
        eps = it.get("episodes") or ([it["episode"]] if it.get("episode") else [])
        if not eps:
            return None
        row.update(season=it.get("season") or 0, episode=eps[0], episode_last=eps[-1] if len(eps) > 1 else None)
        sp = it.get("split") if it.get("needs_split") else None
        if sp and sp.get("starts"):
            row["split"] = "chapters " + ",".join(str(x + 1) for x in sp["starts"])
    elif role == "extra":
        name = str(it.get("name") or "")
        row.update(category=it.get("category") or "Other", name="" if PLACEHOLDER.match(name) else name)
    elif role == "edition":
        row["name"] = naming.edition_of(str(it.get("name") or ""))
    return row


def answer_key(row: dict) -> str:
    """What one title "is", as the server compares it (server/worker.js: answerKey)."""
    n = re.sub(r"\s+", " ", str(row.get("name") or "").lower()).strip()
    kept = 1 if row.get("kept") else 0
    role = row.get("role")
    if role == "episode":
        last = row.get("episode_last") or ""
        return f"episode|{row.get('season')}|{row.get('episode')}|{last}|{kept}"
    if role == "extra":
        return f"extra|{row.get('category')}|{n}|{kept}"
    if role == "edition":
        return f"edition|{n}|{kept}"
    return f"{role}|{kept}"


def note_shown(plan: dict, disc: dict) -> None:
    """Remember what the review page is about to show for a disc the database identified, so filing
    it unchanged can be told apart from filing it after a change (see the top of this file)."""
    ca = plan.get("community_answer")
    if not ca:
        return
    by_file = {t.get("output_file"): t for t in disc.get("titles", [])}
    # Only titles the database had an answer for: a name Shelfrip guessed itself (TMDB, DVDCompare,
    # the disc's playlist names) and filed unchanged isn't someone agreeing with the database.
    known = set(ca["sources"]) if isinstance(ca.get("sources"), list) else None  # None: plans made before 1.1.0
    shown = {}
    for it in plan.get("items", []):
        if known is not None and it.get("source") not in known:
            continue
        row = _title_row(it, by_file.get(it.get("source"), {}))
        if row and it.get("source"):
            shown[it["source"]] = answer_key(row)
    ca["shown"] = shown


def tracks(t: dict, it: dict) -> dict | None:
    """A title's video, audio and subtitle tracks, compactly. Commentary names come from the
    review page (it["audio"]); "Commentary" or "Commentary 2" on its own isn't worth sending."""
    streams = t.get("streams") or []
    if not streams:
        return None
    named = {a.get("i"): a for a in it.get("audio") or [] if isinstance(a, dict)}
    out: dict = {"audio": [], "subs": []}
    k = 0
    for st in streams:
        typ = str(st.get("type") or "")
        flags = int(st.get("flags") or 0)
        if typ == "Video" and "video" not in out:
            out["video"] = {"codec": str(st.get("codec") or "")[:20], "res": str(st.get("resolution") or "")[:20]}
        elif typ == "Audio":
            k += 1
            if len(out["audio"]) >= MAX_AUDIO:
                continue
            mine = named.get(k) or {}
            com = bool(mine.get("commentary")) if mine else bool(flags & 3)
            name = str(mine.get("name") or "").strip() if com else ""
            a = {"lang": str(st.get("lang3") or st.get("lang") or "")[:3], "codec": str(st.get("codec") or "")[:20],
                 "ch": int(st.get("channels") or 0)}
            if com:
                a["commentary"] = 1
                if name and not GENERIC_COMMENTARY.match(name):
                    a["name"] = name[:150]
            if flags & DERIVED:
                a["core"] = 1      # e.g. the AC3 core inside a TrueHD track
            out["audio"].append(a)
        elif typ == "Subtitles" and len(out["subs"]) < MAX_SUBS:
            sub = {"lang": str(st.get("lang3") or st.get("lang") or "")[:3]}
            if flags & FORCED_SUBS:
                sub["forced"] = 1
            out["subs"].append(sub)
    return out if out["audio"] or out["subs"] or out.get("video") else None


def submission(plan: dict, disc: dict, cfg: dict) -> dict | None:
    """What gets sent for one filed disc (see the top of this file), or None if there's nothing useful."""
    if plan.get("kind") not in ("movie", "tv") or not plan.get("tmdb_id"):
        return None
    by_file = {t.get("output_file"): t for t in disc.get("titles", [])}
    ca = plan.get("community_answer") or {}
    shown = ca.get("shown") if isinstance(ca.get("shown"), dict) and _ident(ca) == _ident(plan) else {}
    out = []
    for it in plan.get("items", []):
        t = by_file.get(it.get("source"), {})
        row = _title_row(it, t)
        if not row:
            continue
        if row["kept"] or row["role"] in ("main", "edition", "episode"):
            tr = tracks(t, it)
            if tr:
                row["tracks"] = tr
        if shown.get(it.get("source")) == answer_key(row):
            row["accepted"] = True   # filed just as the database's answer was shown: counts as half a vote
        out.append(row)
    if not out:
        return None
    return {
        "install_id": install_id(cfg), "app_version": paths.APP_VERSION,
        "disc": {"content_hash": str(disc.get("content_hash") or "").lower(), "signature": signature(disc),
                 "format": plan.get("format") or "", "label": plan.get("disc_label") or "",
                 "name": disc.get("name") or ""},
        "media": {"kind": plan["kind"], "title": plan.get("title") or "", "year": str(plan.get("year") or ""),
                  "tmdb_id": str(plan["tmdb_id"]), "imdb_id": plan.get("imdb_id") or "",
                  "season": plan.get("season") if plan["kind"] == "tv" else None},
        "titles": out,
    }


def _ident(x: dict) -> tuple:
    return (x.get("kind"), str(x.get("tmdb_id") or ""), x.get("season") if x.get("kind") == "tv" else None)


def wrong_answer(plan: dict) -> dict | None:
    """The shared answer this disc was given, if you then filed (or re-matched) it as something else."""
    ca = plan.get("community_answer")
    if not ca and SOURCE in str(plan.get("match_source") or "") and plan.get("refiled"):
        ca = {**(plan["refiled"].get("from") or {}), "kind": plan.get("kind")}  # filed before this was noted
    if not ca or not ca.get("tmdb_id") or _ident(ca) == _ident(plan):
        return None
    return {"kind": ca.get("kind"), "tmdb_id": str(ca["tmdb_id"]), "season": ca.get("season")}


def report(plan: dict, body: dict, cfg: dict, log=print) -> None:
    """Tell the database the answer it gave for this disc was wrong (counts against that answer)."""
    wrong = wrong_answer(plan)
    if not wrong:
        return
    try:
        _http(f"{base_url(cfg)}/v1/report", {
            "install_id": body["install_id"], "disc": {k: body["disc"][k] for k in ("content_hash", "signature")},
            "wrong": wrong, "instead": {k: body["media"][k] for k in ("kind", "title", "year", "tmdb_id", "season")}})
        log("Told the Shelfrip disc database its answer for this disc was wrong.")
    except Exception as exc:
        log(f"(couldn't report the wrong answer to the Shelfrip disc database: {exc})")


def send_feedback(fb: dict, cfg: dict) -> dict:
    """Send a message from the Feedback button. Returns {ok} or {error}."""
    import platform
    base = base_url(cfg)
    if not base:
        return {"error": "There's no feedback address set up in this copy of Shelfrip."}
    body = {"install_id": install_id(cfg), "app_version": paths.APP_VERSION, "kind": str(fb.get("kind") or "other"),
            "title": str(fb.get("title") or "")[:200], "body": str(fb.get("body") or "")[:5000],
            "contact": str(fb.get("contact") or "")[:200], "os": platform.platform(terse=True)}
    if not fb.get("versions", True):
        body["app_version"] = body["os"] = ""
    try:
        _http(f"{base}/v1/feedback", body)
        return {"ok": True}
    except urllib.error.HTTPError as exc:
        return {"error": "Too much feedback from here today. Try again tomorrow." if exc.code == 429
                else f"The feedback server said no ({exc.code})."}
    except Exception as exc:
        return {"error": f"Couldn't reach the feedback server ({exc}). Check your internet connection."}


def admin_token() -> str:
    """The database's admin token, if this is the copy that deployed it (server/.secrets.json)."""
    try:
        return str(json.loads((paths.APP_DIR / "server" / ".secrets.json").read_text(encoding="utf-8")).get("ADMIN_TOKEN") or "")
    except (OSError, ValueError):
        return ""


def admin(path: str, cfg: dict, body: dict | None = None) -> dict:
    """Call an admin endpoint (feedback inbox, reports) with the local admin token."""
    token, base = admin_token(), base_url(cfg)
    if not token or not base:
        return {"error": "not the admin copy"}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{base}/v1/admin/{path}", data=data, method="POST" if data else "GET",
                                 headers={"User-Agent": paths.USER_AGENT, "Content-Type": "application/json",
                                          "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def share(plan: dict, disc: dict, cfg: dict, log=print) -> None:
    """Send one filed disc in the background. Never holds up or fails filing."""
    base = base_url(cfg)
    if not base or not cfg.get("share_discs", True):
        return
    body = submission(plan, disc, cfg)
    if not body or not (body["disc"]["content_hash"] or body["disc"]["signature"]):
        return

    def run():
        try:
            _http(f"{base}/v1/submit", body)
            log(f"Shared {plan.get('title')} with the Shelfrip disc database.")
            report(plan, body, cfg, log)
            try:  # remembered for the Pioneer badge: nobody had added this disc before you
                import progress
                d = body["disc"]
                progress.note_shared("h:" + d["content_hash"] if d["content_hash"] else "s:" + d["signature"],
                                     first=SOURCE not in str(plan.get("match_source") or ""))
            except Exception:
                pass
        except Exception as exc:
            log(f"(couldn't share {plan.get('title')} with the Shelfrip disc database: {exc})")
    threading.Thread(target=run, daemon=True).start()
