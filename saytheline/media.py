"""Every ffmpeg recipe in one place: preview windows and final renders."""
import functools
import json
import os
import re
import shutil
import subprocess
import tempfile

from . import config

VAAPI = config.vaapi()          # None = encode previews on the CPU

ASPECTS = {"16:9": 16 / 9, "4:3": 4 / 3, "1:1": 1.0, "9:16": 9 / 16}
MIN_CROP = 0.25              # the crop window can shrink to a quarter of the largest that fits
# Caption size steps, each about 1.4x the one before (XS is half of M, XL twice it), and where a
# caption block sits. Every look uses these, and the editor's preview the same numbers (= JS).
SIZE_STEPS = {"xs": 0.5, "s": 0.71, "m": 1.0, "l": 1.41, "xl": 2.0}
POSITIONS = ("top", "upper", "middle", "lower", "bottom")
EDGE, THIRD = 0.05, 0.25          # top / bottom: this far in from the edge; upper / lower: this far in
# libass sizes SRT text against a 288-line script, so these are relative to the output height.
SIZES = {k: round(20 * v, 1) for k, v in SIZE_STEPS.items()}
COLOURS = {"white": "FFFFFF", "yellow": "FFE14D", "cyan": "6BE4FF", "black": "000000"}
FONTS = ["Inter", "Inter Black", "League Spartan Black", "Liberation Sans", "Lato", "DejaVu Sans",
         "Comfortaa", "Departure Mono"]
# Shipped in saytheline/fonts (open licences, see fonts/licenses), so captions look the same on any
# machine; libass is pointed at that folder and the browser preview loads the same files.
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
FONT_FILES = {"Inter": "Inter-Bold.otf", "Inter Black": "Inter-Black.otf",
              "League Spartan Black": "LeagueSpartan-Black.otf", "Liberation Sans": "LiberationSans-Bold.ttf",
              "Lato": "Lato-Bold.ttf", "DejaVu Sans": "DejaVuSans-Bold.ttf", "Comfortaa": "Comfortaa-Bold.ttf",
              "Departure Mono": "DepartureMono-Regular.otf"}
HIGHLIGHTS = {"yellow": "FFE14D", "green": "3CFF6A", "cyan": "6BE4FF", "pink": "FF4FA3", "orange": "FF9A1F"}
# TikTok-style size = this fraction of the output's short side (cap height reads ~like CapCut's)
TT_SIZES = {k: 0.095 * v for k, v in SIZE_STEPS.items()}


def block_top(pos, block_h, h):
    """Where a caption block of height block_h starts (y of its top edge) in a frame h tall: Top and
    Bottom near the edges, Upper and Lower centred a quarter of the way in, Middle centred."""
    y = {"top": h * EDGE, "upper": h * THIRD - block_h / 2, "lower": h * (1 - THIRD) - block_h / 2,
         "bottom": h * (1 - EDGE) - block_h}.get(pos, (h - block_h) / 2)
    return max(h * 0.02, min(y, h * 0.98 - block_h))


def fill(size):
    """How much of the frame (width, height) a laid-out look (Poster, Kinetic, Intense) may fill:
    its words are scaled to fill this, so the size step sets it rather than the font size alone."""
    return {"xs": (0.45, 0.3), "s": (0.65, 0.42), "m": (0.86, 0.55), "l": (0.93, 0.7), "xl": (0.97, 0.86)}.get(size, (0.86, 0.55))


DEMUCS = config.demucs()        # optional: enables the Dialogue only / No dialogue audio options
AUDIO_MODES = ("original", "dialogue", "nodialogue")


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,width,height:format=duration", "-of", "json", path],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    d = json.loads(r.stdout or "{}")
    streams = d.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    return dict(width=v.get("width"), height=v.get("height"),
                has_audio=any(s.get("codec_type") == "audio" for s in streams),
                duration=float((d.get("format") or {}).get("duration") or 0))


UNTAGGED = ("", "und", "unk", "mis", "zxx")


def _english(tag):
    return tag.split("-")[0] in ("en", "eng")


@functools.lru_cache(maxsize=4096)
def audio_tracks(path):
    """Language tags of the file's audio tracks, in order ("" = untagged)."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                            "stream_tags=language", "-of", "json", path], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        return tuple(((s.get("tags") or {}).get("language") or "").lower()
                     for s in json.loads(r.stdout or "{}").get("streams") or [])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ()


def audio_track(path):
    """The audio track to use (its number among the audio tracks): the first English one, else the
    first. Some releases put a dub ahead of the original (Portuguese, then English)."""
    return next((i for i, t in enumerate(audio_tracks(path)) if _english(t)), 0)


def foreign_audio(path):
    """The track used is tagged as a language other than English, so speech-to-text (English only)
    can't check it: the subtitle timing stands. Untagged tracks are assumed to be English."""
    tags = audio_tracks(path)
    tag = tags[audio_track(path)] if tags else ""
    return None if tag in UNTAGGED or _english(tag) else tag


