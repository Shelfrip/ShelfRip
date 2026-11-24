r"""
naming.py - how files are named for each media server.

Ripping, identifying and the review page work the same whatever server you use; only the
names and folders that filing writes differ. Pick the server in Settings (media_server).

              folder id tag     another cut of a film          extras
  Plex        {tmdb-603}        Title (1999) {edition-Extended}   Featurettes\, Deleted Scenes\, ...
  Jellyfin    [tmdbid-603]      Title (1999) - Extended           same folders as Plex
  Emby        [tmdbid=603]      Title (1999) - Extended           same folders as Plex
  Kodi        (none)            Title (1999) - Extended           Extras\ (one folder for all)

Every server reads all the tag styles when looking through an existing library, so a
library that mixes them (or was made for another server) is still recognised.
"""

from __future__ import annotations

import re

SERVERS = {"plex": "Plex", "jellyfin": "Jellyfin", "emby": "Emby", "kodi": "Kodi"}

# Episode extras named after their episode ("<episode file>-featurette.mkv"): Plex, Jellyfin and Emby.
EXTRA_SUFFIX = {"Behind the Scenes": "behindthescenes", "Deleted Scenes": "deleted", "Featurettes": "featurette",
                "Interviews": "interview", "Scenes": "scene", "Shorts": "short", "Trailers": "trailer", "Other": "other"}

# Any of the id tag styles, in a library folder name.
TAG_PATTERN = (r"(?:\{(?P<src>tmdb|imdb|tvdb)-(?P<id>[^}]+)\}"
               r"|\[(?P<src2>tmdb|imdb|tvdb)id[-=](?P<id2>[^\]]+)\])")
EDITION_RE = re.compile(r"\{edition-(?P<a>[^}]+)\}|\)(?:\s*(?:\{[^}]*\}|\[[^\]]*\]))*\s+-\s+(?P<b>[^\\/]+?)$")


def server(cfg: dict) -> str:
    s = str(cfg.get("media_server") or "plex").lower()
    return s if s in SERVERS else "plex"


def server_name(cfg: dict) -> str:
    return SERVERS[server(cfg)]


def id_tag(cfg: dict, tid: str, src: str = "tmdb") -> str:
    """The id tag for a library folder or file name, or '' (no id, or Kodi)."""
    if not tid:
        return ""
    s = server(cfg)
    if s == "plex":
        return f"{{{src}-{tid}}}"
    if s == "jellyfin":
        return f"[{src}id-{tid}]"
    if s == "emby":
        return f"[{src}id={tid}]"
    return ""


def edition_name(cfg: dict, base: str, edition: str) -> str:
    """The file name (no extension) of one cut of a film."""
    if server(cfg) == "plex":
        return f"{base} {{edition-{edition}}}"
    return f"{base} - {edition}"


def edition_of(stem: str) -> str:
    """The edition a library file name says it is ('' for the main version)."""
    m = EDITION_RE.search(stem)
    if not m:
        return ""
    return (m.group("a") or m.group("b") or "").strip()


def extras_suffixed(cfg: dict) -> bool:
    """Special features named 'Making Of-featurette.mkv' next to the film (or in the season folder)
    instead of filed in a Featurettes folder. Plex, Jellyfin and Emby read both; Kodi only reads
    its Extras folder."""
    return server(cfg) != "kodi" and str(cfg.get("extras_style") or "folders").lower() == "suffix"


def extras_folder(cfg: dict, category: str) -> str:
    return "Extras" if server(cfg) == "kodi" else (category or "Other")


def episode_extras_inline(cfg: dict) -> bool:
    """Can an extra sit next to its episode as '<episode>-featurette.mkv'?"""
    return server(cfg) != "kodi"


def page_info(cfg: dict) -> dict:
    """What the review page needs to build names the same way."""
    return {"server": server(cfg), "server_name": server_name(cfg)}
