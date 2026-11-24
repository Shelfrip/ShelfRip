"""
test_dupes.py - leaving out extra copies of the main feature before ripping (dupes.py), with no
internet and no MakeMKV: the disc scan and the rip are faked.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discs  # noqa: E402

TMP = discs.setup_env({"tmdb_api_key": "", "use_community": False, "use_thediscdb": False})
import dupes  # noqa: E402
import identify  # noqa: E402
import rip_all  # noqa: E402

ENG51 = {"type": "Audio", "lang3": "eng", "codec": "DTS-HD MA", "channels": 6}
FRA51 = {"type": "Audio", "lang3": "fra", "codec": "DTS", "channels": 6}
COMM = {"type": "Audio", "lang3": "eng", "codec": "AC3", "channels": 2, "name": "Commentary"}
SUB = {"type": "Subtitles", "lang3": "eng", "codec": "PGS"}
VIDEO = {"type": "Video", "codec": "MPEG4-AVC", "resolution": "1920x1080"}


def title(tid, secs, segs="", size=30_000_000_000, streams=None, chapters=24, src=""):
    return {"title_id": tid, "duration_seconds": secs, "segments": segs, "size_bytes": size, "chapters": chapters,
            "source_file": src or f"{800 + tid:05d}.mpls", "output_file": f"title_t{tid:02d}.mkv",
            "streams": [VIDEO] + (streams if streams is not None else [ENG51, SUB])}


def main_row(size, src=""):
    return {"itemType": "MainMovie", "size": size, "sourceFile": src}


class SameContent(unittest.TestCase):
    def test_identical_copies_keep_one(self):
        ts = [title(0, 7200, "1,2,3"), title(1, 7200, "1,2,3"), title(2, 300, "9")]
        self.assertEqual(dupes.find(ts), {1: (0, "same clips and tracks")})

    def test_keeps_the_copy_with_more_tracks(self):
        ts = [title(0, 7200, "1,2,3"), title(1, 7200, "1,2,3", streams=[ENG51, COMM, SUB])]
        self.assertEqual(set(dupes.find(ts)), {0})

    def test_different_tracks_are_both_kept(self):
        # same picture, but each has a soundtrack the other lacks: never drop a track
        ts = [title(0, 7200, "1,2,3", streams=[ENG51]), title(1, 7200, "1,2,3", streams=[FRA51])]
        self.assertEqual(dupes.find(ts), {})

    def test_language_version_with_other_clips_is_kept(self):
        ts = [title(0, 7200, "1,2,3,4"), title(1, 7201, "1,2,3,5")]
        self.assertEqual(dupes.find(ts), {})

    def test_different_cuts_are_kept(self):
        ts = [title(0, 7200, "1,2,3"), title(1, 8100, "1,2,3,4")]
        self.assertEqual(dupes.find(ts), {})

    def test_short_titles_are_left_alone(self):
        ts = [title(0, 1300, "1"), title(1, 1300, "1")]
        self.assertEqual(dupes.find(ts), {})


class Decoys(unittest.TestCase):
    def decoys(self, sizes):
        return [title(i, 7380 + i % 2, ",".join(str((i + k) % 40) for k in range(40)), size=s)
                for i, s in enumerate(sizes)]

    def test_database_pick_drops_unknown_decoys(self):
        ts = self.decoys([30_000_000_000, 30_000_000_123, 30_000_000_456])
        known = {"kind": "movie", "pairs": {ts[1]["output_file"]: main_row(30_000_000_123)}}
        self.assertEqual(set(dupes.find(ts, known)), {0, 2})

    def test_ambiguous_database_pick_keeps_everything(self):
        ts = self.decoys([30_000_000_000] * 3)  # same size: the database's pick could be any of them
        known = {"kind": "movie", "pairs": {ts[0]["output_file"]: main_row(30_000_000_000)}}
        self.assertEqual(dupes.find(ts, known), {})

    def test_playlist_name_settles_same_size(self):
        ts = self.decoys([30_000_000_000] * 3)
        known = {"kind": "movie", "pairs": {ts[2]["output_file"]: main_row(30_000_000_000, ts[2]["source_file"])}}
        self.assertEqual(set(dupes.find(ts, known)), {0, 1})

    def test_two_known_features_are_both_kept(self):
        ts = self.decoys([30_000_000_000, 30_000_000_123])
        known = {"kind": "movie", "pairs": {t["output_file"]: main_row(t["size_bytes"]) for t in ts}}
        self.assertEqual(dupes.find(ts, known), {})

    def test_no_database_keeps_everything(self):
        self.assertEqual(dupes.find(self.decoys([30_000_000_000, 30_000_000_123])), {})


def dvd(tid, secs, chapters=1, streams=None):
    t = title(tid, secs, "1", chapters=chapters, streams=streams)
    t["source_file"] = ""
    return t


DVD_TRACKS = [ENG51, FRA51, COMM, SUB]


class Scenes(unittest.TestCase):
    """Discs that list every scene of the movie as its own title."""

    def movie(self):  # a seamless-branching Blu-ray feature: 30 clips, 16 chapters
        return title(0, 6900, ",".join(str(i) for i in range(1, 31)), chapters=16, src="00400.mpls")

    def clip(self, tid, seg, secs=120):
        return title(tid, secs, str(seg), src=f"{seg:05d}.m2ts")

    def test_bluray_scene_clips_are_found(self):
        ts = [self.movie()] + [self.clip(10 + i, i) for i in range(1, 8)] + [self.clip(30, 77)]
        found = dupes.scene_pieces(ts, [ts[0]])
        self.assertEqual(set(found), {11, 12, 13, 14, 15, 16, 17})  # clip 77 isn't in the movie
        self.assertEqual(found[11][0], 0)

    def test_two_scene_clips_are_not_enough(self):
        ts = [self.movie(), self.clip(11, 1), self.clip(12, 2)]
        self.assertEqual(dupes.scene_pieces(ts, [ts[0]]), {})

    def test_play_all_of_featurettes_keeps_its_parts(self):
        # a 2-hour documentary made of 8 featurettes, one per chapter: the parts are the extras
        doc = title(4, 8000, ",".join(str(i) for i in range(38, 46)), chapters=8, src="00039.mpls")
        ts = [doc] + [self.clip(20 + i, 38 + i, 600) for i in range(8)]
        self.assertEqual(dupes.scene_pieces(ts, [doc]), {})
        # unless a database or TMDB says the long one is the movie
        self.assertEqual(len(dupes.scene_pieces(ts, [doc], trusted={4})), 8)

    def test_long_parts_are_not_scenes(self):
        ts = [self.movie()] + [self.clip(10 + i, i, 20 * 60) for i in range(1, 5)]
        self.assertEqual(dupes.scene_pieces(ts, [ts[0]]), {})

    def test_named_titles_are_kept(self):
        ts = [self.movie()] + [self.clip(10 + i, i) for i in range(1, 6)]
        self.assertNotIn(12, dupes.scene_pieces(ts, [ts[0]], keep={12}))

    def test_dvd_scenes_adding_up_to_the_feature(self):
        f = dvd(0, 6000, chapters=10)
        ts = [f] + [dvd(1 + i, 600) for i in range(10)] + [dvd(20, 300)]
        self.assertEqual(set(dupes.scene_pieces(ts, [f])), set(range(1, 11)))

    def test_dvd_extras_that_dont_add_up_are_kept(self):
        f = dvd(0, 6000, chapters=10)
        ts = [f] + [dvd(1 + i, 500) for i in range(10)]
        self.assertEqual(dupes.scene_pieces(ts, [f]), {})

    def test_dvd_scenes_with_the_features_tracks(self):
        # Fast Times at Ridgemont High: 29 short titles with the feature's dub, commentary and subtitles
        f = dvd(0, 5350, chapters=18, streams=DVD_TRACKS)
        ts = [f] + [dvd(1 + i, 70, streams=DVD_TRACKS) for i in range(6)] + [dvd(30, 100, streams=[ENG51])]
        self.assertEqual(set(dupes.scene_pieces(ts, [f])), set(range(1, 7)))

    def test_dvd_same_tracks_needs_enough_tracks_and_titles(self):
        f = dvd(0, 5350, chapters=18, streams=[ENG51])
        ts = [f] + [dvd(1 + i, 70, streams=[ENG51]) for i in range(6)]
        self.assertEqual(dupes.scene_pieces(ts, [f]), {})
        f = dvd(0, 5350, chapters=18, streams=DVD_TRACKS)
        ts = [f] + [dvd(1 + i, 70, streams=DVD_TRACKS) for i in range(4)]
        self.assertEqual(dupes.scene_pieces(ts, [f]), {})

    def test_tv_disc_is_left_alone(self):
        f = dvd(0, 6000, chapters=10)
        disc = {"disc_label": "SHOW_SEASON_2_DISC_1", "titles": [f] + [dvd(1 + i, 600) for i in range(10)]}
        self.assertEqual(dupes.find_scenes(disc, None), {})
        disc["disc_label"] = "SOME_MOVIE"
        self.assertEqual(len(dupes.find_scenes(disc, None)), 10)

    def test_database_names_keep_a_title(self):
        ts = [self.movie()] + [self.clip(10 + i, i) for i in range(1, 6)]
        known = {"kind": "movie", "pairs": {ts[2]["output_file"]: {"itemType": "DeletedScene"}}}
        found = dupes.find_scenes({"disc_label": "MOVIE", "titles": ts}, known)
        self.assertEqual(set(found), {11, 13, 14, 15})


class ReviewScenes(unittest.TestCase):
    def test_unnamed_scenes_set_to_skip(self):
        import identify
        f = dvd(0, 5350, chapters=18, streams=DVD_TRACKS)
        ts = [f] + [dvd(1 + i, 70, streams=DVD_TRACKS) for i in range(6)] + [dvd(9, 100, streams=[ENG51])]
        plan = {"kind": "movie", "items": [
            {"source": f["output_file"], "role": "main", "action": "move", "name": "Movie (1982)"}] + [
            {"source": t["output_file"], "role": "extra", "action": "move",
             "name": "We Got The Beat" if t["title_id"] == 1 else f"Extra {t['title_id']:02d} (1m10s)"} for t in ts[1:]]}
        identify.skip_scenes(plan, ts)
        acts = {it["source"]: it["action"] for it in plan["items"]}
        self.assertEqual(acts[ts[1]["output_file"]], "move")   # has a real name: kept
        self.assertEqual([acts[t["output_file"]] for t in ts[2:7]], ["skip"] * 5)
        self.assertEqual(acts[ts[7]["output_file"]], "move")   # different tracks: a real extra
        self.assertIn("a scene from Movie (1982)", plan["items"][2]["note"])

    def test_tv_plans_untouched(self):
        import identify
        plan = {"kind": "tv", "items": []}
        identify.skip_scenes(plan, [])
        self.assertEqual(plan["items"], [])


class RipDisc(unittest.TestCase):
    """rip_disc rips only the titles that are left, one by one, and records what it left out."""

    def run_rip(self, setting, disc=None):
        disc = disc or {"titles": [title(0, 7200, "1,2,3"), title(1, 7200, "1,2,3"), title(2, 600, "7")]}
        calls = []

        def fake(cfg, args, logfile=None, on_progress=None, device="", on_phase=None):
            calls.append(args)
            if args[1] == "info":
                return 0, "info"
            out = Path(args[-1])
            ids = [t["title_id"] for t in disc["titles"]] if args[3] == "all" else [int(args[3])]
            for t in disc["titles"]:
                if t["title_id"] in ids:
                    (out / t["output_file"]).write_bytes(b"x")
            return 0, ""

        cfg = {**rip_all.DEFAULT_CONFIG, "output_root": str(TMP / "rip"), "skip_duplicate_features": setting, "eject_when_done": False,
               "retry_failed_titles": False, "use_community": False, "use_thediscdb": False}
        rip_all.run_makemkv, old_run = fake, rip_all.run_makemkv
        rip_all.parse_disc_info, old_parse = (lambda _: json.loads(json.dumps(disc))), rip_all.parse_disc_info
        try:
            info = rip_all.rip_disc(cfg, {"device": "disc:9", "disc_label": f"DUPES_{setting}_{len(disc['titles'])}", "drive_name": "x", "index": 9})
        finally:
            rip_all.run_makemkv, rip_all.parse_disc_info = old_run, old_parse
        return info, [c for c in calls if c[1] == "mkv"]

    def test_setting_on(self):
        info, rips = self.run_rip(True)
        self.assertEqual([c[3] for c in rips], ["0", "2"])
        self.assertEqual(info["status"], "ok")
        self.assertEqual(info["skipped_duplicates"], 1)
        t1 = info["titles"][1]
        self.assertEqual((t1["skipped_duplicate_of"], t1["ripped"]), (0, False))

    def test_scenes_left_out(self):
        disc = {"titles": [title(0, 6900, ",".join(str(i) for i in range(1, 31)), chapters=16)]
                + [{**title(i, 120, str(i), src=f"{i:05d}.m2ts")} for i in range(1, 5)]}
        info, rips = self.run_rip(True, disc)
        self.assertEqual([c[3] for c in rips], ["0"])
        self.assertEqual(info["skipped_scenes"], 4)
        self.assertNotIn("skipped_duplicates", info)
        self.assertEqual(info["titles"][1]["skip_kind"], "scene")

    def test_many_titles_small_scenes_one_pass(self):
        # ten extras and a few small scene titles: one pass, then the scene titles are deleted
        disc = {"titles": [title(0, 6900, ",".join(str(i) for i in range(1, 31)), chapters=16)]
                + [{**title(i, 120, str(i), size=100_000_000, src=f"{i:05d}.m2ts")} for i in range(1, 5)]
                + [title(10 + i, 600, str(100 + i), size=500_000_000) for i in range(10)]}
        from collections import namedtuple
        usage, old = namedtuple("u", "total used free")(10**13, 0, 10**13), rip_all.shutil.disk_usage
        rip_all.shutil.disk_usage = lambda _: usage
        try:
            info, rips = self.run_rip(True, disc)
        finally:
            rip_all.shutil.disk_usage = old
        self.assertEqual([c[3] for c in rips], ["all"])
        self.assertEqual(info["skipped_scenes"], 4)
        self.assertEqual([t["ripped"] for t in info["titles"][1:5]], [False] * 4)  # deleted afterwards
        self.assertTrue(all(t["ripped"] for t in info["titles"][5:]) and info["titles"][0]["ripped"])
        self.assertEqual(info["status"], "ok")

    def test_setting_off(self):
        info, rips = self.run_rip(False)
        self.assertEqual([c[3] for c in rips], ["all"])
        self.assertNotIn("skipped_duplicates", info)

    def test_drive_lost_is_not_retried(self):
        # a USB drive dropped off: MakeMKV can't open it, nothing is saved, and retrying can't help
        disc = {"titles": [title(0, 3000, "1"), title(1, 3000, "2")]}
        calls = []
        lost = ('MSG:2024,0,1,"Unknown device - \'J:\'","Unknown device - \'%1\'","J:"\n'
                'MSG:5010,0,0,"Failed to open disc","Failed to open disc"')

        def fake(cfg, args, logfile=None, on_progress=None, device="", on_phase=None):
            calls.append(args)
            return (0, "info") if args[1] == "info" else (1, lost)

        cfg = {**rip_all.DEFAULT_CONFIG, "output_root": str(TMP / "rip"), "skip_duplicate_features": False,
               "eject_when_done": False, "retry_failed_titles": True, "use_community": False, "use_thediscdb": False}
        rip_all.run_makemkv, old_run = fake, rip_all.run_makemkv
        rip_all.parse_disc_info, old_parse = (lambda _: json.loads(json.dumps(disc))), rip_all.parse_disc_info
        try:
            info = rip_all.rip_disc(cfg, {"device": "J:", "disc_label": "LOST_DRIVE", "drive_name": "x", "index": 2})
        finally:
            rip_all.run_makemkv, rip_all.parse_disc_info = old_run, old_parse
        self.assertEqual(len([c for c in calls if c[1] == "mkv"]), 1)  # no retries
        self.assertTrue(info.get("drive_lost"))
        self.assertEqual(info["status"], "check")
        self.assertIn("disconnected", info["makemkv_error"])

    def test_authored_playlist_names(self):
        d = {"disc_label": "SPECIAL_FEATURES", "name": "Special Features"}
        name = lambda f: identify.authored_name({"output_file": f}, d)
        self.assertEqual(name("Special Features-SEG_01_BTS_08_BattleReady_t12.mkv"), ("Battle Ready", "Behind the Scenes"))
        self.assertEqual(name("Special Features-SF_02_Trls_02_IMAX_NCR_t18.mkv"), ("IMAX Trailer", "Trailers"))
        self.assertEqual(name("Special Features-SF_03_Extras_01_MusicVideo_t22.mkv"), ("Music Video", "Other"))
        self.assertEqual(name("Special Features-SF_01_BTS_00_PlayAll_t25.mkv")[0], "Play All")
        self.assertEqual(name("Special Features-Uni_ScreenSaver_t02.mkv")[0], "Screen Saver")
        self.assertIsNone(name("SPECIAL_FEATURES_t03.mkv"))  # MakeMKV's usual disc-name file: nothing to read
        self.assertIsNone(identify.authored_name({"output_file": "THE_MATRIX_t01.mkv"}, {"disc_label": "MATRIX"}))
        # used for unnamed extras, and the menu / screensaver are then skipped as clutter
        t = {"output_file": "Special Features-Uni_ScreenSaver_t02.mkv", "duration_seconds": 300, "duration": "0:05:00", "title_id": 2}
        it = identify.extra_item(t, 1, "x", {**identify.DEFAULTS, "skip_extras_under_seconds": 30}, d)
        self.assertEqual(it["name"], "Screen Saver")
        identify.skip_clutter([it])
        self.assertEqual(it["action"], "skip")

    def test_repeated_playlist_names_are_numbered(self):
        mk = lambda n: {"role": "extra", "action": "move", "dest_dir": "x", "category": "Featurettes", "name": n}
        items = [mk("Ithaca"), mk("Ithaca"), mk("Battle Ready"), {**mk("Ithaca"), "action": "skip"}]
        identify.number_repeats(items)
        self.assertEqual([i["name"] for i in items], ["Ithaca (Part 1)", "Ithaca (Part 2)", "Battle Ready", "Ithaca"])

    def test_rip_that_saved_nothing_is_not_already_ripped(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as root:
            d = Path(root) / "GOT_S2__20261124-145526"
            d.mkdir()
            (d / "disc_info.json").write_text(json.dumps({"titles_saved": 0}))
            self.assertEqual(rip_all.already_ripped({"output_root": root}, "GOT_S2"), "")
            (d / "t00.mkv").write_bytes(b"x")
            self.assertEqual(rip_all.already_ripped({"output_root": root}, "GOT_S2"), d.name)

    def test_eject_ignores_anything_but_a_drive_letter(self):
        calls = []
        real = rip_all.subprocess.run
        rip_all.subprocess.run = lambda *a, **k: calls.append(a[0])
        try:
            rip_all.eject("E:'); Remove-Item C:\\x; ('")
            rip_all.eject("e:")
        finally:
            rip_all.subprocess.run = real
        self.assertEqual(len(calls), 1)
        self.assertIn("ParseName('E:')", calls[0][-1])

    def test_skipped_titles_still_say_what_they_are(self):
        mk = lambda n, d: {"role": "extra", "action": "move", "dest_dir": "x", "category": "Other",
                           "name": f"Extra {n:02d} ({identify.mmss(d)})", "duration_seconds": d}
        items = [mk(1, 17), mk(2, 200), mk(3, 150), mk(4, 160), mk(5, 510)]  # 5 plays 2, 3 and 4
        identify.skip_clutter(items, "Some Film")
        self.assertEqual([i["action"] for i in items], ["skip", "move", "move", "move", "skip"])
        self.assertTrue(items[0]["name"].startswith("Probably a Logo, Warning or Menu ("))
        self.assertTrue(items[4]["name"].startswith("Probably Play All ("))
        self.assertTrue(identify.PLACEHOLDER.match(items[0]["name"]))  # never shared as a real name
        import community
        self.assertEqual(community._title_row(items[0], {})["name"], "")

    def test_shared_names_for_skipped_titles_come_through(self):
        import catalog
        row = catalog._as_discdb_row({"role": "extra", "kept": 0, "category": "Other", "name": "Menu Background",
                                      "season": None, "episode": None, "size_bytes": 1, "duration_s": 92})
        self.assertEqual(row["description"], "Menu Background")
        plan = {"kind": "movie", "title": "Film", "year": "2020", "items": [
            {"source": "a.mkv", "role": "extra", "action": "move", "name": "Extra 01 (1m32s)", "category": "Other",
             "duration_seconds": 92, "dest_dir": "x", "note": ""}]}
        identify.apply_discdb(plan, {"pairs": {"a.mkv": row}, "community": True, "src_name": "the Shelfrip disc database"}, None)
        it = plan["items"][0]
        self.assertEqual((it["action"], it["name"]), ("skip", "Menu Background"))

    def test_drive_lost_detection(self):
        self.assertTrue(rip_all.drive_lost('MSG:5010,0,0,"Failed to open disc","Failed to open disc"'))
        self.assertFalse(rip_all.drive_lost('MSG:2003,0,1,"Error \'Scsi error - MEDIUM ERROR\' occurred while reading"'))


if __name__ == "__main__":
    unittest.main()