def font_path(family="Inter"):
    p = os.path.join(FONT_DIR, FONT_FILES.get(family, ""))
    if FONT_FILES.get(family) and os.path.isfile(p):
        return p
    try:                       # not shipped: whatever fontconfig has
        r = subprocess.run(["fc-match", "--format=%{file}", f"{family}:bold"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        return r.stdout.strip()
    except OSError:
        return ""


@functools.lru_cache(maxsize=64)
def font_box(family):
    """(ascent, descent) of a font per pixel of em size."""
    try:
        from PIL import ImageFont              # only the caption looks need Pillow
        a, d = ImageFont.truetype(font_path(family), 1000).getmetrics()
        return a / 1000, d / 1000
    except Exception:
        return 0.82, 0.18                     # sums to 1: sizes pass through unchanged


def ass_size(family, size):
    """The ASS font size that draws text `size` px to the em, as the preview (CSS) and the layout
    (Pillow) measure it. libass, like VSFilter, sizes a font by its ascent + descent instead, so the
    same number came out about 18% smaller in renders than in the editor (Inter: 1.21 em)."""
    a, d = font_box(family)
    return round(size * (a + d), 1)


def _fpath(p):
    """A path as an option value inside a filter graph (-vf / -filter_complex). ffmpeg unescapes it
    twice: the graph parser first (\\ ' [ ] , ;), then the filter's option parser (\\ : '), so it is
    escaped for both, option level first. A Windows drive colon (D:) needs both levels."""
    p = re.sub(r"([\\:'])", r"\\\1", p.replace("\\", "/"))
    return re.sub(r"([\\'\[\],;])", r"\\\1", p)


def preview(src, start_ms, end_ms, out):
    """A short browser-playable window (H.264/AAC, 480p, faststart). VAAPI encode keeps it light;
    falls back to x264 ultrafast if the GPU path fails. Input-side -ss with a re-encode is frame
    accurate, so t=0 in the preview is exactly start_ms. The 90 kHz time base keeps each frame's own
    time: at the default (one tick per frame) a 23.976 fps source's frames would shift by up to a
    frame, and the editor could not show the exact first and last frame of a cut."""
    dur = (end_ms - start_ms) / 1000
    common_in = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{start_ms / 1000:.3f}"]
    common_out = ["-t", f"{dur:.3f}", "-enc_time_base:v", "1:90000", "-video_track_timescale", "90000",
                  "-map", "0:v:0", "-map", f"0:a:{audio_track(src)}?", "-c:a", "aac", "-b:a", "128k",
                  "-ac", "2", "-movflags", "+faststart", "-sn", "-dn", "-map_chapters", "-1", out]
    gpu = common_in + ["-vaapi_device", VAAPI or "", "-i", src,
                       "-vf", "scale=-2:480,format=nv12,hwupload", "-c:v", "h264_vaapi", "-qp", "26"] + common_out
    cpu = common_in + ["-i", src, "-vf", "scale=-2:480", "-c:v", "libx264", "-preset", "ultrafast",
                       "-crf", "26", "-pix_fmt", "yuv420p"] + common_out
    for cmd in ((gpu, cpu) if VAAPI else (cpu,)):
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode == 0 and os.path.getsize(out) > 0:
            return out
    raise RuntimeError(r.stderr.decode(errors="replace").strip()[-400:])


def _even(x):
    return max(2, int(round(x / 2)) * 2)


def crop_box(src_w, src_h, target, crop=None):
    """The part of the picture a crop to `target` keeps: (w, h, x, y) in source pixels. `crop`
    {"x", "y", "s"} moves and shrinks the window: its centre as a fraction of the picture's width
    and height, and its size as a fraction of the largest window that fits (0.25-1). None = centred
    and as large as fits."""
    cw, ch = (src_h * target, src_h) if target < src_w / src_h else (src_w, src_w / target)
    c = crop or {}
    s = min(1.0, max(MIN_CROP, float(c.get("s") or 1)))
    w, h = min(src_w, _even(cw * s)), min(src_h, _even(ch * s))
    x = min(src_w - w, max(0, round(float(c.get("x", 0.5)) * src_w - w / 2)))
    y = min(src_h - h, max(0, round(float(c.get("y", 0.5)) * src_h - h / 2)))
    return w, h, x, y


def moved_crop(crop):
    """True when `crop` is anything but the plain centred, full-size window."""
    return bool(crop) and (abs(float(crop.get("s") or 1) - 1) > 0.005 or abs(float(crop.get("x", 0.5)) - 0.5) > 0.005
                           or abs(float(crop.get("y", 0.5)) - 0.5) > 0.005)


def geometry(src_w, src_h, aspect, fit, short_side, upscale=False, crop=None):
    """Output size plus the filter chain that gets there. `short_side` 0 = keep the source's.
    `upscale` is for supercuts, where every segment must come out the same size. `crop`: where the
    window sits and how big it is (crop_box), when `fit` is crop."""
    sar_src = src_w / src_h
    target = ASPECTS.get(aspect, sar_src)
    short = short_side or min(src_w, src_h)
    if not upscale:
        short = min(short, min(src_w, src_h))   # never upscale past the source
    if target >= 1:
        w, h = _even(short * target), _even(short)
    else:
        w, h = _even(short), _even(short / target)
    moved = aspect != "original" and fit != "pad" and moved_crop(crop)
    if aspect == "original" or abs(target - sar_src) < 0.01 and not moved:
        return w, h, [f"scale={w}:{h}"]
    if fit == "pad":
        return w, h, [f"scale={w}:{h}:force_original_aspect_ratio=decrease",
                      f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"]
    if moved:                    # the window the owner placed, in source pixels
        cw, ch, cx, cy = crop_box(src_w, src_h, target, crop)
        return w, h, [f"crop={cw}:{ch}:{cx}:{cy}", f"scale={w}:{h}"]
    # centre crop to the target shape first, then scale
    if target < sar_src:
        crop = f"crop=ih*{target:.6f}:ih"
    else:
        crop = f"crop=iw:iw/{target:.6f}"
    return w, h, [crop, f"scale={w}:{h}"]


def write_srt(cues, in_ms, out_ms, path):
    """The matched track's cues inside the cut, re-timed so 0 = the in point."""
    def ts(ms):
        ms = max(0, ms)
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for c in cues:
            if c["end_ms"] <= in_ms or c["start_ms"] >= out_ms:
                continue
            n += 1
            f.write(f"{n}\n{ts(c['start_ms'] - in_ms)} --> {ts(min(c['end_ms'], out_ms) - in_ms)}\n{c['text']}\n\n")
    return n


# Word-synced looks. Chunk rules per look (max words, max chars, break on a pause longer than);
# the browser preview uses the same table.
CHUNK_RULES = {"tiktok": (3, 18, 350), "popin": (5, 28, 500), "bigword": (1, 999, 0)}
LOOKS = tuple(CHUNK_RULES) + ("poster", "kinetic", "intense")  # every look that needs word timing


def chunk_words(words, max_words=3, max_chars=18, gap_ms=350):
    """Short-form captions show a few words at a time. Break on a pause, after a sentence end, or
    before the chunk gets too wide."""
    chunks, cur = [], []
    for w in words:
        if cur:
            text = " ".join(x["w"] for x in cur)
            if (len(cur) >= max_words or len(text) + 1 + len(w["w"]) > max_chars
                    or w["s"] - cur[-1]["e"] > gap_ms or cur[-1]["w"][-1:] in ".?!,"):
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def _ass_time(ms):
    ms = max(0, int(ms))
    return f"{ms // 3600000}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000 // 10:02d}"


def _bgr(hex6):
    return hex6[4:6] + hex6[2:4] + hex6[0:2]


MIN_WORD_MS = 70        # every word gets at least ~2 video frames on screen
GAP = "\\h "             # word spacing: a normal space plus a hard space, so heavy outlines and the
                         # enlarged active word never crowd their neighbours


def _pop(scale, dur_ms=150):
    """Entrance: 75 % -> 106 % -> 100 % of `scale`. Normally 150 ms; squeezed into under half the
    word's screen time in fast speech, and dropped entirely below ~110 ms (a word that brief just
    cuts in, which reads better than a pop that never finishes)."""
    if dur_ms < 110:
        return f"\\fscx{scale}\\fscy{scale}"
    total = int(min(150, dur_ms * 0.45))
    mid = total * 55 // 100
    return (f"\\fscx{scale * 75 // 100}\\fscy{scale * 75 // 100}"
            f"\\t(0,{mid},\\fscx{scale * 106 // 100}\\fscy{scale * 106 // 100})\\t({mid},{total},\\fscx{scale}\\fscy{scale})")


def normalize_words(words, min_ms=MIN_WORD_MS):
    """Whisper sometimes returns zero-length or bunched-up words in fast speech ("I" 920-920).
    A run of words starting closer than `min_ms` apart is re-spaced evenly across the time the run
    was actually spoken (stretched only as much as `min_ms` per word needs), so every word gets
    frames on screen without the whole sentence drifting later than the voice."""
    ws = [dict(x) for x in sorted(words, key=lambda x: x["s"])]
    i, n = 0, len(ws)
    while i < n:
        j = i
        while j + 1 < n and ws[j + 1]["s"] - ws[j]["s"] < min_ms:
            j += 1
        if j > i:
            a = ws[i]["s"]
            b = max(ws[j]["e"], a + (j - i + 1) * min_ms)
            if j + 1 < n:
                b = min(b, max(ws[j + 1]["s"], a + (j - i + 1) * min_ms))
            step = (b - a) / (j - i + 1)
            for k in range(i, j + 1):
                ws[k]["s"] = round(a + (k - i) * step)
                ws[k]["e"] = max(ws[k]["e"], round(a + (k - i + 1) * step))
        i = j + 1
    for k, x in enumerate(ws):
        x["e"] = max(x["e"], x["s"] + min_ms)
        if k > 0 and x["s"] < ws[k - 1]["s"] + min_ms:       # the stretch nudged into the next word
            x["s"] = ws[k - 1]["s"] + min_ms
            x["e"] = max(x["e"], x["s"] + min_ms)
    return ws


def word_ass(cues, in_ms, out_ms, w, h, st, path):
    """Word-synced captions burned with libass. Looks:
      tiktok  - 1-3 words; the spoken word in the highlight colour at 112 %; chunk pops in
      popin   - up to 5 words; each word pops in as it is said and stays until the line ends
                (words not yet said are laid out but invisible, so the line never shifts)
      bigword - one large word at a time, each popping in; words ending in ! use the highlight
    Heavy font, thick outline. Times are relative to the cut. Returns the event count."""
    look = st.get("style") if st.get("style") in LOOKS else "tiktok"
    pop = (lambda scale, dur=0: _pop(scale, 0)) if st.get("still") else _pop    # a still shows the settled word
    words = caption_words(cues, in_ms, out_ms)
    if st.get("caps"):
        words = [dict(x, w=x["w"].upper()) for x in words]
    font = st.get("font") if st.get("font") in FONTS else "Inter Black"
    fs = round(min(w, h) * TT_SIZES.get(st.get("size"), 0.095) * (1.7 if look == "bigword" else 1))
    outline, shadow = max(2, round(fs * 0.09)), max(1, round(fs * 0.05))
    default_pos = "middle" if look == "bigword" else "bottom"
    align, margin_v = {"top": (8, round(h * EDGE)), "upper": (8, round(h * (THIRD - 0.06))), "middle": (5, 0),
                       "lower": (2, round(h * (THIRD - 0.06))), "bottom": (2, round(h * (EDGE + 0.01)))
                       }.get(st.get("position") or default_pos, (2, round(h * (EDGE + 0.01))))
    hl = _bgr(HIGHLIGHTS.get(st.get("hl"), "FFE14D"))
    base = _bgr(COLOURS.get(st.get("colour"), "FFFFFF") if st.get("colour") != "black" else "FFFFFF")
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 0",
             "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
             "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
             "MarginL, MarginR, MarginV, Encoding",
             f"Style: TT,{font},{ass_size(font, fs)},&H00{base},&H00{base},&H00000000,&H78000000,{0 if 'Black' in font else -1},0,0,0,100,100,0,0,1,"
             f"{outline},{shadow},{align},{round(w * 0.06)},{round(w * 0.06)},{margin_v},1",
             "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    events = []
    chunks = chunk_words(words, *CHUNK_RULES[look])      # big word: strictly one word per chunk
    hold = 200 if look == "bigword" else 150
    for ci, ch in enumerate(chunks):
        c_start, c_end = ch[0]["s"], ch[-1]["e"] + hold
        if ci + 1 < len(chunks):
            c_end = min(c_end, chunks[ci + 1][0]["s"])
        for k, wk in enumerate(ch):
            ev_a = c_start if k == 0 else wk["s"]
            ev_b = ch[k + 1]["s"] if k + 1 < len(ch) else c_end
            dur = ev_b - ev_a
            parts = []
            if look == "bigword":
                if k > 0:
                    break                                         # a big-word chunk is one event
                text = ch[0]["w"]
                # shrink a long word so it fits ~88 % of the width (~0.62 em per character)
                fit = min(fs, int(w * 0.88 / max(1, 0.62 * len(text))))
                col = hl if text[-1:] == "!" else base
                events.append((c_start, c_end, "{" + f"\\c&H{col}&\\fs{ass_size(font, fit)}" + pop(100, c_end - c_start) + "}" + text))
                continue
            for j, wj in enumerate(ch):
                if look == "tiktok":
                    scale = 112 if j == k else 100
                    tag = f"\\c&H{hl if j == k else base}&" + (pop(scale, dur) if k == 0 else f"\\fscx{scale}\\fscy{scale}")
                else:                                                         # popin
                    if j > k:
                        tag = f"\\alpha&HFF&\\fscx100\\fscy100"          # not said yet: reserve its space
                    elif j == k:
                        tag = f"\\alpha&H00&\\c&H{hl}&" + pop(100, dur)
                    else:
                        tag = f"\\alpha&H00&\\c&H{base}&\\fscx100\\fscy100"
                parts.append("{" + tag + "}" + wj["w"])
            events.append((ev_a, ev_b, GAP.join(parts)))
    n = 0
    for ev_a, ev_b, text in events:
        a, b = max(0, ev_a - in_ms), min(out_ms, ev_b) - in_ms
        if b > a:
            lines.append(f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},TT,,0,0,0,,{text}")
            n += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return n


def output_size(src, opts):
    info = probe(src)
    w, h, _ = geometry(info["width"], info["height"], opts.get("aspect", "original"),
                       opts.get("fit", "crop"), int(opts.get("res") or 0))
    return w, h


def caption_words(cues, in_ms, out_ms):
    words = sorted((x for c in cues for x in (c.get("words") or [])), key=lambda x: x["s"])
    words = [dict(x, w=x["w"].replace("{", "(").replace("}", ")").replace("\\", "/")) for x in words
             if x["e"] > in_ms and x["s"] < out_ms and x["w"].strip()]
    return normalize_words(words)


def poster_ass(posters, in_ms, out_ms, w, h, st, path):
    """ASS for poster.layout(): each line is its own event, centred at its computed top, with
    per-word font/size/colour so libass sets mixed sizes on one baseline. Words appear as they are
    spoken (unsaid words are laid out but invisible) and pop in. Tier 4 uses the highlight colour;
    tier 1 is a lighter face in soft grey for contrast."""
    hl = _bgr(HIGHLIGHTS.get(st.get("hl"), "FFE14D"))
    base = _bgr(COLOURS.get(st.get("colour"), "FFFFFF") if st.get("colour") != "black" else "FFFFFF")
    soft = _bgr("D9D9D9") if base == "FFFFFF" else base
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 2",
             "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
             "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
             "MarginL, MarginR, MarginV, Encoding",
             "Style: P,Inter,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,0,0,0,0,100,100,0,0,1,3,2,8,0,0,0,1",
             "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    n = 0
    for p in posters:
        order = [(li, wi) for li, ln in enumerate(p["lines"]) for wi in range(len(ln["words"]))]
        flat = [p["lines"][li]["words"][wi] for li, wi in order]
        for k, wk in enumerate(flat):
            ev_a = p["start_ms"] if k == 0 else wk["s"]
            ev_b = flat[k + 1]["s"] if k + 1 < len(flat) else p["end_ms"]
            a, b = max(0, ev_a - in_ms), min(out_ms, ev_b) - in_ms
            if b <= a:
                continue
            seen = 0
            for ln in p["lines"]:
                parts = []
                for x in ln["words"]:
                    j = seen
                    seen += 1
                    col = hl if x["tier"] == 4 else soft if x["tier"] <= 1 else base
                    bold = 0 if "Black" in x["font"] else 1
                    tag = (f"\\fn{x['font']}\\fs{ass_size(x['font'], x['size'])}\\b{bold}\\c&H{col}&"
                           f"\\bord{max(2, round(x['size'] * 0.075))}\\shad{max(1, round(x['size'] * 0.04))}")
                    if j > k:
                        tag += "\\alpha&HFF&\\fscx100\\fscy100"
                    elif j == k:
                        tag += "\\alpha&H00&" + _pop(100, 0 if st.get("still") else ev_b - ev_a)
                    else:
                        tag += "\\alpha&H00&\\fscx100\\fscy100"
                    if x.get("whisper") and j <= k:       # a whisper: outline only (= drawPoster)
                        tag += f"\\1a&HFF&\\3c&H{base}&\\bord{max(1, round(x['size'] * 0.045))}\\shad0"
                    parts.append("{" + tag + "}" + x["w"])
                lines.append(f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},P,,0,0,0,,"
                             f"{{\\an8\\pos({w // 2},{ln['y']})}}" + " ".join(parts))
                n += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return n


def force_style(st):
    inset, third = round(288 * EDGE), round(288 * (THIRD - 0.05))
    align, margin = {"top": (6, inset), "upper": (6, third), "middle": (10, 0), "lower": (2, third)
                     }.get(st.get("position"), (2, inset))
    colour = COLOURS.get(st.get("colour"), "FFFFFF")
    bgr = colour[4:6] + colour[2:4] + colour[0:2]
    font = st.get("font") if st.get("font") in FONTS else "Inter"
    parts = [f"FontName={font}", f"FontSize={ass_size(font, SIZES.get(st.get('size'), 20))}",
             f"PrimaryColour=&H00{bgr}", "Bold=1",
             # ffmpeg reads force_style Alignment in legacy SSA numbering (2 bottom, 6 top, 10 middle;
             # all centred), not the ASS numpad layout: 5 would be top-left, 8 middle-left.
             # MarginV is in the 288-line script's units.
             f"Alignment={align}", f"MarginV={margin}"]
    dark = st.get("colour") == "black"          # black text gets a light box / white outline
    if st.get("box"):
        back = "&H40FFFFFF" if dark else "&H80000000"
        parts += ["BorderStyle=3", "Outline=2", "Shadow=0", f"BackColour={back}", f"OutlineColour={back}"]
    else:
        parts += ["BorderStyle=1", "Outline=1.6", "Shadow=0.6", "OutlineColour=" + ("&H00FFFFFF" if dark else "&H00000000"),
                  "BackColour=&H80000000"]
    return ",".join(parts)


def render(src, in_ms, out_ms, opts, cues, out, label=None, uniform=False, slow=None, progress=None, smooth=0, ramp=None):
    """Final clip. x264/AAC MP4 by default; WebM (VP9/Opus) or GIF (palette, no audio).
    Subtitles are burned after scaling, so the style is relative to the output size.
    `uniform` (supercut segments): fixed size, 30 fps, square pixels, 48 kHz stereo always present,
    so the segments concatenate without re-encoding. `label`: small source caption, top-left.
    `slow` (a, b, factor, sound) from slowmo.part_of: that part plays slower (see slowmo.py);
    `smooth` (60, from slowmo.smooth_of): that frame rate, the frames in between made up;
    `ramp` {"beats", "length", "opts", "keep"?} (ramp.py): speed easing in and out on the song's beats,
    silent but for the lines it keeps at normal speed;
    progress(done, total) while frames are made."""
    with tempfile.TemporaryDirectory(prefix="saytheline-") as tmp:
        return _render(src, in_ms, out_ms, opts, cues, out, label, uniform, slow, progress, tmp, smooth, ramp)


def _render(src, in_ms, out_ms, opts, cues, out, label, uniform, slow, progress, tmp, smooth=0, ramp=None):
    info = probe(src)
    if not info["width"]:
        raise RuntimeError("no video stream")
    aspect = opts.get("aspect", "original")
    if uniform and aspect == "original":
        aspect = "16:9"          # sources differ, so a supercut needs one fixed shape
    # (a supercut at 16:9 shows each whole picture, unless that clip's crop window was placed)
    w, h, chain = geometry(info["width"], info["height"], aspect,
                           "pad" if uniform and aspect == "16:9" and not moved_crop(opts.get("crop")) else opts.get("fit", "crop"),
                           int(opts.get("res") or (720 if uniform else 0)), upscale=uniform, crop=opts.get("crop"))
    fmt = "mp4" if uniform else opts.get("format", "mp4")
    if ramp:
        # the speed-ramped segment becomes the source: silent (the song carries it), or with the
        # sound of the lines it kept at normal speed
        from . import ramp as rampmod
        keep = ramp.get("keep") or ()
        src, times = rampmod.master(src, in_ms, out_ms, ramp["length"], ramp["beats"], ramp["opts"], chain, (w, h), tmp, progress,
                                    keep=keep, track=audio_track(src) if keep and info["has_audio"] else None)
        cues = rampmod.remap_cues(cues, in_ms, times)
        in_ms, out_ms = 0, round(len(times) * 1000 / rampmod.OUT_FPS)
        chain, info = [], probe(src)
    elif slow or (smooth and fmt != "gif"):
        # the slowed / smoothed cut, already at the output size, becomes the source: nothing else changes
        from . import slowmo
        track = audio_track(src) if info["has_audio"] else None
        src, _ = slowmo.master(src, in_ms, out_ms, slow, chain, (w, h), track, tmp, progress,
                               smooth=smooth if fmt != "gif" else 0)
        part = slow[:3] if slow else (0, 0, 1)
        cues = slowmo.remap_cues(cues, in_ms, *part)
        in_ms, out_ms = 0, round(slowmo.timemap(out_ms - in_ms, *part))
        chain, info = [], probe(src)
    dur = (out_ms - in_ms) / 1000
    # The input -t below is not exact: depending on where the start falls between frames it lets one
    # frame past the out point through, and with a cut there that frame is the next shot. The trim
    # keeps exactly the frames before the out point (times here are relative to the -ss point).
    chain.insert(0, f"trim=end={dur:.3f}")
    sub = caption_filter(opts.get("subs") or {}, cues, in_ms, out_ms, w, h, tmp)
    if sub:
        chain.append(sub)
    if label:
        tf = os.path.join(tmp, "label.txt")
        with open(tf, "w", encoding="utf-8") as f:
            f.write(label)
        chain.append(f"drawtext=fontfile={_fpath(font_path())}:textfile={_fpath(tf)}:x=h*0.03:y=h*0.03:fontsize=h*0.032"
                     f":fontcolor=white:box=1:boxcolor=black@0.5:boxborderw={max(4, round(h * 0.012))}")
    if uniform:
        # exactly round(dur x 30) frames: the fps conversion can round up one, which adds up over a cut
        chain += ["fps=30", f"trim=end_frame={max(1, round(dur * 30))}", "setsar=1", "format=yuv420p"]
        if opts.get("transition") == "fade" and dur > 0.6:     # quick dip at each join
            chain += [f"fade=t=in:st=0:d=0.12", f"fade=t=out:st={dur - 0.12:.3f}:d=0.12"]
    cmd = [*config.nice(), "ffmpeg", "-v", "error", "-nostdin", "-y",
           "-ss", f"{in_ms / 1000:.3f}", "-t", f"{dur:.3f}", "-i", src]   # input-side -t: the
    # GIF palette pass waits for end of input, so the input itself must stop at the out point
    silent = uniform and not info["has_audio"]
    if silent:   # every segment needs an audio track or the concat drops audio
        cmd += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    if fmt == "gif":
        # two-pass palette in one graph; GIFs stay small: 12 fps, at most 480 px wide
        gw = min(w, 480)
        graph = ("[0:v:0]" + ",".join(chain) + f",fps=12,scale={gw}:-2:flags=lanczos,split[a][b];"
                 "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4[g]")
        cmd += ["-filter_complex", graph, "-map", "[g]", out]
    else:
        cmd += ["-map", "0:v:0", "-map", "1:a:0" if silent else f"0:a:{audio_track(src)}?", "-vf", ",".join(chain),
                "-sn", "-dn", "-map_chapters", "-1"]
        if uniform:
            # Sources differ wildly in level (a quiet drama next to a loud sitcom) and every hard
            # cut clicks. Even out loudness per segment and fade the audio a few ms at each end.
            fa = 0.12 if opts.get("transition") == "fade" else 0.015
            # the audio exactly as long as the video's frames, so joined segments don't drift apart
            vdur = max(1, round(dur * 30)) / 30
            af = [f"atrim=end={vdur:.4f}"] + (["loudnorm=I=-16:TP=-1.5:LRA=11"] if opts.get("loudnorm", True) else []) + \
                 ["aresample=48000", f"apad=whole_dur={vdur:.4f}", f"afade=t=in:st=0:d={fa}", f"afade=t=out:st={max(0, vdur - fa):.3f}:d={fa}"]
            cmd += ["-af", ",".join(af), "-ar", "48000"]
        if fmt == "webm":
            cmd += ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-row-mt", "1",
                    "-b:v", "0", "-crf", "33", "-c:a", "libopus", "-b:a", "128k", out]
        else:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-movflags", "+faststart", out]
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()[-500:])
    mode = opts.get("audio", "original")
    if mode in ("dialogue", "nodialogue") and fmt != "gif" and info["has_audio"]:
        separate(out, mode, fmt)
    if fmt == "gif" and w > 480:                 # GIFs are scaled down to 480 wide after the chain
        w, h = 480, round(h * 480 / w / 2) * 2
    return dict(width=w, height=h, seconds=round(dur, 2), bytes=os.path.getsize(out))


