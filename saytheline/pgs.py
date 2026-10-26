"""Blu-ray (PGS) subtitles -> text, by OCR. Many discs carry English only as PGS: pictures of the
lines, which players draw but nothing can search. This reads a .sup stream (ffmpeg -c copy of the
track), draws each line as black text on white and runs tesseract over all of them at once.

PGS is a run of segments: "PG", PTS (90 kHz), DTS, type, size, data. A presentation (PCS) with
objects starts a line at its PTS; the next one without objects ends it. Palettes (PDS) are
Y Cr Cb A; objects (ODS) are run-length coded bitmaps, possibly split over several segments."""
import os
import re
import struct
import subprocess
import tempfile

from . import config

try:
    from PIL import Image, ImageOps
except ImportError:                 # optional, like the Poster look: no OCR without it
    Image = ImageOps = None

TESSERACT = config.tesseract()
PAD = 24                       # white border around each line: tesseract reads edges badly
MIN_GAP_MS = 40
WORKERS = 4                    # tesseract runs at once (the box's thermal rule: keep it modest)


def available():
    return bool(TESSERACT and Image)


def _rle(data, w, h):
    """The bitmap's palette indexes, row by row."""
    out, row, i, n = bytearray(), 0, 0, len(data)
    line = bytearray()
    while i < n and row < h:
        b = data[i]; i += 1
        if b:
            line.append(b)
            continue
        b2 = data[i]; i += 1
        if b2 == 0:                                   # end of line
            out += line[:w].ljust(w, b"\0"); line = bytearray(); row += 1
            continue
        flag = b2 & 0xC0
        ln = b2 & 0x3F
        if flag & 0x40:
            ln = (ln << 8) | data[i]; i += 1
        colour = 0
        if flag & 0x80:
            colour = data[i]; i += 1
        line += bytes([colour]) * ln
    while row < h:
        out += line[:w].ljust(w, b"\0"); line = bytearray(); row += 1
    return bytes(out)


def read_sup(path):
    """[(start_ms, end_ms, [(x, y, PIL image)])]: each shown line with its pictures."""
    with open(path, "rb") as f:
        buf = f.read()
    i, n = 0, len(buf)
    palettes, objects, pending = {}, {}, {}
    shown, cur = [], None                 # cur: (start_ms, [(object_id, x, y)], palette_id)
    comp = None
    while i + 13 <= n:
        if buf[i:i + 2] != b"PG":
            i += 1
            continue
        pts = struct.unpack(">I", buf[i + 2:i + 6])[0] // 90
        kind, size = buf[i + 10], struct.unpack(">H", buf[i + 11:i + 13])[0]
        d = buf[i + 13:i + 13 + size]
        i += 13 + size
        if kind == 0x16 and len(d) >= 11:                         # presentation
            count = d[10]
            refs, j = [], 11
            for _ in range(count):
                oid, _win, crop, x, y = struct.unpack(">HBBHH", d[j:j + 8]); j += 8
                if crop & 0x80:
                    j += 8
                refs.append((oid, x, y))
            comp = (pts, refs, d[9])
            if cur and (count == 0 or refs != cur[1]):
                shown.append((cur, pts))
                cur = None
        elif kind == 0x14 and len(d) >= 2:                        # palette
            pal = palettes.setdefault(d[0], {})
            for j in range(2, len(d) - 4, 5):
                pal[d[j]] = tuple(d[j + 1:j + 5])                  # Y, Cr, Cb, A
        elif kind == 0x15 and len(d) >= 4:                        # object (maybe in parts)
            oid, seq = struct.unpack(">H", d[:2])[0], d[3]
            if seq & 0x80:
                w, h = struct.unpack(">HH", d[7:11])
                pending[oid] = [w, h, bytearray(d[11:])]
            elif oid in pending:
                pending[oid][2] += d[4:]
            if seq & 0x40 and oid in pending:
                w, h, data = pending.pop(oid)
                objects[oid] = (w, h, bytes(data))
        elif kind == 0x80 and comp:                               # end of a display set
            start, refs, pid = comp
            comp = None
            if refs and cur is None:
                pal = dict(palettes.get(pid, {}))
                pics = []
                for oid, x, y in refs:
                    if oid in objects:
                        w, h, data = objects[oid]
                        pics.append((x, y, _picture(_rle(data, w, h), w, h, pal)))
                if pics:
                    cur = (start, refs, pics)
    out = []
    for (start, _refs, pics), end in shown:
        if end - start >= MIN_GAP_MS:
            out.append((start, end, pics))
    return out


