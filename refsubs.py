"""
refsubs.py - reference subtitles from OpenSubtitles.com, for checking which episode a video is.

Optional (setting "reference_subtitles", off by default; needs a free OpenSubtitles.com account).
The subtitle check compares what is said in each ripped video with each episode's subtitles
from OpenSubtitles: dialogue against dialogue is a much stronger match than dialogue against
TMDB's short plot summaries.

  * Downloads go to your own OpenSubtitles account: a free account allows 20 a day. Each episode
    is downloaded once and kept in Shelfrip's cache folder (.cache\\refsubs), so a season takes
    a day or two of quota at most and later discs of the same show cost nothing.
  * The files are only read for matching. They're never added to your rips, never shared and
    never sent to the Shelfrip disc database.
  * New OpenSubtitles passwords use the desktop Secret Service keyring on Linux, or
    your Windows account's DPAPI protection on Windows.

API: https://opensubtitles.stoplight.io/docs/opensubtitles-api
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.opensubtitles.com/api/v1"
# Shelfrip's own API key (an OpenSubtitles "consumer"). A key in settings takes its place.
APP_KEY = "KgshlMrRXQ2wBrFFlpCSfeerF6zF3orx"

# ISO 639-2 (MKV track languages) -> the codes OpenSubtitles uses
LANG = {"eng": "en", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de", "spa": "es", "ita": "it", "por": "pt-PT",
        "dut": "nl", "nld": "nl", "swe": "sv", "nor": "no", "dan": "da", "fin": "fi", "pol": "pl", "cze": "cs",
        "ces": "cs", "hun": "hu", "tur": "tr", "rus": "ru", "jpn": "ja", "chi": "zh-CN", "zho": "zh-CN", "kor": "ko"}


class QuotaUsed(Exception):
    """No downloads left today on this OpenSubtitles account."""


# --------------------------------------------------------------------------- the password

PROTECTED = "dpapi:"


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src, dst = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if protect:
        ok = fn(ctypes.byref(src), "Shelfrip", None, None, None, 0, ctypes.byref(dst))
    else:
        ok = fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst))
    if not ok:
        raise OSError("Windows couldn't encrypt or decrypt the password")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def protect(password: str) -> str:
    """Protect a newly entered password; Linux requires a working desktop keyring."""
    if password and sys.platform.startswith("linux"):
        import linux_keyring
        return linux_keyring.store(password)
    if not password or password.startswith(PROTECTED) or os.name != "nt":
        return password
    try:
        return PROTECTED + base64.b64encode(_dpapi(password.encode("utf-8"), True)).decode("ascii")
    except Exception:
        return password


def reveal(stored: str) -> str:
    if stored and (sys.platform.startswith("linux") or stored.startswith("secret-service:")):
        import linux_keyring
        return linux_keyring.lookup(stored)
    if not stored or not stored.startswith(PROTECTED):
        return stored or ""
    try:
        return _dpapi(base64.b64decode(stored[len(PROTECTED):]), False).decode("utf-8")
    except Exception:
        return ""


# --------------------------------------------------------------------------- the API

class OpenSubtitles:
    def __init__(self, cfg: dict, version: str = "", resolve_password: bool = True):
        self.key = (cfg.get("opensubtitles_api_key") or APP_KEY or "").strip()
        self.user = (cfg.get("opensubtitles_username") or "").strip()
        self.credential_error = ""
        try:
            stored = cfg.get("opensubtitles_password") or ""
            self.password = reveal(stored) if resolve_password else ("saved" if stored else "")
        except ValueError as exc:
            self.password, self.credential_error = "", str(exc)
        self.agent = f"Shelfrip v{version or '1'}"
        self.base = API
        self.token = ""
        self.remaining: int | None = None

    def ready(self) -> str:
        """'' when it can be used, else why not, in plain words."""
        if self.credential_error:
            return self.credential_error
        if not self.key:
            return "no OpenSubtitles API key"
        if not self.user or not self.password:
            return "not signed in to OpenSubtitles (Settings > Names and lookups)"
        return ""

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        headers = {"Api-Key": self.key, "User-Agent": self.agent, "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        url = self.base + path
        if params:  # sorted and lower case, as the API asks (otherwise it redirects)
            url += "?" + urllib.parse.urlencode(sorted((k.lower(), str(v).lower()) for k, v in params.items()))
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read().decode("utf-8") or "{}")
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")[:300]
                if exc.code == 406 or (exc.code == 403 and "download" in path):
                    raise QuotaUsed(text)
                if exc.code == 401:
                    raise PermissionError("OpenSubtitles didn't accept the username, password or API key")
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise RuntimeError(f"OpenSubtitles said {exc.code}: {text}")
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                if attempt == 2:
                    raise
            time.sleep(1.5 * (attempt + 1))
        return {}

    def login(self) -> dict:
        r = self._call("POST", "/login", body={"username": self.user, "password": self.password})
        self.token = r.get("token") or ""
        if r.get("base_url"):          # VIP accounts get their own address
            self.base = f"https://{r['base_url']}/api/v1"
        self.remaining = (r.get("user") or {}).get("allowed_downloads")
        return r

    def find(self, tmdb_show: str, season: int, episode: int, lang: str) -> dict | None:
        """The best subtitle file for one episode: most downloaded, made by a person (not machine or
        AI translated), not hearing-impaired when there's a choice."""
        r = self._call("GET", "/subtitles", params={
            "parent_tmdb_id": tmdb_show, "season_number": season, "episode_number": episode,
            "languages": lang, "order_by": "download_count", "order_direction": "desc"})
        best, best_rank = None, None
        for d in r.get("data") or []:
            a = d.get("attributes") or {}
            fd = a.get("feature_details") or {}
            if fd.get("season_number") not in (None, season) or fd.get("episode_number") not in (None, episode):
                continue
            files = a.get("files") or []
            if not files or a.get("machine_translated") or a.get("ai_translated"):
                continue
            rank = (0 if a.get("from_trusted") else 1, 1 if a.get("hearing_impaired") else 0,
                    -int(a.get("download_count") or 0))
            if best_rank is None or rank < best_rank:
                best, best_rank = {"file_id": files[0]["file_id"], "name": files[0].get("file_name") or "",
                                   "release": a.get("release") or "", "downloads": a.get("download_count")}, rank
        return best

    def download(self, file_id: int) -> str:
        if not self.token:
            self.login()
        r = self._call("POST", "/download", body={"file_id": file_id, "sub_format": "srt"})
        if "remaining" in r:
            self.remaining = r["remaining"]
        link = r.get("link")
        if not link:
            raise QuotaUsed(r.get("message") or "no download link")
        with urllib.request.urlopen(urllib.request.Request(link, headers={"User-Agent": self.agent}),
                                    timeout=60) as f:
            raw = f.read()
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")