def caption_filter(st, cues, in_ms, out_ms, w, h, tmp):
    """The libass filter that burns the chosen caption look (times relative to in_ms), or None."""
    if not st.get("on"):
        return None
    if st.get("style") in ("kinetic", "intense"):
        from . import intense, kinetic
        mod = intense if st.get("style") == "intense" else kinetic
        ass = os.path.join(tmp, "cut.ass")
        if mod.ass(mod.layout(caption_words(cues, in_ms, out_ms), w, h, st), in_ms, out_ms, w, h, st, ass):
            return f"subtitles={_fpath(ass)}:fontsdir={_fpath(FONT_DIR)}"
    elif st.get("style") == "poster":
        from . import poster
        ass = os.path.join(tmp, "cut.ass")
        if poster_ass(poster.layout(caption_words(cues, in_ms, out_ms), w, h, st), in_ms, out_ms, w, h, st, ass):
            return f"subtitles={_fpath(ass)}:fontsdir={_fpath(FONT_DIR)}"
    elif st.get("style") in LOOKS:
        ass = os.path.join(tmp, "cut.ass")
        if word_ass(cues, in_ms, out_ms, w, h, st, ass):
            return f"subtitles={_fpath(ass)}:fontsdir={_fpath(FONT_DIR)}"
    else:
        srt = os.path.join(tmp, "cut.srt")
        if write_srt(cues, in_ms, out_ms, srt):
            return f"subtitles={_fpath(srt)}:fontsdir={_fpath(FONT_DIR)}:force_style='{force_style(st)}'"
    return None


