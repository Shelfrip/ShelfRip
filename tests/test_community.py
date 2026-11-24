"""
test_community.py - what gets sent to the shared disc database, and what's done with its answers.

Covers: track lists in submissions, "accepted" (filed just as the database's answer was shown),
confidence coming back from a lookup, and commentary names carried over from other people's filings.
No internet: the database's answer is made up.
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discs  # noqa: E402

TMP = discs.setup_env({"tmdb_api_key": "", "install_id": "00000001-aaaa-bbbb-cccc-dddddddddddd"})
import community  # noqa: E402
import identify  # noqa: E402

CFG = {"install_id": "00000001-aaaa-bbbb-cccc-dddddddddddd", "community_url": "http://db.test"}


def disc():
    streams = [
        {"type": "Video", "codec": "Mpeg2", "resolution": "720x480"},
        {"type": "Audio", "lang3": "eng", "codec": "DD", "channels": 6, "desc": "DD 5.1 English"},
        {"type": "Audio", "lang3": "eng", "codec": "DD", "channels": 2, "desc": "DD Stereo English"},
        {"type": "Subtitles", "lang3": "eng"},
        {"type": "Subtitles", "lang3": "fra", "flags": 4096},
    ]
    return {"content_hash": "AB" * 16, "name": "The Matrix", "titles": [
        {"output_file": "t00.mkv", "source_file": "00001.mpls", "duration_seconds": 8160, "size_bytes": 7_000_000_000,
         "chapters": 32, "streams": streams},
        {"output_file": "t01.mkv", "source_file": "00002.mpls", "duration_seconds": 1200, "size_bytes": 500_000_000,
         "chapters": 4, "streams": streams[:2]},
        {"output_file": "t02.mkv", "source_file": "00003.mpls", "duration_seconds": 30, "size_bytes": 5_000_000,
         "chapters": 1, "streams": streams[:2]},
    ]}


def plan():
    return {"kind": "movie", "title": "The Matrix", "year": "1999", "tmdb_id": "603", "disc_label": "MATRIX",
            "items": [
                {"source": "t00.mkv", "role": "main", "action": "move", "duration_seconds": 8160, "name": "The Matrix (1999)",
                 "audio": [{"i": 1, "commentary": False, "name": ""},
                           {"i": 2, "commentary": True, "name": "Commentary with the Cast"}]},
                {"source": "t01.mkv", "role": "extra", "action": "move", "duration_seconds": 1200,
                 "category": "Featurettes", "name": "Making Of"},
                {"source": "t02.mkv", "role": "extra", "action": "skip", "duration_seconds": 30,
                 "category": "Other", "name": "Unused clip (0:30)"},
            ]}


class Tracks(unittest.TestCase):
    def test_tracks_sent_for_kept_titles(self):
        body = community.submission(plan(), disc(), CFG)
        main, extra, clip = body["titles"]
        self.assertEqual(main["tracks"]["video"], {"codec": "Mpeg2", "res": "720x480"})
        self.assertEqual(main["tracks"]["audio"], [
            {"lang": "eng", "codec": "DD", "ch": 6},
            {"lang": "eng", "codec": "DD", "ch": 2, "commentary": 1, "name": "Commentary with the Cast"}])
        self.assertEqual(main["tracks"]["subs"], [{"lang": "eng"}, {"lang": "fra", "forced": 1}])
        self.assertIn("tracks", extra)
        self.assertNotIn("tracks", clip)  # a skipped clip isn't worth the bytes

    def test_generic_commentary_name_not_sent(self):
        p = plan()
        p["items"][0]["audio"][1]["name"] = "Commentary 2"
        a = community.submission(p, disc(), CFG)["titles"][0]["tracks"]["audio"][1]
        self.assertEqual(a, {"lang": "eng", "codec": "DD", "ch": 2, "commentary": 1})

    def test_no_accepted_without_a_shared_answer(self):
        body = community.submission(plan(), disc(), CFG)
        self.assertFalse(any(t.get("accepted") for t in body["titles"]))


class Accepted(unittest.TestCase):
    def shown_plan(self):
        p = plan()
        p["community_answer"] = {"kind": "movie", "tmdb_id": "603", "season": None, "title": "The Matrix"}
        community.note_shown(p, disc())
        return p

    def test_filed_unchanged_is_accepted(self):
        body = community.submission(self.shown_plan(), disc(), CFG)
        self.assertTrue(all(t.get("accepted") for t in body["titles"]))

    def test_changed_title_is_not(self):
        p = self.shown_plan()
        p["items"][1]["name"] = "The Making of The Matrix"
        p["items"][2]["action"] = "move"
        accepted = [bool(t.get("accepted")) for t in community.submission(p, disc(), CFG)["titles"]]
        self.assertEqual(accepted, [True, False, False])

    def test_rematched_disc_is_not(self):
        p = self.shown_plan()
        p["tmdb_id"] = "604"
        self.assertFalse(any(t.get("accepted") for t in community.submission(p, disc(), CFG)["titles"]))

    def test_commentary_edits_dont_count_as_title_changes(self):
        p = self.shown_plan()
        p["items"][0]["audio"][1]["name"] = "Commentary with the Writers"
        self.assertTrue(community.submission(p, disc(), CFG)["titles"][0].get("accepted"))


class Lookup(unittest.TestCase):
    ANSWER = {"found": True, "kind": "movie", "title": "The Matrix", "year": "1999", "tmdb_id": "603",
              "submitters": 2, "agree": 2, "confirmed": False, "confidence": 65, "titles": [
                  {"source_file": "00001.mpls", "duration_s": 8160, "size_bytes": 7_000_000_000, "role": "main",
                   "kept": 1, "votes": 2, "weight": 1.5, "confidence": 65,
                   "tracks": {"audio": [{"lang": "eng", "codec": "DD", "ch": 6},
                                        {"lang": "eng", "codec": "DD", "ch": 2, "commentary": 1,
                                         "name": "Commentary with the Cast"}]}},
                  {"source_file": "00002.mpls", "duration_s": 1200, "size_bytes": 500_000_000, "role": "extra",
                   "kept": 1, "category": "Featurettes", "name": "Making Of", "votes": 2, "confidence": 65}]}

    def lookup(self, answer):
        real = community._http
        community._http = lambda url, body=None: copy.deepcopy(answer)
        try:
            d = disc()
            titles = [dict(t) for t in d["titles"]]
            return community.lookup(d, titles, CFG)
        finally:
            community._http = real

    def test_confidence_comes_through(self):
        known = self.lookup(self.ANSWER)
        self.assertEqual(known["confidence"], 65)
        self.assertEqual(known["pairs"]["t00.mkv"]["confidence"], 65)
        self.assertEqual(known["pairs"]["t00.mkv"]["tracks"]["audio"][1]["name"], "Commentary with the Cast")

    def test_older_server_without_confidence(self):
        ans = copy.deepcopy(self.ANSWER)
        ans.pop("confidence")
        for t in ans["titles"]:
            t.pop("confidence"); t.pop("tracks", None)
        known = self.lookup(ans)
        self.assertIsNone(known["pairs"]["t00.mkv"]["confidence"])

    def test_broken_answers_are_ignored(self):
        for bad in ({"tmdb_id": "603; drop"}, {"kind": "music"}, {"season": "one"}):
            self.assertIsNone(self.lookup({**self.ANSWER, **bad}), bad)
        ans = copy.deepcopy(self.ANSWER)
        ans["titles"][0]["duration_s"] = "long"   # wrong type from the server: planned without it
        ans["titles"][0]["size_bytes"] = None
        self.assertIsNone(self.lookup(ans))
        ans = copy.deepcopy(self.ANSWER)
        ans["confidence"], ans["titles"][0]["confidence"] = 250, "high"
        known = self.lookup(ans)
        self.assertEqual(known["confidence"], 100)
        self.assertIsNone(known["pairs"]["t00.mkv"]["confidence"])

    def test_commentary_names_carried_over(self):
        p = plan()
        for it in p["items"]:
            it.pop("audio", None)
        p["items"][0]["shared_audio"] = self.ANSWER["titles"][0]["tracks"]["audio"]
        identify.attach_audio(p, TMP, disc())
        a = p["items"][0]["audio"]
        self.assertTrue(a[1]["commentary"])
        self.assertEqual(a[1]["name"], "Commentary with the Cast")
        self.assertNotIn("shared_audio", p["items"][0])

    def test_names_not_carried_to_different_tracks(self):
        p = plan()
        for it in p["items"]:
            it.pop("audio", None)
        p["items"][0]["shared_audio"] = [{"lang": "eng", "ch": 6}, {"lang": "spa", "ch": 2, "commentary": 1, "name": "X"}]
        identify.attach_audio(p, TMP, disc())
        self.assertFalse(p["items"][0]["audio"][1]["commentary"])


if __name__ == "__main__":
    unittest.main()
