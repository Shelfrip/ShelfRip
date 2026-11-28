"""
test_next_part.py - a box-set part that runs past the end of TMDB's season carries on after the
episodes of the next season already in the library (Fairy Tail Part 6 Disc 1, 2026-11-27: Part 5
was filed as s02e01-e12, then Part 6 was numbered from s02e01 again and every episode was set to
Skip as "already in Plex").
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discs  # noqa: E402

TMP = discs.setup_env({"tmdb_api_key": "", "use_tvmaze": False})
import identify  # noqa: E402


class FakeTMDB:
    """Season 1 has 14 episodes, season 2 has 20, 25 minutes each."""
    SEASONS = {1: 14, 2: 20}

    def search(self, kind, q):
        return [{"id": 7, "name": "Partshow", "first_air_date": "2009-01-01"}]

    def tv(self, tid):
        return {"id": 7, "name": "Partshow", "first_air_date": "2009-01-01",
                "seasons": [{"season_number": n, "episode_count": c} for n, c in self.SEASONS.items()]}

    def season(self, tid, n):
        return {"episodes": [{"episode_number": e, "name": f"S{n} Ep {e}", "runtime": 25}
                             for e in range(1, self.SEASONS.get(n, 0) + 1)]}


def file_episodes(season, numbers):
    cfg = identify.load_config()
    show = identify.lib_path(TMP / "lib", identify.library_dirs(cfg, TMP / "lib")["tv"]) / "Partshow (2009) {tmdb-7}"
    folder = show / f"Season {season}"
    folder.mkdir(parents=True, exist_ok=True)
    for e in numbers:
        (folder / f"Partshow (2009) - s{season:02d}e{e:02d}.mkv").write_bytes(b"")


def plan(label, n=8):
    titles = [{"title_id": i, "output_file": f"t{i:02d}.mkv", "duration_seconds": 25 * 60 + i,
               "size_bytes": 1_000_000_000, "chapters": 6, "streams": []} for i in range(n)]
    folder = TMP / "rip" / f"{label}__1"
    folder.mkdir(parents=True, exist_ok=True)
    cfg = identify.load_config()
    lib = identify.index_library(TMP / "lib", cfg)
    warnings = []
    p = identify.plan_tv(folder, TMP / "rip", {"titles": titles, "disc_label": label}, titles,
                         identify.parse_label(label), FakeTMDB(), lib, None, cfg, warnings)
    return [(it["season"], it["episode"]) for it in p["items"] if it["role"] == "episode"]


class NextPart(unittest.TestCase):
    def test_continues_after_filed_episodes_of_next_season(self):
        file_episodes(1, range(1, 15))
        file_episodes(2, range(1, 13))
        self.assertEqual(plan("PARTSHOW_P6_D1"), [(2, e) for e in range(13, 21)])


if __name__ == "__main__":
    unittest.main()