def still(src, at_ms, in_ms, out_ms, opts, cues, out):
    """One JPEG frame at at_ms, captioned exactly as the cut [in_ms, out_ms] shows it at that moment
    (word looks without their entrance pop, which a single frame could catch mid-grow). Seeks
    straight to the frame and shifts its timestamp so the caption track lines up: well under a second."""
    info = probe(src)
    if not info["width"]:
        raise RuntimeError("no video stream")
    w, h, chain = geometry(info["width"], info["height"], opts.get("aspect", "original"), opts.get("fit", "crop"),
                           int(opts.get("res") or 0), crop=opts.get("crop"))
    with tempfile.TemporaryDirectory(prefix="saytheline-still-") as tmp:
        st = dict(opts.get("subs") or {}, still=True)
        sub = caption_filter(st, cues, in_ms, out_ms, w, h, tmp) if in_ms <= at_ms < out_ms else None
        vf = [f"setpts=PTS-STARTPTS+{(at_ms - in_ms) / 1000:.3f}/TB"] + chain + ([sub] if sub else [])
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{at_ms / 1000:.3f}", "-i", src,
                            "-map", "0:v:0", "-vf", ",".join(vf), "-frames:v", "1", "-q:v", "2", out],
                           capture_output=True, timeout=60)
        if r.returncode != 0 or not os.path.exists(out):
            raise RuntimeError(r.stderr.decode(errors="replace").strip()[-500:] or "no frame")
    return dict(width=w, height=h, bytes=os.path.getsize(out))


