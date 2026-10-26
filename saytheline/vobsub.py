"""DVD (VobSub) subtitles -> text, by OCR, like pgs.py does for Blu-ray. The track is copied out on
its own into a small .mkv (ffmpeg -c copy); ffprobe gives each packet's time and size and the
16-colour palette (the track's .idx text, kept as Matroska codec private data), and each packet is
one subpicture (SPU):

  size, offset of the control sequences, then the 2-bit run-length coded picture, top field
  (even rows) and bottom field (odd rows) apart. Control sequences: delay (1024/90000 s), offset
  of the next, commands: 01 show, 02 hide, 03 colours (4 palette indexes), 04 alphas (0-15),
  05 area (x1 x2 y1 y2, 12 bits each), 06 field offsets, FF end."""
import json
import re
import struct
import subprocess

from . import pgs

# no palette in the track: the usual DVD layout (clear, white letters, black edge, grey anti-alias)
DEFAULT_LUMA = [0, 255, 0, 128]


def _probe(mkv):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_data", "-of", "json", mkv],
                       capture_output=True, text=True, timeout=300)
    st = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
    raw = bytes.fromhex("".join(re.sub(r"\s", "", l[10:50]) for l in (st.get("extradata") or "").splitlines()
                                if re.match(r"[0-9a-f]{8}:", l)))
    m = re.search(r"palette:\s*([0-9a-fA-F, ]+)", raw.decode("latin-1"))
    palette = [int(x, 16) for x in m.group(1).replace(" ", "").split(",") if x][:16] if m else None
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "packet=pts_time,duration_time,size",
                        "-of", "csv=p=0", mkv], capture_output=True, text=True, timeout=300)
    packets = []
    for line in r.stdout.split():
        pts, dur, size = (line.split(",") + ["", "", ""])[:3]
        if pts and size:
            packets.append((round(float(pts) * 1000), round(float(dur) * 1000) if dur not in ("", "N/A") else None, int(size)))
    return palette, packets


def _luma(rgb):
    return (299 * (rgb >> 16 & 255) + 587 * (rgb >> 8 & 255) + 114 * (rgb & 255)) // 1000


def _field(p, start, end, w, rows):
    """One field's rows of 2-bit colours. Each run is 1-4 nibbles: count << 2 | colour, a count
    of 0 filling the rest of the row; rows start on a byte."""
    pos, end, out = start * 2, min(end, len(p)) * 2, []
    for _ in range(rows):
        line = bytearray()
        while len(line) < w and pos < end:
            v, k = 0, 0
            while True:
                b = p[pos >> 1]
                v = v << 4 | (b >> 4 if not pos & 1 else b & 15)
                pos += 1
                k += 1
                if k == 4 or v >= (4, 0x10, 0x40)[k - 1] or pos >= end:
                    break
            run = v >> 2 or w - len(line)
            line += bytes([v & 3]) * min(run, w - len(line))
        pos += pos & 1
        out.append(bytes(line.ljust(w, b"\0")))
    return out


def _spu(p):
    """(show ms, hide ms or None, x, y, w, h, 4 colours, 4 alphas, top offset, bottom offset)."""
    if len(p) < 4:
        return None
    ctrl = struct.unpack(">H", p[2:4])[0]
    show = hide = area = offs = None
    cols, alphas = [0, 1, 2, 3], [0, 15, 15, 15]
    i, seen = ctrl, set()
    while i + 4 <= len(p) and i not in seen:
        seen.add(i)
        delay, nxt = struct.unpack(">HH", p[i:i + 4])
        t, j = delay * 1024 // 90, i + 4
        while j < len(p):
            c = p[j]; j += 1
            if c in (0x00, 0x01):
                show = t if show is None else show
            elif c == 0x02:
                hide = t
            elif c in (0x03, 0x04):
                n = [p[j + 1] & 15, p[j + 1] >> 4, p[j] & 15, p[j] >> 4]       # colour 0..3
                if c == 0x03:
                    cols = n
                else:
                    alphas = n
                j += 2
            elif c == 0x05:
                b = p[j:j + 6]; j += 6
                area = (b[0] << 4 | b[1] >> 4, (b[1] & 15) << 8 | b[2], b[3] << 4 | b[4] >> 4, (b[4] & 15) << 8 | b[5])
            elif c == 0x06:
                offs = struct.unpack(">HH", p[j:j + 4]); j += 4
            else:                                     # FF, or something unknown: this sequence ends
                break
        if nxt == i:
            break
        i = nxt
    if not area or not offs:
        return None
    x1, x2, y1, y2 = area
    return show or 0, hide, x1, y1, x2 - x1 + 1, y2 - y1 + 1, cols, alphas, offs[0], offs[1], ctrl


def _picture(p, spu, palette):
    _, _, _, _, w, h, cols, alphas, top, bottom, ctrl = spu
    rows_top = _field(p, top, bottom, w, (h + 1) // 2)
    rows_bot = _field(p, bottom, ctrl, w, h // 2)
    rows = [r for pair in zip(rows_top, rows_bot) for r in pair] + rows_top[len(rows_bot):]
    luma = [_luma(palette[c]) if palette and c < len(palette) else DEFAULT_LUMA[k] for k, c in enumerate(cols)]
    # as pgs.py: brightness times opacity, inverted, so white letters come out black on white
    lut = bytes(255 - luma[k & 3] * (alphas[k & 3] * 17) // 255 for k in range(256))
    return pgs.Image.frombytes("L", (w, h), b"".join(rows).translate(lut))


def read_track(mkv):
    """[(start_ms, end_ms, [(x, y, PIL image)])], as pgs.read_sup gives."""
    palette, packets = _probe(mkv)
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", mkv, "-map", "0", "-c", "copy", "-f", "data", "-"],
                       capture_output=True, timeout=300)
    data, pos, out = r.stdout, 0, []
    for k, (pts, dur, size) in enumerate(packets):
        p = data[pos:pos + size]
        pos += size
        spu = _spu(p)
        if not spu or spu[4] <= 0 or spu[5] <= 0:
            continue
        start = pts + spu[0]
        end = pts + spu[1] if spu[1] else pts + dur if dur else packets[k + 1][0] if k + 1 < len(packets) else start + 3000
        if k + 1 < len(packets):
            end = min(end, packets[k + 1][0])
        if end - start >= pgs.MIN_GAP_MS:
            out.append((start, end, [(spu[2], spu[3], _picture(p, spu, palette))]))
    return out


def to_srt(mkv, lang="eng"):
    pgs._need()
    return pgs.ocr(read_track(mkv), lang)