def _picture(idx, w, h, pal):
    """Black text on white: each pixel's brightness times its opacity, inverted, so the white
    letters turn black and their dark outline and the clear background stay white."""
    lut = bytes(255 - (pal[k][0] * pal[k][3] // 255) if k in pal else 255 for k in range(256))
    return Image.frombytes("L", (w, h), idx.translate(lut))


def _sheet(pics):
    """One line's pictures (usually one, sometimes two rows) as one image, in place."""
    x0, y0 = min(x for x, _, _ in pics), min(y for _, y, _ in pics)
    W = max(x + p.width for x, _, p in pics) - x0
    H = max(y + p.height for _, y, p in pics) - y0
    im = Image.new("L", (W + 2 * PAD, H + 2 * PAD), 255)
    for x, y, p in pics:
        im.paste(p, (x - x0 + PAD, y - y0 + PAD))
    return ImageOps.autocontrast(im)               # grey or half-clear text to full black


# what tesseract makes of a ♪ at either end of a sung line ("J City of stars I")
_NOTE = r"(?:J|f|ff|ft|Jf|fJ|d|<|¢|£|&|I|\?|♪|♫)"
_SUNG = re.compile(rf"^(- )?({_NOTE})\s+(.*?)(?:\s+({_NOTE}))?$")


def _notes(line):
    """Stray marks at a sung line's ends back to ♪. A lone "I" counts only when the other end is
    a note too, so "Neither do I" stays as it is."""
    if re.fullmatch(_NOTE, line) and line not in ("I", "d", "&"):
        return "♪"
    m = _SUNG.match(line)
    if not m:
        m2 = re.match(rf"^(- )?(.*?)\s+((?:J|f|ff|ft|Jf|<|¢|£|♪|♫))$", line)   # a note only at the end
        return f"{m2.group(1) or ''}{m2.group(2)} ♪" if m2 and m2.group(2) else line
    dash, start, body, end = m.groups()
    if start == "I" and not end or start in ("d", "&") and not end:
        return line
    return f"{dash or ''}♪ {body}" + (" ♪" if end else "")


def _clean(t):
    """Tesseract's text for one line, tidied: straight quotes, | as I, notes as ♪."""
    t = t.replace("|", "I").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    lines = [l for l in (_notes(re.sub(r"\s+", " ", l).strip()) for l in t.splitlines()) if l]
    # a sung line over two rows: the ♪ that opens the first closes the last
    if len(lines) > 1 and re.match(r"^(- )?♪ ", lines[0]) and re.search(r"\s(I|J|f|<|¢)$", lines[-1]):
        lines[-1] = lines[-1][:-1] + "♪"
    return "\n".join(lines)


def to_srt(sup_path, lang="eng"):
    """The track as SRT text, read by tesseract. Raises RuntimeError without tesseract."""
    _need()
    return ocr(read_sup(sup_path), lang)


def _need():
    if not available():
        raise RuntimeError("picture subtitles need tesseract (apt install tesseract-ocr tesseract-ocr-eng) and pillow")


def ocr(subs, lang="eng"):
    """[(start_ms, end_ms, [(x, y, picture)])] (from read_sup, or vobsub.read_track) as SRT text."""
    _need()
    if not subs:
        return ""
    with tempfile.TemporaryDirectory(prefix="saytheline-ocr-") as tmp:
        names = []
        for k, (_, _, pics) in enumerate(subs):
            p = os.path.join(tmp, f"{k:05d}.png")
            _sheet(pics).save(p)
            names.append(p)
        # a few tesseract runs side by side, one thread each (its own threading barely helps on
        # small images); each returns its pages separated by form feeds
        k = max(1, min(WORKERS, len(names) // 50 or 1))
        size = -(-len(names) // k)
        runs = []
        for j in range(k):
            lst = os.path.join(tmp, f"list{j}.txt")
            with open(lst, "w", encoding="utf-8") as f:
                f.write("\n".join(names[j * size:(j + 1) * size]) + "\n")
            runs.append(subprocess.Popen([TESSERACT, lst, "stdout", "-l", lang, "--psm", "6"],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, encoding="utf-8", errors="replace",     # not the locale's (cp1252 on Windows)
                                         env={**os.environ, "OMP_THREAD_LIMIT": "1"}))
        pages = []
        for j, r in enumerate(runs):
            out_, err = r.communicate(timeout=3600)
            if r.returncode != 0:
                raise RuntimeError("tesseract failed: " + err.strip()[-300:])
            got = out_.split("\f")
            pages += got[:len(names[j * size:(j + 1) * size])]
    out, k = [], 0
    for (a, b, _), text in zip(subs, pages):
        text = _clean(text)
        if not text:
            continue
        k += 1
        out.append(f"{k}\n{_ts(a)} --> {_ts(b)}\n{text}\n")
    return "\n".join(out)


def _ts(ms):
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