def separate(path, mode, fmt):
    """Replace a rendered clip's audio with one demucs stem, in place. `--two-stems=vocals` splits
    into vocals (on film/TV audio that is the dialogue) and everything else (music + effects).
    CPU only: about 2x the clip length (8 s took 14 s), so clips only, never whole episodes."""
    if not DEMUCS:
        raise RuntimeError("Dialogue only / No dialogue need demucs (pip install demucs, or set tools.demucs)")
    with tempfile.TemporaryDirectory(prefix="saytheline-stem-") as tmp:
        wav = os.path.join(tmp, "a.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", path, "-vn", "-ac", "2",
                        "-ar", "44100", wav], check=True, capture_output=True, timeout=120)
        r = subprocess.run([*config.nice(), DEMUCS, "-n", "htdemucs", "--two-stems=vocals",
                            "-d", "cpu", "-j", "1", "-o", tmp, wav],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
                           env={**os.environ, "OMP_NUM_THREADS": "4", "MKL_NUM_THREADS": "4"})
        if r.returncode != 0:
            raise RuntimeError("stem separation failed: " + r.stderr.strip()[-300:])
        stem = os.path.join(tmp, "htdemucs", "a", "vocals.wav" if mode == "dialogue" else "no_vocals.wav")
        mixed = os.path.join(tmp, "out." + fmt)
        acodec = ["-c:a", "libopus", "-b:a", "128k"] if fmt == "webm" else ["-c:a", "aac", "-b:a", "160k"]
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", path, "-i", stem, "-map", "0:v:0",
                        "-map", "1:a:0", "-c:v", "copy", *acodec, "-ar", "48000", "-shortest",
                        "-movflags", "+faststart", mixed], check=True, capture_output=True, timeout=120)
        shutil.move(mixed, path)       # tmp is tmpfs: a rename would cross devices


