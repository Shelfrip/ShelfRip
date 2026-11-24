"""
test_library_dirs.py - the Movies and TV Shows folders inside the media library: chosen in
Settings, or found whatever their case or spacing ("movies", "TVShows"), else the defaults.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discs  # noqa: E402

TMP = discs.setup_env({"tmdb_api_key": ""})
import identify  # noqa: E402


def lib(*folders: str) -> Path:
    root = Path(tempfile.mkdtemp(dir=TMP))
    for f in folders:
        (root / f).mkdir(parents=True)
    return root


def dirs(root: Path, **cfg) -> dict:
    return identify.library_dirs({"plex_library_root": str(root), **cfg})


class LibraryDirs(unittest.TestCase):
    def test_defaults_for_a_new_library(self):
        self.assertEqual(dirs(lib()), {"movies": "Movies", "tv": "TV Shows"})
        self.assertEqual(identify.library_dirs({"plex_library_root": ""}), {"movies": "Movies", "tv": "TV Shows"})

    def test_existing_folders_any_case_or_spacing(self):
        self.assertEqual(dirs(lib("movies", "TVShows")), {"movies": "movies", "tv": "TVShows"})
        self.assertEqual(dirs(lib("Films", "tv_shows", "Music")), {"movies": "Films", "tv": "tv_shows"})
        self.assertEqual(dirs(lib("Movies", "TV Shows", "TV")), {"movies": "Movies", "tv": "TV Shows"})

    def test_chosen_folder_wins_and_matches_case_on_disk(self):
        root = lib("Movies", "Kids Movies", "Series")
        self.assertEqual(dirs(root, movies_folder="kids movies", tv_folder="")["movies"], "Kids Movies")
        self.assertEqual(dirs(root, tv_folder="Video\\Shows")["tv"], "Video\\Shows")
        self.assertEqual(dirs(root)["tv"], "Series")

    def test_clean_lib_dir(self):
        c = identify.clean_lib_dir
        self.assertEqual(c("movies", ""), "movies")
        self.assertEqual(c(" Video/Films/ ", ""), "Video\\Films")
        self.assertEqual(c(r"n:\media library\TVShows", r"N:\Media Library"), "TVShows")
        self.assertEqual(c(r"\\NAS\Media\Movies", r"\\nas\media"), "Movies")
        self.assertEqual(c(r"d:/Elsewhere//Movies\\", r"N:\Media Library"), r"d:\Elsewhere\Movies")  # kept elsewhere
        self.assertEqual(c(r"\\NAS\TV", r"N:\Media Library"), r"\\NAS\TV")
        self.assertEqual(c(r"N:\Media Library", r"N:\Media Library"), r"N:\Media Library")
        self.assertEqual(c(r"..\Movies", r"N:\Media Library"), "")
        self.assertEqual(c("", r"N:\Media Library"), "")

    def test_index_library_reads_the_chosen_folders(self):
        root = lib("movies/Some Film (2001) {tmdb-1}", "TVShows/Some Show (2002) {tmdb-2}")
        got = identify.index_library(root, {**identify.DEFAULTS, "plex_library_root": str(root)})
        self.assertEqual([m["title"] for m in got["movies"]], ["Some Film"])
        self.assertEqual([s["title"] for s in got["shows"]], ["Some Show"])

    def test_folders_somewhere_else(self):
        root, other = lib("Movies"), lib("Films/Some Film (2001) {tmdb-1}")
        tops = dirs(root, movies_folder=str(other / "Films"))
        self.assertEqual(tops["movies"], str(other / "Films"))
        self.assertEqual(tops["tv"], "TV Shows")
        self.assertTrue(identify.is_full_path(tops["movies"]))
        self.assertEqual(identify.lib_path(root, tops["movies"] + "\\X: Y"), other / "Films" / "X - Y")
        self.assertEqual(identify.lib_path(root, "Movies\\X"), root / "Movies" / "X")
        got = identify.index_library(root, {**identify.DEFAULTS, "plex_library_root": str(root),
                                            "movies_folder": str(other / "Films")})
        self.assertEqual([m["title"] for m in got["movies"]], ["Some Film"])

    def test_music_folder(self):
        import music
        self.assertEqual(music.music_root({"plex_library_root": str(lib())}).name, "Music")
        self.assertEqual(music.music_root({"plex_library_root": str(lib("music"))}).name, "music")
        self.assertEqual(str(music.music_root({"plex_library_root": str(lib()), "music_library_root": r"M:\Audio"})), r"M:\Audio")

    def test_rip_folder_inside_library(self):
        import app
        root = lib("Movies")
        cfg = {"plex_library_root": str(root)}
        self.assertEqual(app.rip_folder_in_library(str(root / "Ripped"), cfg), str(root))
        self.assertEqual(app.rip_folder_in_library(str(root), cfg), str(root))
        self.assertEqual(app.rip_folder_in_library(str(root.parent / "Ripped"), cfg), "")
        self.assertEqual(app.rip_folder_in_library(str(root) + "-rips", cfg), "")  # same prefix, different folder
        self.assertEqual(app.rip_folder_in_library(str(root / "x"), {}), "")


if __name__ == "__main__":
    unittest.main()
