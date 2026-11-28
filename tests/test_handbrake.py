"""
test_handbrake.py - the optional HandBrake step: reading your presets, what's checked before
filing, the command (every track kept), stopping mid-encode, and a real encode when HandBrakeCLI,
ffmpeg and MKVToolNix are installed.

Run on its own (python tests/test_handbrake.py): Shelfrip reads its settings when first imported.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discs  # noqa: E402

TMP = discs.setup_env({"tmdb_api_key": "", "eject_after_filing": False, "media_server": "plex"})
(TMP / "lib").mkdir()
import handbrake  # noqa: E402
import review  # noqa: E402

PRESETS = TMP / "presets.json"
PRESETS.write_text(json.dumps({"PresetList": [
    {"PresetName": "Top Level", "FileFormat": "av_mkv", "AudioList": [{"AudioEncoder": "av_aac", "AudioMixdown": "stereo",
                                                                      "AudioBitrate": 160}]},
    {"Folder": True, "PresetName": "My Presets", "ChildrenArray": [
        {"PresetName": "Small MP4", "FileFormat": "av_mp4", "AudioList": [{"AudioEncoder": "av_aac",
                                                                          "AudioTrackQualityEnable": True}]}]}]}))


def cfg(**kw) -> dict:
    return {"handbrake_enabled": True, "handbrake_preset_file": str(PRESETS), "handbrake_preset": "Top Level", **kw}


class Presets(unittest.TestCase):
    def test_reads_presets_in_folders(self):
        self.assertEqual([p["PresetName"] for p in handbrake.presets(cfg())], ["Top Level", "Small MP4"])
        self.assertEqual(handbrake.extension(cfg()), ".mkv")
        self.assertEqual(handbrake.extension(cfg(handbrake_preset="Small MP4")), ".mp4")

    def test_off_unless_turned_on(self):
        self.assertFalse(handbrake.applies({}, {"role": "main"}))
        self.assertEqual(handbrake.problem({}), "")
        self.assertTrue(handbrake.applies(cfg(), {"role": "main"}))
        self.assertTrue(handbrake.applies(cfg(), {"role": "episode"}))
        self.assertFalse(handbrake.applies(cfg(), {"role": "extra"}))
        self.assertTrue(handbrake.applies(cfg(handbrake_scope="all"), {"role": "extra"}))

    def test_problems_before_filing(self):
        none = str(TMP / "nowhere" / "HandBrakeCLI.exe")
        real = shutil.which
        handbrake.shutil.which = lambda n: None
        try:
            self.assertIn("HandBrakeCLI wasn't found", handbrake.problem(cfg(handbrake_cli=none)))
        finally:
            handbrake.shutil.which = real
        cli = TMP / "HandBrakeCLI.exe"
        cli.write_text("")
        self.assertEqual(handbrake.problem(cfg(handbrake_cli=str(cli))), "")
        self.assertIn("no presets file", handbrake.problem(cfg(handbrake_cli=str(cli), handbrake_preset_file=str(TMP / "x.json"))))
        self.assertIn("no preset is chosen", handbrake.problem(cfg(handbrake_cli=str(cli), handbrake_preset="")))
        msg = handbrake.problem(cfg(handbrake_cli=str(cli), handbrake_preset="Nope"))
        self.assertIn("isn't in presets.json", msg)
        self.assertIn("Small MP4", msg)

    def test_command_keeps_every_track(self):
        cmd = handbrake.command(cfg(), Path("in.mkv"), Path("out.mkv"))
        for flag in ("--all-audio", "--all-subtitles"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--audio-lang-list") + 1], "any")
        self.assertEqual(cmd[cmd.index("-E") + 1], "av_aac")
        self.assertEqual(cmd[cmd.index("-B") + 1], "160")
        self.assertEqual(cmd[cmd.index("-Z") + 1], "Top Level")
        q = handbrake.command(cfg(handbrake_preset="Small MP4"), Path("in.mkv"), Path("out.mp4"))
        self.assertNotIn("-B", q)  # quality-based audio: no bitrate forced
        off = handbrake.command(cfg(handbrake_keep_tracks=False), Path("in.mkv"), Path("out.mkv"))
        self.assertNotIn("--all-audio", off)


@unittest.skipIf(os.name == "nt", "uses a shebang script as a stand-in for HandBrakeCLI")
class FakeEncode(unittest.TestCase):
    def fake(self, body: str) -> Path:
        f = TMP / f"fakehb{time.time_ns()}"
        f.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
        f.chmod(0o755)
        return f

    def test_progress_and_output(self):
        cli = self.fake('''
            import sys, time
            out = sys.argv[sys.argv.index("-o") + 1]
            for p in (10, 50, 99.5):
                sys.stdout.write(f"\\rEncoding: task 1 of 1, {p:.2f} %"); sys.stdout.flush(); time.sleep(0.05)
            open(out, "wb").write(b"encoded")
        ''')
        seen = []
        out = TMP / "enc" / "a.mkv"
        handbrake.encode(TMP / "presets.json", out, cfg(handbrake_cli=str(cli)), on_progress=seen.append)
        self.assertEqual(out.read_bytes(), b"encoded")
        self.assertEqual([round(x, 3) for x in seen], [0.1, 0.5, 0.995])

    def test_failure_says_why(self):
        cli = self.fake('''
            print("[12:00:00] hb_init: starting libhb thread")
            print("ERROR: Invalid preset Top Level")
            raise SystemExit(3)
        ''')
        with self.assertRaises(RuntimeError) as e:
            handbrake.encode(PRESETS, TMP / "enc" / "b.mkv", cfg(handbrake_cli=str(cli)))
        self.assertIn("Invalid preset", str(e.exception))
        self.assertFalse((TMP / "enc" / "b.encoding.mkv").exists())

    def test_stop_mid_encode(self):
        cli = self.fake('''
            import sys, time
            out = sys.argv[sys.argv.index("-o") + 1]
            open(out, "wb").write(b"half")
            for p in range(100):
                sys.stdout.write(f"\\rEncoding: task 1 of 1, {p:.2f} %"); sys.stdout.flush(); time.sleep(0.1)
        ''')
        stop = threading.Event()
        threading.Timer(0.4, stop.set).start()
        t0 = time.time()
        with self.assertRaises(handbrake.Stopped):
            handbrake.encode(PRESETS, TMP / "enc" / "c.mkv", cfg(handbrake_cli=str(cli)), stopped=stop.is_set)
        self.assertLess(time.time() - t0, 5)
        self.assertFalse((TMP / "enc" / "c.encoding.mkv").exists())
        self.assertFalse((TMP / "enc" / "c.mkv").exists())


def mp4(seconds: float, version: int = 0, moov_last: bool = True, large_mdat: bool = False) -> bytes:
    """A bare MP4: ftyp, mdat and a moov holding just the movie header."""
    import struct
    scale = 90000
    if version == 1:
        mvhd = struct.pack(">B3xQQIQ", 1, 0, 0, scale, int(seconds * scale)) + bytes(80)
    else:
        mvhd = struct.pack(">B3xIIII", 0, 0, 0, scale, int(seconds * scale)) + bytes(80)
    mvhd = struct.pack(">I4s", 8 + len(mvhd), b"mvhd") + mvhd
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    ftyp = struct.pack(">I4s", 16, b"ftyp") + b"isom" + bytes(4)
    data = bytes(1000)
    mdat = (struct.pack(">I4sQ", 1, b"mdat", 16 + len(data)) if large_mdat else struct.pack(">I4s", 8 + len(data), b"mdat")) + data
    return ftyp + (mdat + moov if moov_last else moov + mdat)


class Mp4Length(unittest.TestCase):
    """MKVToolNix gives no length for MP4s, so every MP4 preset's encode looked 0 s long and was thrown away."""
    def test_reads_the_movie_header(self):
        for kw in ({}, {"version": 1}, {"moov_last": False}, {"large_mdat": True}):
            f = TMP / "len.mp4"
            f.write_bytes(mp4(6180.5, **kw))
            self.assertAlmostEqual(review._mp4_length(f), 6180.5, places=3, msg=kw)
            self.assertAlmostEqual(review._length(f, "mkvmerge-not-used"), 6180.5, places=3)

    def test_not_an_mp4(self):
        f = TMP / "junk.mp4"
        f.write_bytes(b"not a movie at all")
        self.assertEqual(review._mp4_length(f), 0)