def supercut(segments, opts, out, progress=None):
    """Concatenate many hits into one MP4. Each segment is rendered `uniform` (same size, fps,
    audio layout) into a temp dir, then joined with the concat demuxer without re-encoding.
    segments: [{"src", "in_ms", "out_ms", "cues", "label", "slow"?, "crop"?}] (slow: slowmo.part_of;
    crop: that clip's own crop window, geometry)."""
    with tempfile.TemporaryDirectory(prefix="saytheline-sc-") as tmp:
        parts = []
        for i, sg in enumerate(segments):
            if progress:
                progress(i, len(segments))
            p = os.path.join(tmp, f"{i:04d}.mp4")
            try:
                render(sg["src"], sg["in_ms"], sg["out_ms"], {**opts, "format": "mp4", "audio": "original", "crop": sg.get("crop")},
                       sg["cues"], p, label=sg.get("label"), uniform=True, slow=sg.get("slow"), ramp=sg.get("ramp"),
                       progress=(lambda d, t, i=i: progress(i, len(segments), d, t)) if progress and sg.get("ramp") else None)
                parts.append(p)
            except Exception:
                continue          # one unreadable file must not sink the whole supercut
        if not parts:
            raise RuntimeError("no segment rendered")
        lst = os.path.join(tmp, "list.txt")
        with open(lst, "w", encoding="utf-8") as f:
            for p in parts:              # each part lasts exactly its video (AAC pads its last frame)
                r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration",
                                    "-of", "csv=p=0", p], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
                f.write(f"file '{p}'\n" + (f"duration {float(r.stdout.strip()):.4f}\n" if r.stdout.strip() else ""))
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0",
                            "-i", lst, "-c", "copy", "-movflags", "+faststart", out],
                           capture_output=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode(errors="replace").strip()[-400:])
        if opts.get("audio") in ("dialogue", "nodialogue"):
            separate(out, opts["audio"], "mp4")
        info = probe(out)
    return dict(segments=len(parts), skipped=len(segments) - len(parts),
                seconds=round(info["duration"], 2), bytes=os.path.getsize(out))


def safe_name(s, limit=60):
    s = re.sub(r"[^\w\s\-'.,!&()]", "", s or "").strip()
    return re.sub(r"\s+", " ", s)[:limit].rstrip(" .")