# --------------------------------------------------------------------------- the cache

def cache_dir() -> Path:
    import paths
    return paths.CACHE_DIR / "refsubs"


def cached(tmdb_show: str, season: int, episode: int, lang: str) -> Path:
    return cache_dir() / f"tv{int(tmdb_show)}" / lang / f"s{season:02d}e{episode:02d}.srt"


def missing_marker(p: Path) -> Path:
    return p.with_suffix(".none")


def wanted_episodes(planned: list[int], season_eps: list[int], clip_eps: set[int], spread: int = 2) -> list[int]:
    """Which episodes' subtitles a disc needs: the planned ones first, then their neighbours (a disc
    numbered a little off), then the season's clip shows."""
    out = []
    have = set(season_eps)
    for e in planned:
        if e in have and e not in out:
            out.append(e)
    for d in range(1, spread + 1):
        for e in planned:
            for x in (e - d, e + d):
                if x in have and x not in out:
                    out.append(x)
    out += [e for e in sorted(clip_eps) if e in have and e not in out]
    return out


def fetch(cfg: dict, tmdb_show: str, season: int, episodes: list[int], lang3: str, log=print,
          limit: int = 10, version: str = "") -> dict[int, str]:
    """Reference subtitles for these episodes, from the cache or OpenSubtitles: {episode: srt text}.
    Downloads at most `limit` new files per call and stops quietly when the day's quota is used.
    Episodes OpenSubtitles has nothing for are remembered for a week so they aren't searched again."""
    lang = LANG.get(lang3, "en")
    out: dict[int, str] = {}
    todo = []
    for e in episodes:
        p = cached(tmdb_show, season, e, lang)
        if p.is_file():
            try:
                out[e] = p.read_text(encoding="utf-8")
                continue
            except OSError:
                pass
        m = missing_marker(p)
        if m.is_file() and time.time() - m.stat().st_mtime < 7 * 86400:
            continue
        todo.append(e)
    if not todo:
        return out
    os_ = OpenSubtitles(cfg, version)
    why = os_.ready()
    if why:
        log(f"   reference subtitles: {why}")
        return out
    got = 0
    for e in todo:
        if got >= limit:
            log(f"   reference subtitles: stopped at {limit} downloads for this disc; the rest come next time")
            break
        p = cached(tmdb_show, season, e, lang)
        try:
            hit = os_.find(tmdb_show, season, e, lang)
            if not hit:
                p.parent.mkdir(parents=True, exist_ok=True)
                missing_marker(p).write_text("", encoding="utf-8")
                continue
            text = os_.download(hit["file_id"])
        except QuotaUsed:
            log("   reference subtitles: today's OpenSubtitles downloads are used up; the rest come tomorrow")
            break
        except PermissionError as exc:
            log(f"   reference subtitles: {exc}")
            break
        except Exception as exc:
            log(f"   reference subtitles for e{e:02d} skipped: {exc}")
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        out[e] = text
        got += 1
    if got:
        left = f" ({os_.remaining} left today)" if os_.remaining is not None else ""
        log(f"   reference subtitles: downloaded {got} from OpenSubtitles{left}")
    return out
