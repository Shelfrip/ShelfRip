r"""
subcheck.py - check what Shelfrip identified against each video's subtitles.

For every rip folder (plan.json + its videos) it:

  1. picks the subtitle track to read (text captions first, then Blu-ray PGS or DVD VobSub
     pictures in your language),
  2. copies that track out with mkvextract into a temporary folder,
  3. reads every subtitle's start and end time, and turns about 60 of them into text with
     Tesseract OCR (captions are already text),
  4. scores that text against TMDB: character names, episode titles and plot summaries, and
  5. prints what fits, what doesn't, and why (the matched words), and saves subcheck.json
     next to plan.json.

It also checks, from subtitle timing alone, that split points don't land in the middle of a
line of dialogue and whether titles marked as copies really are copies.

It only reads. Videos, plan.json and every track stay exactly as they are; the temporary
copies are deleted afterwards.

Needs MKVToolNix (already used for splitting) and Tesseract OCR for picture subtitles:
    winget install UB-Mannheim.TesseractOCR
(or set "tesseract_path" in config.json, or put it in tools\tesseract\).

    python subcheck.py                   every rip folder that hasn't been filed yet
    python subcheck.py "E:\Ripped\X"     one or more folders
    python subcheck.py --filed Middle    filed discs whose folder name contains "Middle"
                                         (reads the copies in your library)

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import bisect
import difflib
import json
import math
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import zlib
from pathlib import Path

HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SAMPLES = 600         # subtitle lines read per title or episode (OCR is about 1 s per 60)
EDGE = 90             # seconds skipped at the start and end of long titles (theme songs, credits)
TESSERACT_URL = "https://github.com/UB-Mannheim/tesseract/wiki"

# ISO 639-2 (what MKV tracks use) -> Tesseract language data names
TESS_LANG = {"eng": "eng", "fre": "fra", "fra": "fra", "ger": "deu", "deu": "deu", "spa": "spa", "ita": "ita",
             "por": "por", "dut": "nld", "nld": "nld", "swe": "swe", "nor": "nor", "dan": "dan", "fin": "fin",
             "pol": "pol", "cze": "ces", "ces": "ces", "hun": "hun", "tur": "tur", "rus": "rus", "jpn": "jpn",
             "chi": "chi_sim", "zho": "chi_sim", "kor": "kor"}


# --------------------------------------------------------------------------- tools

def tesseract(cfg: dict) -> str | None:
    for cand in (cfg.get("tesseract_path"), HERE / "tools" / "tesseract" / "tesseract.exe",
                 r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                 r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
        if cand and Path(cand).is_file():
            return str(cand)
    return shutil.which("tesseract")


def tess_langs(exe: str) -> set[str]:
    try:
        r = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=30, creationflags=NOWIN)
        return {ln.strip() for ln in (r.stdout + r.stderr).splitlines()[1:] if ln.strip()}
    except Exception:
        return set()


def mkv_info(path: Path, cfg: dict) -> dict:
    import split
    r = subprocess.run([split.tool(cfg, "mkvmerge"), "-J", str(path)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", creationflags=NOWIN)
    return json.loads(r.stdout or "{}")


# --------------------------------------------------------------------------- choosing a track

def subtitle_tracks(info: dict, want: list[str]) -> list[dict]:
    """Subtitle tracks worth reading, best first: text, then pictures; your language first;
    forced-only tracks (a few foreign-dialogue lines) last."""
    out = []
    for t in info.get("tracks", []):
        if t.get("type") != "subtitles":
            continue
        p = t.get("properties", {})
        cid = p.get("codec_id", "")
        kind = "text" if cid.startswith("S_TEXT") else "pgs" if cid == "S_HDMV/PGS" else \
            "vobsub" if cid == "S_VOBSUB" else ""
        if not kind:
            continue
        lang = p.get("language") or "und"
        rank = (want.index(lang) if lang in want else len(want),
                1 if p.get("forced_track") or "forced" in (p.get("track_name") or "").lower() else 0,
                {"text": 0, "pgs": 1, "vobsub": 2}[kind])
        out.append({"id": t["id"], "kind": kind, "lang": lang, "rank": rank})
    return sorted(out, key=lambda x: x["rank"])


# --------------------------------------------------------------------------- reading subtitles
# A cue is {"start": s, "end": s, "text": str} for text, or {"start", "end", "img": callable}
# for pictures, where img() returns (width, height, bytes) with 1 = text pixel, 0 = not.

def read_srt(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    cues = []
    for block in re.split(r"\n\s*\n", raw.replace("\r", "")):
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        text = block[m.end():].strip()
        text = re.sub(r"<[^>]+>|\{[^}]*\}", "", text)
        if text:
            cues.append({"start": start, "end": end, "text": text})
    return cues


def _u16(b: bytes, i: int) -> int:
    return (b[i] << 8) | b[i + 1]


def read_pgs(path: Path) -> list[dict]:
    """Blu-ray PGS (.sup): display sets of palette, picture and composition segments."""
    data = path.read_bytes()
    cues: list[dict] = []
    palettes: dict[int, dict[int, tuple[int, int]]] = {}
    objects: dict[int, list] = {}
    pending = None
    i, n = 0, len(data)
    while i + 13 <= n:
        if data[i:i + 2] != b"PG":
            j = data.find(b"PG", i + 1)
            if j < 0:
                break
            i = j
            continue
        pts = struct.unpack(">I", data[i + 2:i + 6])[0] / 90000
        kind, size = data[i + 10], _u16(data, i + 11)
        seg = data[i + 13:i + 13 + size]
        i += 13 + size
        if kind == 0x16 and len(seg) >= 11:                      # presentation composition
            state, pal_id, count = seg[7], seg[9], seg[10]
            if state & 0x80:                                     # epoch start: everything is new
                palettes.clear()
                objects.clear()
            objs, p = [], 11
            for _ in range(count):
                if p + 8 > len(seg):
                    break
                oid, crop = _u16(seg, p), seg[p + 3]
                objs.append((oid, _u16(seg, p + 4), _u16(seg, p + 6)))
                p += 16 if crop & 0x40 else 8
            if cues and cues[-1]["end"] is None:
                cues[-1]["end"] = pts                            # an empty composition clears the screen
            pending = {"start": pts, "end": None, "pal": pal_id, "objs": objs} if objs else None
        elif kind == 0x14 and seg:                               # palette
            pal = palettes.setdefault(seg[0], {})
            for j in range(2, len(seg) - 4, 5):
                pal[seg[j]] = (seg[j + 1], seg[j + 4])           # (luma, alpha)
        elif kind == 0x15 and len(seg) >= 4:                     # picture (maybe in pieces)
            oid, flag = _u16(seg, 0), seg[3]
            if flag & 0x80 and len(seg) >= 11:
                objects[oid] = [_u16(seg, 7), _u16(seg, 9), bytearray(seg[11:])]
            elif oid in objects:
                objects[oid][2] += seg[4:]
        elif kind == 0x80 and pending:                           # end of display set
            parts = [(x, y, objects[o][0], objects[o][1], bytes(objects[o][2])) for o, x, y in pending["objs"]
                     if o in objects]
            if parts:
                pal = dict(palettes.get(pending["pal"], {}))
                cue = {"start": pending["start"], "end": None, "img": _pgs_image(parts, pal)}
                if cues and cues[-1]["end"] is None:
                    cues[-1]["end"] = cue["start"]
                cues.append(cue)
            pending = None
    for c in cues:
        if c["end"] is None or c["end"] <= c["start"]:
            c["end"] = c["start"] + 3
    return cues


def _pgs_rle(rle: bytes, w: int, h: int) -> bytearray:
    out = bytearray(w * h)
    i, x, y, n = 0, 0, 0, len(rle)
    while i < n and y < h:
        b = rle[i]
        i += 1
        if b:
            if x < w:
                out[y * w + x] = b
            x += 1
            continue
        if i >= n:
            break
        f = rle[i]
        i += 1
        if f == 0:                       # end of line
            x, y = 0, y + 1
            continue
        if f & 0x40:
            run = ((f & 0x3F) << 8) | rle[i]
            i += 1
        else:
            run = f & 0x3F
        color = 0
        if f & 0x80:
            color = rle[i]
            i += 1
        if color:
            end = min(x + run, w)
            if end > x:
                out[y * w + x:y * w + end] = bytes([color]) * (end - x)
        x += run
    return out


def _pgs_image(parts, pal):
    def render():
        x0 = min(p[0] for p in parts)
        y0 = min(p[1] for p in parts)
        W = max(p[0] + p[2] for p in parts) - x0
        H = max(p[1] + p[3] for p in parts) - y0
        out = bytearray(W * H)
        lut = bytes(1 if (pal.get(c, (0, 0))[1] >= 128 and pal.get(c, (0, 0))[0] >= 128) else 0 for c in range(256))
        for x, y, w, h, rle in parts:
            px = _pgs_rle(rle, w, h).translate(lut)
            for r in range(h):
                o = (y - y0 + r) * W + (x - x0)
                out[o:o + w] = px[r * w:(r + 1) * w]
        return W, H, bytes(out)
    return render


def read_vobsub(idx: Path, sub: Path) -> list[dict]:
    """DVD VobSub (.idx + .sub): a 16-colour palette, then one picture packet per subtitle."""
    palette = [(0, 0, 0)] * 16
    entries = []
    for line in idx.read_text(encoding="latin-1", errors="replace").splitlines():
        if line.lower().startswith("palette:"):
            cols = [c.strip() for c in line.split(":", 1)[1].split(",")]
            palette = [(int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)) for c in cols[:16] if len(c) >= 6] \
                + palette[len(cols):]
        m = re.match(r"timestamp:\s*(-?)(\d+):(\d+):(\d+):(\d+),\s*filepos:\s*([0-9a-fA-F]+)", line.strip())
        if m:
            t = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4)) + int(m.group(5)) / 1000
            entries.append((-t if m.group(1) else t, int(m.group(6), 16)))
    data = sub.read_bytes()
    luma = [0.299 * r + 0.587 * g + 0.114 * b for r, g, b in palette]
    cues = []
    for t, pos in entries:
        spu = _ps_packet(data, pos)
        if not spu:
            continue
        try:
            ctl = _spu_control(spu)
        except Exception:
            continue
        if not ctl:
            continue
        start = t + ctl["start"]
        end = t + ctl["stop"] if ctl.get("stop") else None
        cues.append({"start": start, "end": end, "img": _vobsub_image(spu, ctl, luma)})
    cues.sort(key=lambda c: c["start"])
    for k, c in enumerate(cues):   # some discs never say when a line goes away: cap it at the next one
        nxt = cues[k + 1]["start"] if k + 1 < len(cues) else c["start"] + 10
        if c["end"] is None or c["end"] <= c["start"] or c["end"] > nxt or c["end"] - c["start"] > 10:
            c["end"] = min(c["start"] + 10, nxt, c["end"] if c["end"] and c["end"] > c["start"] else 1e12)
    return cues


def _ps_packet(data: bytes, pos: int) -> bytes | None:
    """Collect one subtitle packet from MPEG program stream packs starting at pos."""
    buf, need, n = bytearray(), None, len(data)
    while pos + 6 <= n:
        if data[pos:pos + 3] != b"\x00\x00\x01":
            return bytes(buf[:need]) if buf else None
        sid = data[pos + 3]
        if sid == 0xBA:                                          # pack header
            pos += 14 + (data[pos + 13] & 7) if data[pos + 4] & 0xC0 == 0x40 else 12
            continue
        plen = _u16(data, pos + 4)
        end = pos + 6 + plen
        if sid == 0xBD:
            start = pos + 9 + data[pos + 8] + 1                  # PES header, then the sub-stream id
            buf += data[start:end]
            if need is None and len(buf) >= 2:
                need = _u16(buf, 0)
            if need and len(buf) >= need:
                return bytes(buf[:need])
        pos = end
    return bytes(buf) if buf else None


def _spu_control(spu: bytes) -> dict | None:
    ctl = {"start": 0.0, "stop": None, "color": [0, 1, 2, 3], "alpha": [0, 15, 15, 15]}
    pos, seen = _u16(spu, 2), set()
    while pos + 4 <= len(spu) and pos not in seen:
        seen.add(pos)
        delay = _u16(spu, pos) * 1024 / 90000
        nxt = _u16(spu, pos + 2)
        p = pos + 4
        while p < len(spu):
            cmd = spu[p]
            p += 1
            if cmd in (0x00, 0x01):
                ctl["start"] = delay
            elif cmd == 0x02:
                ctl["stop"] = delay
            elif cmd in (0x03, 0x04):
                a, b = spu[p], spu[p + 1]
                ctl["color" if cmd == 3 else "alpha"] = [b & 15, b >> 4, a & 15, a >> 4]
                p += 2
            elif cmd == 0x05:
                b = spu[p:p + 6]
                ctl["x1"], ctl["x2"] = (b[0] << 4) | (b[1] >> 4), ((b[1] & 15) << 8) | b[2]
                ctl["y1"], ctl["y2"] = (b[3] << 4) | (b[4] >> 4), ((b[4] & 15) << 8) | b[5]
                p += 6
            elif cmd == 0x06:
                ctl["off"] = (_u16(spu, p), _u16(spu, p + 2))
                p += 4
            else:
                break
        if nxt == pos:
            break
        pos = nxt
    return ctl if "off" in ctl and "x2" in ctl else None


def _vobsub_image(spu: bytes, ctl: dict, luma: list[float]):
    def render():
        w = ctl["x2"] - ctl["x1"] + 1
        h = ctl["y2"] - ctl["y1"] + 1
        if w <= 0 or h <= 0:
            return 0, 0, b""
        rows = [None] * h
        nib = 0

        def get():
            nonlocal nib
            b = spu[nib >> 1]
            nib += 1
            return (b >> 4) if nib & 1 else (b & 15)

        for field, off in enumerate(ctl["off"]):
            nib = off * 2
            for y in range(field, h, 2):
                row = bytearray()
                while len(row) < w:
                    v = get()
                    if v < 0x4:
                        v = (v << 4) | get()
                        if v < 0x10:
                            v = (v << 4) | get()
                            if v < 0x40:
                                v = (v << 4) | get()
                    run, c = v >> 2, v & 3
                    if run == 0:
                        run = w - len(row)
                    row += bytes([c]) * min(run, w - len(row))
                if nib & 1:
                    nib += 1
                rows[y] = row
        # the text is the brightest colour that isn't see-through (white text, dark outline)
        opaque = [k for k in range(4) if ctl["alpha"][k] > 0]
        if not opaque:
            return w, h, bytes(w * h)
        lum = {k: luma[ctl["color"][k]] for k in opaque}
        text = max(opaque, key=lambda k: lum[k])
        if max(lum.values()) - min(lum.values()) < 40:           # all one shade: take the commonest
            counts = {k: sum(r.count(k) for r in rows) for k in opaque}
            text = max(counts, key=counts.get)
        lut = bytes(1 if k == text else 0 for k in range(4)) + bytes(252)
        return w, h, b"".join(bytes(r).translate(lut) for r in rows)
    return render


# --------------------------------------------------------------------------- OCR

def _png(w: int, h: int, gray: bytes) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + gray[y * w:(y + 1) * w] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _crop(w: int, h: int, px: bytes):
    rows = [y for y in range(h) if 1 in px[y * w:(y + 1) * w]]
    if not rows:
        return None
    acc = 0
    for y in rows:
        acc |= int.from_bytes(px[y * w:(y + 1) * w], "big")
    used = acc.to_bytes(w, "big")
    x0 = next(x for x in range(w) if used[x])
    x1 = w - next(x for x in range(w) if used[w - 1 - x])
    y0, y1 = rows[0], rows[-1] + 1
    return [px[y * w + x0:y * w + x1] for y in range(y0, y1)]


INK = bytes([255, 0]) + bytes(254)   # 1 = text -> black, 0 -> white


def ocr(cues: list[dict], exe: str, lang: str, tmp: Path, scale: int) -> str:
    """Stack the cue pictures (black text on white) into one image and OCR it in one go."""
    lines = []
    for c in cues:
        try:
            w, h, px = c["img"]()
        except Exception:
            continue
        rows = _crop(w, h, px) if w and h else None
        if rows:
            lines.append(rows)
    if not lines:
        return ""
    pad = 16 * scale
    W = max(len(r[0]) for r in lines) * scale + 2 * pad
    out = bytearray()
    blank = b"\xff" * W
    for rows in lines:
        out += blank * pad
        for r in rows:
            row = bytes(r).translate(INK)
            if scale > 1:
                row = b"".join(bytes([v]) * scale for v in row)
            row = b"\xff" * pad + row + b"\xff" * (W - pad - len(row))
            out += row * scale
    out += blank * pad
    H = len(out) // W
    img = tmp / f"ocr_{len(list(tmp.glob('ocr_*.png')))}.png"
    img.write_bytes(_png(W, H, bytes(out)))
    r = subprocess.run([exe, str(img), "stdout", "-l", lang, "--psm", "6"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=600, creationflags=NOWIN)
    return r.stdout


# --------------------------------------------------------------------------- words

STOP = set("""
a about above after again against all almost also although always am among an and another any anyone anything are
around as at away back be became because become becomes been before begin begins behind being below best better
between big both but by came can cannot come comes could day days did do does doing done down during each either
else even ever every everyone everything far few find finds first for found from get gets getting give gives go
goes going gone good got great had has have having he her here hers herself him himself his how however if in
into is it its itself just keep know last later least left less let lets life like little long look looks lot made
make makes making man many may me meanwhile meet meets might more most much must my myself need needs never new
next no none nor not nothing now of off old on once one only or other others our out over own part people place
plan plans put really right said same say see seems set she should show shows since so some someone something
soon still such take takes taking tell than that the their them themselves then there these they thing things
think this those though three through time times to together told too tries try trying turn turns two under
until up upon us use very want wants was way we well went were what when where whether which while who whom whose
why will with without woman women world would year years yet you your yours
episode episodes season series film movie story stories voice voices uncredited self narrator additional various
himself herself themselves cameo archive footage decides decide learns learn discovers discover becomes help helps
tries friends friend family home school work night team group brother sister father mother son daughter wife
husband boy girl guy kids kid baby
""".split())


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower().replace("\u2019", "'")


def words(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+(?:'[a-z]+)?", fold(text)):
        w = re.sub(r"'(s|ll|re|ve|d|t|m)$", "", w)
        if len(w) >= 3 and not w.isdigit() and w not in STOP:
            out.append(w)
    return out


def character_words(chars: list[str]) -> list[str]:
    out = []
    for c in chars:
        c = re.sub(r"\(.*?\)", " ", c or "")
        for part in re.split(r"[/,]", c):
            out += [w for w in words(part) if w not in ROLES]
    return out


# Titles and job words in character names ("Knight of House Frey", "Pentoshi Servant", "Guard #2")
ROLES = set("""
lord lady ser sir king queen prince princess knight house maester septa grand master captain doctor dr mr mrs
miss officer guard guards soldier soldiers man men woman women girl boy old young little big the servant
waiter waitress driver nurse reporter student teacher priest agent detective sergeant lieutenant general
commander chief judge cop police pilot clerk bartender customer passenger kid child baby mom dad wife husband
""".split())


class Matcher:
    """Which of a list of words appear in the OCR text, allowing for OCR slips."""

    def __init__(self, text: str):
        self.flat = " " + " ".join(re.findall(r"[a-z0-9]+", fold(text))) + " "
        self.words = set(words(text))
        # Names are written with a capital in subtitles; a word also seen in lower case is an
        # ordinary word ("servant", "drunk") even when TMDB lists it as part of a character.
        self.lower = {fold(t) for t in re.findall(r"[^\W\d_][\w']*", text) if t[0].islower()}
        self.by_first: dict[str, list[str]] = {}
        for w in self.words:
            self.by_first.setdefault(w[0], []).append(w)
        self.memo: dict[str, bool] = {}

    def has(self, w: str) -> bool:
        if w in self.memo:
            return self.memo[w]
        ok = w in self.words
        if not ok and len(w) >= 5:
            for s in self.by_first.get(w[0], []):
                if abs(len(s) - len(w)) <= 2:
                    sm = difflib.SequenceMatcher(None, w, s)
                    if sm.real_quick_ratio() >= 0.84 and sm.quick_ratio() >= 0.84 and sm.ratio() >= 0.84:
                        ok = True
                        break
        self.memo[w] = ok
        return ok

    def phrase(self, seq: list[str]) -> bool:
        return bool(seq) and f" {' '.join(seq)} " in self.flat


def weigh(profiles: dict) -> dict[str, float]:
    """Rarer words count for more: a name every episode shares tells nothing apart."""
    n = len(profiles)
    df: dict[str, int] = {}
    for p in profiles.values():
        for w in p["words"]:
            df[w] = df.get(w, 0) + 1
    return {w: math.log((n + 1) / (d + 0.5)) + 0.1 for w, d in df.items()}


def common_on_disc(matchers: list, profiles: dict) -> dict[str, float]:
    """Words heard in nearly every title on this disc ("lord", the hero's name) say little about
    which episode a title is, so they count for less."""
    if len(matchers) < 3:
        return {}
    out = {}
    for w in {w for p in profiles.values() for w in p["words"]}:
        if sum(m.has(w) for m in matchers) >= 0.75 * len(matchers):
            out[w] = 0.3
    return out


def score(m: Matcher, prof: dict, weight: dict[str, float], damp: dict[str, float] | None = None
          ) -> tuple[float, list[str]]:
    damp = damp or {}
    ws = prof["words"]
    mult = {w: min(ws[w], 0.35) if w in m.lower else ws[w] for w in ws}
    val = {w: weight.get(w, 0) * mult[w] * damp.get(w, 1.0) for w in ws if m.has(w)}
    hit = sorted(val, key=lambda w: -val[w])
    total = sum(weight.get(w, 0) * mult[w] for w in ws) or 1
    sc = sum(val.values()) / math.sqrt(total)
    if m.phrase(prof.get("phrase")):        # the episode's own title said out loud
        sc += 1.0
        hit.insert(0, '"' + " ".join(prof["phrase"]) + '"')
    return sc, hit


# --------------------------------------------------------------------------- TMDB profiles
# A profile is {"name", "words": {word: how much it counts}, "phrase": title words, "raw": what TMDB said}.
# Character names count most, other names in the summary next, ordinary summary words least.

def proper_nouns(text: str) -> list[str]:
    out = []
    for sent in re.split(r"(?<=[.!?;:])\s+", text or ""):
        for t in re.findall(r"[^\W\d_][\w'\u2019-]*", sent):   # sentence starters too: STOP drops "The", "When"...
            if t[0].isupper():
                out += words(t)
    return out


def make_profile(name: str, title: str, overview: str, chars: list[str]) -> dict:
    ws: dict[str, float] = {}

    def add(lst, mult):
        for w in lst:
            ws[w] = max(ws.get(w, 0.0), mult)
    add(words(overview), 0.35)
    add(words(title), 0.5)
    add(proper_nouns(overview), 1.0)
    add(character_words(chars), 1.2)
    phrase = re.findall(r"[a-z0-9]+", fold(title))
    return {"name": name, "words": ws, "phrase": phrase if len(phrase) >= 2 else [],
            "raw": {"title": title, "overview": overview, "characters": [c for c in chars if c]}}


def episode_profiles(tmdb, tid: str, season: int) -> dict[int, dict]:
    data = tmdb.season(int(tid), season)
    out = {}
    for e in data.get("episodes", []):
        chars = [g.get("character") for g in e.get("guest_stars") or []]
        out[int(e["episode_number"])] = make_profile(e.get("name") or "", e.get("name") or "",
                                                     e.get("overview") or "", chars)
    return out


def movie_profile(tmdb, mid) -> dict:
    m = tmdb.get(f"/movie/{int(mid)}")
    try:
        cast = tmdb.get(f"/movie/{int(mid)}/credits").get("cast", [])[:40]
    except Exception:
        cast = []
    p = make_profile(f"{m.get('title')} ({(m.get('release_date') or '')[:4]})", m.get("title") or "",
                     f"{m.get('overview') or ''} {m.get('tagline') or ''}", [c.get("character") for c in cast])
    return dict(p, kind="movie", id=str(mid))


def show_profile(tmdb, tid) -> dict:
    s = tmdb.tv(int(tid))
    try:
        cast = tmdb.get(f"/tv/{int(tid)}/aggregate_credits").get("cast", [])[:40]
        chars = [r.get("character") for c in cast for r in c.get("roles") or []]
    except Exception:
        chars = []
    p = make_profile(f"{s.get('name')} (TV series)", s.get("name") or "", s.get("overview") or "", chars)
    return dict(p, kind="tv", id=str(tid))


# --------------------------------------------------------------------------- timing checks

def same_timing(a: list[dict], b: list[dict]) -> float:
    """Share of a's subtitles that start when one of b's does (after lining the two up)."""
    if len(a) < 10 or len(b) < 10:
        return 0.0
    sb = [c["start"] for c in b]
    offs: dict[int, int] = {}
    for c in a[:: max(1, len(a) // 80)]:
        j = bisect.bisect_left(sb, c["start"] - 30)
        while j < len(sb) and sb[j] <= c["start"] + 30:
            k = round((sb[j] - c["start"]) * 5)
            offs[k] = offs.get(k, 0) + 1
            j += 1
    if not offs:
        return 0.0
    off = max(offs, key=offs.get) / 5
    hits = 0
    for c in a:
        j = bisect.bisect_left(sb, c["start"] + off - 0.25)
        hits += j < len(sb) and sb[j] <= c["start"] + off + 0.25
    return hits / len(a)


def cut_check(cues: list[dict], t: float) -> dict:
    """Is a split point at time t inside a subtitle, and how much quiet is around it?"""
    inside = [c for c in cues if c["start"] < t - 0.1 and c["end"] > t + 0.1]
    before = max((c["end"] for c in cues if c["end"] <= t + 0.1), default=0.0)
    after = min((c["start"] for c in cues if c["start"] >= t - 0.1), default=t + 999)
    return {"at": round(t, 1), "inside_line": bool(inside), "quiet_before": round(max(0.0, t - before), 1),
            "quiet_after": round(max(0.0, after - t), 1)}


# --------------------------------------------------------------------------- per folder

def hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def pick(cues: list[dict], ranges: list[tuple[float, float]], n: int, edge: float) -> list[dict]:
    pool = []
    for a, b in ranges:
        if b - a > 6 * edge:
            a, b = a + edge, b - edge
        pool += [c for c in cues if a <= c["start"] < b]
    if len(pool) <= n:
        return pool
    step = len(pool) / n
    return [pool[int(k * step)] for k in range(n)]


def load_track(video: Path, cfg: dict, want: list[str], tmp: Path, log) -> dict | None:
    """Copy the best subtitle track out of the video and read it: {'kind', 'lang', 'cues'}."""
    import split
    info = mkv_info(video, cfg)
    tracks = subtitle_tracks(info, want)
    if not tracks:
        return None
    best_lang = tracks[0]["lang"]
    tracks = [t for t in tracks if t["lang"] == best_lang][:4]    # same language: keep the fullest one
    args, files = [split.tool(cfg, "mkvextract"), str(video), "tracks"], {}
    for t in tracks:
        ext = {"text": "srt", "pgs": "sup", "vobsub": "sub"}[t["kind"]]
        f = tmp / f"{video.stem}.{t['id']}.{ext}"
        files[t["id"]] = f
        args.append(f"{t['id']}:{f}")
    t0 = time.time()
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NOWIN)
    if r.returncode not in (0, 1):
        log(f"    mkvextract failed: {(r.stdout + r.stderr).strip()[-300:]}")
        return None
    got = None
    for t in tracks:
        f = files[t["id"]]
        try:
            if t["kind"] == "text":
                cues = read_srt(f)
            elif t["kind"] == "pgs":
                cues = read_pgs(f)
            else:
                cues = read_vobsub(f.with_suffix(".idx"), f)
        except Exception as exc:
            log(f"    couldn't read subtitle track {t['id']}: {exc}")
            continue
        if got is None or len(cues) > len(got["cues"]):
            got = dict(t, cues=cues)
    if got:
        got["extract_seconds"] = round(time.time() - t0, 1)
    return got


def units(plan: dict, folder: Path) -> list[dict]:
    """What to check: each title, and each episode inside a video that gets split."""
    out = []
    for it in plan.get("items", []):
        src = folder / it.get("source", "")
        filed = it.get("filed_to")
        role = it.get("role")
        if role not in ("episode", "main") and not (role == "extra" and (it.get("duration_seconds") or 0) >= 20 * 60):
            continue
        sp = it.get("split") if it.get("needs_split") else None
        eps = it.get("episodes") or ([it["episode"]] if it.get("episode") else [])
        if src.is_file():
            total = float(it.get("duration_seconds") or 0) or 1e9
            if sp and len(sp.get("starts", [])) == len(eps):
                ch, n = sp["chapters"], len(sp["chapters"])
                tm = lambda j: ch[j] if j < n else sp["total"]
                for k, ep in enumerate(eps):
                    a = sp["starts"][k]
                    b = sp["starts"][k + 1] if k + 1 < len(sp["starts"]) else n - int(sp.get("ending") or 0)
                    out.append({"item": it, "video": src, "ranges": [(tm(a), tm(b))], "episode": ep,
                                "label": f"{it['source']} part {k + 1}", "part": k})
            else:
                out.append({"item": it, "video": src, "ranges": [(0, total)], "episode": eps[0] if eps else None,
                            "label": it["source"]})
        elif isinstance(filed, list) and len(filed) == len(eps):
            for k, (f, ep) in enumerate(zip(filed, eps)):
                if Path(f).is_file():
                    out.append({"item": it, "video": Path(f), "ranges": [(0, 1e9)], "episode": ep,
                                "label": Path(f).name, "part": k if len(eps) > 1 else None})
        elif isinstance(filed, str) and Path(filed).is_file():
            out.append({"item": it, "video": Path(filed), "ranges": [(0, 1e9)],
                        "episode": eps[0] if eps else None, "label": Path(filed).name})
    return out


def check_folder(folder: Path, cfg: dict, tmdb, exe: str | None, langs: set[str], log=print,
                 samples: int = SAMPLES) -> dict:
    plan = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
    want = [x for x in (cfg.get("subtitle_language"), cfg.get("audio_language"), "eng") if x]
    report = {"folder": folder.name, "kind": plan.get("kind"), "title": plan.get("title"), "titles": [],
              "findings": [], "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    log(f"\n== {folder.name}: {plan.get('title')} ({plan.get('kind')}"
        + (f", season {plan.get('season')}" if plan.get("kind") == "tv" else "") + ")")
    try:
        imported = bool(json.loads((folder / "disc_info.json").read_text(encoding="utf-8")).get("imported_from"))
    except (OSError, ValueError):
        imported = False
    todo = units(plan, folder)
    if not todo:
        log("   nothing to check (no episode or main-feature videos found)")
        return report
    t_start = time.time()
    tracks: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="shelfrip-subs-") as tmpname:
        tmp = Path(tmpname)
        for u in todo:
            key = str(u["video"])
            if key not in tracks:
                tracks[key] = load_track(u["video"], cfg, want, tmp, log) or {}
            tr = tracks[key]
            row = {"title": u["label"], "planned": u["item"].get("name"), "episode": u["episode"],
                   "action": u["item"].get("action")}
            report["titles"].append(row)
            u["row"] = row
            if not tr.get("cues"):
                row["status"] = "no subtitles"
                u["text"] = ""
                continue
            row.update(track=f"{tr['kind']} {tr['lang']}", lines=len(tr["cues"]))
            sample = pick(tr["cues"], u["ranges"], samples, EDGE)
            t0 = time.time()
            twin = next((v for v in todo if v.get("text") and v["ranges"] == u["ranges"]
                         and same_timing(tr["cues"], tracks[str(v["video"])].get("cues") or []) >= 0.95), None)
            if twin:                      # an exact copy of a title already read: reuse its text
                u["text"] = twin["text"]
            elif tr["kind"] == "text":
                u["text"] = "\n".join(c["text"] for c in sample)
            elif exe:
                lang = TESS_LANG.get(tr["lang"], "eng")
                lang = lang if lang in langs else "eng"
                u["text"] = ocr(sample, exe, lang, tmp, 2 if tr["kind"] == "vobsub" else 1)
            else:
                u["text"] = ""
                row["status"] = "picture subtitles, Tesseract not installed"
            row["ocr_seconds"] = round(time.time() - t0, 1)
            row["sample"] = " / ".join(ln.strip() for ln in u["text"].splitlines() if ln.strip())[:300]
            row["text"] = u["text"]   # the whole OCR result, kept while the scoring is being tuned
        # ---- timing: split points, and copies
        for it in plan.get("items", []):
            sp = it.get("split") if it.get("needs_split") else None
            cues = tracks.get(str(folder / it.get("source", "")), {}).get("cues")
            if sp and cues:
                ch, n = sp["chapters"], len(sp["chapters"])
                for k in sp["starts"][1:]:
                    if k < n:
                        c = cut_check(cues, ch[k])
                        if c["inside_line"]:
                            report["findings"].append(f"{it['source']}: the cut at {hms(c['at'])} lands in the "
                                                      f"middle of a line of dialogue. Check that split point.")
                        log(f"   cut {hms(c['at'])} in {it['source']}: "
                            + ("INSIDE a line of dialogue" if c["inside_line"] else
                               f"quiet {c['quiet_before']}s before, {c['quiet_after']}s after"))
        vids = [u for u in todo if tracks.get(str(u["video"]), {}).get("cues") and u["label"] == u["item"].get("source")]
        for u in vids:
            it = u["item"]
            # a title skipped because Plex already has it isn't a copy of another title on this disc,
            # so comparing it with them only gave false alarms (Fairy Tail Part 6, 2026-11-27)
            if it.get("action") == "skip" and not it.get("library_copy") and \
                    re.search(r"copy|duplicate|same length", it.get("note") or "", re.I):
                mine = tracks[str(u["video"])]["cues"]
                others = [o for o in vids if o is not u and o["item"].get("action") != "skip"] or \
                    [o for o in vids if o is not u]
                sims = [(same_timing(mine, tracks[str(o["video"])]["cues"]), o["label"]) for o in others]
                best = max(sims, default=(0.0, ""))
                u["row"]["copy_of"] = {"title": best[1], "timing_match": round(best[0], 2)}
                if best[0] < 0.5:
                    report["findings"].append(f"{it['source']} is set to Skip as a copy, but its subtitles don't "
                                              f"match any other title (best {best[0]:.0%}, {best[1]}). It's probably "
                                              f"a different episode.")
                else:
                    log(f"   {it['source']}: really is a copy of {best[1]} ({best[0]:.0%} of subtitles line up)")
        # ---- words: compare with TMDB
        if tmdb and plan.get("tmdb_id"):
            try:
                if plan.get("kind") == "tv":
                    _judge_tv(plan, todo, tmdb, report, log, cfg, imported)
                else:
                    _judge_movie(plan, todo, tmdb, report, log)
            except Exception as exc:
                log(f"   TMDB comparison skipped: {exc}")
        elif not tmdb:
            log("   (no TMDB key, so only timing was checked)")
    report["seconds"] = round(time.time() - t_start, 1)
    for f in report["findings"]:
        log(f"   !! {f}")
    if not report["findings"]:
        log("   No problems found.")
    log(f"   ({report['seconds']}s for {len(tracks)} video(s))")
    return report


def _judge_tv(plan, todo, tmdb, report, log, cfg: dict | None = None, imported: bool = False):
    """Which episode is each video? Every video is scored against every episode of the season
    (plot-summary words, and dialogue against downloaded subtitles when that's turned on), then
    clip shows are matched first and the rest paired one-to-one for the best total (epmatch)."""
    import epmatch
    cfg = cfg or {}
    season = int(plan.get("season") or 1)
    profs = episode_profiles(tmdb, plan["tmdb_id"], season)
    if not profs:
        log("   TMDB has no episode list for this season")
        return
    weight = weigh(profs)
    report["tmdb"] = {str(k): v["raw"] for k, v in profs.items()}
    clip_eps = {e for e, p in profs.items() if epmatch.is_clip_show(p["raw"]["title"], p["raw"]["overview"])}
    if clip_eps:
        report["clip_show_episodes"] = sorted(clip_eps)
    eps = [u for u in todo if u.get("text") and u["episode"] is not None]
    kept = [k for k, u in enumerate(eps) if u["item"].get("action") != "skip"]
    for u in eps:
        u["m"] = Matcher(u["text"])
    damp = common_on_disc([eps[k]["m"] for k in kept], profs)
    table: dict[int, dict[int, tuple[float, list[str]]]] = {}
    for idx, u in enumerate(eps):
        table[idx] = {e: score(u["m"], p, weight, damp) for e, p in profs.items()}
    combined = {idx: {e: v[0] for e, v in table[idx].items()} for idx in table}

    # ---- dialogue against reference subtitles (optional, OpenSubtitles)
    ref_share: dict[int, dict[int, float]] = {}
    if cfg.get("reference_subtitles") and kept:
        import refsubs
        langs = [str(u["row"].get("track", "")).split(" ")[-1] for u in eps if u["row"].get("track")]
        lang = max(set(langs), key=langs.count) if langs else "eng"
        want = list(profs) if imported else refsubs.wanted_episodes(
            [eps[k]["episode"] for k in kept], list(profs), clip_eps)
        try:
            import paths
            texts = refsubs.fetch(cfg, plan["tmdb_id"], season, want, lang, log,
                                  limit=int(cfg.get("reference_subtitles_per_disc") or 10),
                                  version=getattr(paths, "APP_VERSION", ""))
        except Exception as exc:
            log(f"   reference subtitles skipped: {exc}")
            texts = {}
        refs = {e: epmatch.shingles(epmatch.srt_text(t)) for e, t in texts.items()}
        bad = epmatch.mislabeled(refs)
        for e in bad:
            refs.pop(e, None)
        report["reference"] = {"source": "OpenSubtitles.com", "language": lang, "episodes": sorted(refs),
                               "dropped_as_mislabeled": sorted(bad)}
        if refs:
            for idx, u in enumerate(eps):
                rs = epmatch.reference_scores(u["text"], refs)
                sample = epmatch.shingles(u["text"])
                ref_share[idx] = {e: epmatch.containment(sample, r) for e, r in refs.items()}
                for e in combined[idx]:
                    if e in rs:
                        combined[idx][e] = rs[e] + 0.3 * combined[idx][e]
            log(f"   compared dialogue with OpenSubtitles for {len(refs)} episode(s)")

    # ---- the whole disc at once
    planned_of = {k: eps[k]["episode"] for k in kept}
    result = epmatch.assign({k: combined[k] for k in kept}, planned_of, clip_eps,
                            prior=0.0 if imported else 0.15)
    for idx, u in enumerate(eps):
        row, planned = u["row"], u["episode"]
        sc = combined[idx]
        ranked = sorted(sc, key=lambda e: -sc[e])
        res = result.get(idx) or {"episode": ranked[0], "clip_show": False}
        best = res["episode"] if res["episode"] is not None else ranked[0]
        sp, sb = sc.get(planned, 0.0), sc.get(best, 0.0)
        second = max((v for e, v in sc.items() if e != best), default=0.0)
        by_ref = idx in ref_share and best in ref_share[idx]
        words = table[idx].get(best, (0, []))[1]
        row.update(planned_score=round(sp, 2), best_episode=best, best_score=round(sb, 2),
                   best_words=words[:8], planned_words=table[idx].get(planned, (0, []))[1][:8],
                   best_name=profs.get(best, {}).get("name", ""), clip_show=bool(res.get("clip_show")),
                   source=u["item"].get("source"), part=u.get("part"), by_reference=by_ref)
        if by_ref:
            row["reference_share"] = round(ref_share[idx].get(best, 0.0), 3)
            row["planned_reference_share"] = round(ref_share[idx].get(planned, 0.0), 3)
        if row.get("copy_of", {}).get("timing_match", 0) >= 0.5:
            row["verdict"] = f"copy of {row['copy_of']['title']}"
        elif u["item"].get("action") == "skip":
            row["verdict"] = "fits" if best == planned and sb > 0 else "unsure"
        elif best == planned and sb > 0 and (sb >= 1.3 * second or res.get("clip_show")):
            row["verdict"] = "fits"
        elif best != planned and sb >= 1.5 * max(sp, 0.01) and (by_ref or len(words) >= 2):
            row["verdict"] = f"looks like e{best:02d}"
        else:
            row["verdict"] = "unsure"
        if res.get("clip_show") and not row["verdict"].startswith("copy"):
            row["verdict"] += " (clip show)"
        why = (f"{row.get('reference_share', 0):.0%} of lines in its subtitles" if by_ref
               else ", ".join(words[:6]))
        log(f"   {u['label']}: planned e{planned:02d} ({profs.get(planned, {}).get('name', '?')}) score {sp:.2f}"
            f" | matched e{best:02d} ({profs.get(best, {}).get('name', '?')}) {sb:.2f} [{why}]"
            f" -> {row['verdict']}")
    # The disc holds a run of episodes: does the whole run fit better shifted?
    if len(kept) >= 2:   # titles set to Skip aren't part of the run
        planned = [eps[k]["episode"] for k in kept]
        lo, hi = min(profs), max(profs)
        totals = {}
        for d in range(lo - min(planned), hi - max(planned) + 1):
            totals[d] = sum(combined[k].get(p + d, 0.0) for k, p in zip(kept, planned))
        best_d = max(totals, key=totals.get)
        report["run"] = {"planned_total": round(totals.get(0, 0), 2), "best_shift": best_d,
                         "best_total": round(totals[best_d], 2)}
        if best_d != 0 and totals[best_d] >= 1.4 * max(totals.get(0, 0), 0.01):
            report["findings"].append(
                f"The episodes look shifted: subtitles fit e{min(planned) + best_d:02d}-e{max(planned) + best_d:02d} "
                f"better than the planned e{min(planned):02d}-e{max(planned):02d} "
                f"(score {totals[best_d]:.1f} vs {totals.get(0, 0):.1f}).")
    for u in eps:
        r = u["row"]
        if r.get("verdict", "").startswith("looks like") and u["item"].get("action") != "skip":
            why = (f"{r['reference_share']:.0%} of its lines match that episode's subtitles"
                   if r.get("by_reference") else ", ".join(r["best_words"][:5]))
            report["findings"].append(f"{u['label']} is planned as e{u['episode']:02d} but its dialogue fits "
                                      f"e{r['best_episode']:02d} {r['best_name']} better ({why}).")


def _judge_movie(plan, todo, tmdb, report, log):
    cands = {plan["tmdb_id"]: movie_profile(tmdb, plan["tmdb_id"])}
    for c in plan.get("tmdb_candidates") or []:
        if str(c.get("id")) != str(plan["tmdb_id"]) and len(cands) < 4:
            try:
                cands[str(c["id"])] = movie_profile(tmdb, c["id"])
            except Exception:
                pass
    # Could it be a TV series instead (a feature-length pilot, say)?
    q = plan.get("search_query") or plan.get("title") or ""
    try:
        shows = tmdb.search("tv", q)[:1] or tmdb.search("tv", plan.get("title") or "")[:1]
    except Exception:
        shows = []
    for s in shows:
        cands["tv:" + str(s["id"])] = show_profile(tmdb, s["id"])
    weight = weigh(cands)
    report["tmdb"] = {v["name"]: v["raw"] for v in cands.values()}
    for u in todo:
        if not u.get("text"):
            continue
        m = Matcher(u["text"])
        sc = {k: score(m, p, weight) for k, p in cands.items()}
        best = max(sc, key=lambda k: sc[k][0])
        mine = sc[str(plan["tmdb_id"])]
        row = u["row"]
        row.update(planned_score=round(mine[0], 2), planned_words=mine[1][:8], best=cands[best]["name"],
                   best_score=round(sc[best][0], 2), best_words=sc[best][1][:8],
                   others={cands[k]["name"]: round(v[0], 2) for k, v in sc.items()})
        is_main = u["item"].get("role") == "main"
        log(f"   {u['label']} ({'main feature' if is_main else 'long extra'}): "
            + "; ".join(f"{cands[k]['name']} {v[0]:.2f} [{', '.join(v[1][:6])}]" for k, v in sc.items()))
        if best != str(plan["tmdb_id"]) and sc[best][0] >= 1.5 * max(mine[0], 0.01) and len(sc[best][1]) >= 2:
            row["verdict"] = f"looks like {cands[best]['name']}"
            what = "The main feature" if is_main else f"The long extra {u['label']}"
            report["findings"].append(f"{what} fits {cands[best]['name']} better than {cands[str(plan['tmdb_id'])]['name']} "
                                      f"({', '.join(sc[best][1][:5])}).")
        else:
            row["verdict"] = "fits" if best == str(plan["tmdb_id"]) and mine[0] > 0 else "unsure"
        u["fit"] = mine[0]
    # Another cut of the movie hiding among the extras (theatrical vs extended), and a length
    # warning that the dialogue settles.
    mains = [u for u in todo if u["item"].get("role") == "main" and u.get("row", {}).get("verdict") == "fits"]
    if not mains:
        return
    main = mains[0]
    name = cands[str(plan["tmdb_id"])]["name"]
    if any("wrong movie" in w or "Check it's the right movie" in w for w in plan.get("warnings") or []):
        report["findings"].append(f"The dialogue confirms the main feature is {name}, so its unexpected length is "
                                  f"most likely a different cut (extended, director's), not a different film.")
    mlen = float(main["item"].get("duration_seconds") or 0)
    for u in todo:
        it = u["item"]
        if u is main or it.get("role") == "main" or not u.get("fit"):
            continue
        if float(it.get("duration_seconds") or 0) >= 0.6 * mlen and u["fit"] >= 0.6 * main["fit"] \
                and u["row"].get("verdict") == "fits":
            what = "set to Skip" if it.get("action") == "skip" else f"filed as the extra \"{it.get('name')}\""
            report["findings"].append(f"{u['label']} ({hms(it.get('duration_seconds') or 0)}) is {what}, but its "
                                      f"dialogue is {name}'s. It's probably another cut of the movie "
                                      f"(theatrical or extended) rather than an extra or a Play All.")


# --------------------------------------------------------------------------- command line

def main() -> int:
    ap = argparse.ArgumentParser(description="Check rips against their subtitles (reads only).")
    ap.add_argument("folders", nargs="*", help="rip folders (default: every unfiled folder in your rip folder)")
    ap.add_argument("--filed", metavar="TEXT", help="check filed discs whose folder name contains TEXT ('*' = all)")
    ap.add_argument("--samples", type=int, default=SAMPLES, help="subtitle lines to read per title")
    args = ap.parse_args()

    import identify
    cfg = identify.load_config()
    tmdb = identify.tmdb_for(cfg)
    import split
    if not split.available(cfg):
        print("MKVToolNix isn't installed (or mkvtoolnix_dir in config.json is wrong). It's needed to read subtitles.")
        return 1
    exe = tesseract(cfg)
    langs = tess_langs(exe) if exe else set()
    if not exe:
        print(f"Tesseract OCR isn't installed, so only captions and subtitle timing can be checked.\n"
              f"Install it with:  winget install UB-Mannheim.TesseractOCR   ({TESSERACT_URL})")
    root = Path(cfg.get("output_root") or ".")
    folders = [Path(f) for f in args.folders]
    if args.filed:
        folders += [p for p in sorted((root / "_Filed").iterdir()) if p.is_dir() and (p / "plan.json").exists()
                    and (args.filed == "*" or args.filed.lower() in p.name.lower())]
    if not folders:
        folders = [p for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith("_")
                   and (p / "plan.json").exists()]
    if not folders:
        print(f"No rip folders with a plan.json in {root}")
        return 1
    for f in folders:
        try:
            rep = check_folder(f, cfg, tmdb, exe, langs, samples=args.samples)
            (f / "subcheck.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            import traceback
            print(f"\n== {f.name}: check failed: {exc}")
            traceback.print_exc()
    return 0


if __name__ == "__main__":
    sys.exit(main())