class StopMessage(unittest.TestCase):
    def test_progress_after_stop_keeps_the_stopped_message(self):
        """HandBrake can print progress after Stop; it used to replace "Filing stopped" with "encoding (2%)"."""
        from unittest import mock
        src = TMP / "stopsrc.mkv"
        src.write_bytes(b"x" * 100)
        job = {}

        def fake_encode(s, out, c, on_progress=None, stopped=None):
            on_progress(0.01)
            job["stop"] = True
            job["current"] = "Filing stopped."
            on_progress(0.02)
            raise handbrake.Stopped()
        with mock.patch.object(handbrake, "applies", lambda c, it: True), \
                mock.patch.object(handbrake, "encode", fake_encode):
            with self.assertRaises(review.Stopped):
                review._place(src, TMP / "lib" / "x.mkv", {}, job, TMP / "rip", "Filing 1 of 1")
        self.assertEqual(job["current"], "Filing stopped.")


REAL = all(shutil.which(t) for t in ("HandBrakeCLI", "ffmpeg", "ffprobe", "mkvmerge"))


@unittest.skipUnless(REAL, "needs HandBrakeCLI, ffmpeg and MKVToolNix installed")
class RealEncode(unittest.TestCase):
    def test_files_an_encode_with_every_track(self):
        d = TMP / "rip" / "ENC__1"
        d.mkdir()
        (d / "disc_info.json").write_text("{}")
        srt = d / "s.srt"
        srt.write_text("1\n00:00:01,000 --> 00:00:03,000\nhello\n")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=6:s=320x240:r=24",
                        *sum((["-f", "lavfi", "-i", f"sine=f={f}:d=6"] for f in (440, 660, 880)), []),
                        "-i", str(srt), "-i", str(srt), *sum((["-map", str(k)] for k in range(6)), []),
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "ac3", "-c:s", "srt",
                        "-metadata:s:a:1", "title=Director's Commentary", "-metadata:s:a:2", "language=fre",
                        str(d / "t00.mkv")], check=True)
        srt.unlink()
        export = TMP / "real-presets.json"  # a preset as HandBrake exports it, keeping only the first audio track
        subprocess.run(["HandBrakeCLI", "-Z", "Very Fast 480p30", "--preset-export", "Mine", "--preset-export-file",
                        str(export), "-i", str(d / "t00.mkv"), "-o", str(TMP / "ignore.mkv")], capture_output=True)
        lib = "Movies\\Enc (2000) {tmdb-77}"
        plan = {"kind": "movie", "title": "Enc", "year": "2000", "tmdb_id": "77", "status": "proposed", "format": "DVD",
                "disc_label": "ENC", "library_dir": lib, "items": [{"source": "t00.mkv", "action": "move", "role": "main",
                                                                    "dest_dir": lib, "name": "Enc (2000) {tmdb-77}"}]}
        review.save_plan(d, plan)
        review.CFG.update(cfg(handbrake_preset_file=str(export), handbrake_preset="Mine"))
        try:
            self.assertEqual(review.start_filing(d, json.loads((d / "plan.json").read_text())), [])
            review.THREADS["ENC__1"].join(120)
        finally:
            for k in list(review.CFG):
                if k.startswith("handbrake_"):
                    review.CFG.pop(k)
        job = review.JOBS["ENC__1"]
        self.assertEqual(job["state"], "done", job)
        out = TMP / "lib" / "Movies" / "Enc (2000) {tmdb-77}" / "Enc (2000) {tmdb-77}.mkv"
        self.assertTrue(out.exists())
        streams = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(out)],
                                            capture_output=True, text=True).stdout)["streams"]
        kinds = [s["codec_type"] for s in streams]
        self.assertEqual((kinds.count("audio"), kinds.count("subtitle")), (3, 2))
        self.assertIn("Director's Commentary", [s.get("tags", {}).get("title") for s in streams])
        self.assertFalse(d.exists())  # filed: the rip folder (original included) is cleared


if __name__ == "__main__":
    unittest.main(verbosity=2)
