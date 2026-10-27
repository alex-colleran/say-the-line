"""The "wasted" ending: the meme's game-over screen, in a look of its own. From a moment near the
end, the picture goes into slow motion, drains to grey and darkens while the camera pushes slowly
in, and the "fall" sound starts with it; then a dark band and a big red word slam in over it, and
the sound plays out over the rest.

The look follows the game's "DeathFailOut" effect, from frames of it: a flash as it hits, then a
glowing yellow-green monochrome, easing through a muted olive into a cool, slightly teal black and
white, with the middle dodged bright and the edges burned dark. The word lands with the sound's
second hit (its loudest peak after the first moments; 1.5 s, the game's own, without one), on a
translucent grey band whose top and bottom edges are slightly angled and which fades toward the
sides. The band and the dodge/burn are drawn once per output size (data/cache/wasted).

The timing is GTA V's own, from Rockstar's scripts: time runs at 0.2 (SET_TIME_SCALE(0.2f)), so the
ending is 5x slow motion, made by RIFE like any slow part (slowmo.py), still at the source's frame
rate; the word lands 1.5 s after the start ("TextHit" 1500 ms after the "ScreenFlash").

A request's `wasted` is {"text": the word (default "wasted"), "sub": a line under it in the band
(like the game's "The elk heard Trevor."; none when blank), "at_ms": when it starts (media
timeline; default: as late as lets the slow part reach the out point), "length_ms": how long the
ending lasts on screen (default and most: the fall sound's length), "sound": true}. The ending is
the end of the clip: its slow part takes length / 5 of the source from the start, and what the cut
has after that is left out; when the out point comes first, the last frame holds for the rest.
Without RIFE it plays at normal speed. The banner is drawn over the captions. The font is the
bundled League Spartan Black, or any font file set as [tools] wasted_font."""
import functools
import os
import re
import subprocess

from . import config, media

CACHE = os.path.join(config.data_dir(), "cache", "wasted")
VERSION = 9                    # bump when the drawing changes, so cached images are redrawn

FACTOR = 5                     # GTA V: SET_TIME_SCALE(0.2f)
SAT_S = 0.15                   # the colour is gone this long after it starts (it hits monochrome)
DRAIN_S = 0.3                  # the dodge/burn and the harder contrast are in by this long after
FLASH_S = 0.1                  # the flash at the start fades with this time constant
# the tints in turn, as gammas (r, g, b): seconds after the start each is fully in
TINTS = ((0.0, (1.15, 1.5, 0.7)),          # glowing yellow-green
         (0.9, (1.05, 1.18, 0.9)),         # muted olive
         (1.6, (0.95, 1.0, 1.05)))         # cool grey
GLOW_S = (0.5, 1.1)            # the highlights' glow: full until the first, gone by the second
LAND_S = 0.12                  # the word's slam is this long before it lands
BANNER_S = 1.5                 # the word lands this long after it starts, without a hit in the sound
PUSH = 0.07                    # the push-in: 7% closer by the end
MIN_LENGTH_MS = 1000
DEFAULT_LENGTH_MS = 9400       # without the fall sound to measure
MIN_PART_MS = 200
RED = "0xC62539"               # the word's crimson, and how much of it covers the picture: worked back
WORD_ALPHA = 0.68              # from game frames (the picture's texture shows through the letters)
# the dodge/burn grey in the middle and at the far corners: under 128 the picture is multiplied by
# it (x0.5 at 64), over 128 lifted toward white (a sixth of the way at 148); 128 leaves it alone
DODGE, BURN = 148, 18
BAND_ALPHA = 0.51              # the band is black at this (inside = 0.46 x outside + 6 in the game)
# where things sit, as shares of the height, measured off game frames: the band's middle and its
# height there, and how far each edge slopes across the width (wider on the right); the word's size
# (its letters come out about 0.08 tall with a line, 0.1 without) and the line's; how far above the
# band's middle the word is set, alone and with a line, and the line below (so the letters land at
# 0.476 and the line at 0.545, as in the game)
BAND_Y, BAND_H, BAND_TILT = 0.485, 0.205, 0.038
WORD_SIZE, WORD_SIZE_SUB = 0.125, 0.083
WORD_STRETCH = 1.0             # how much taller than the font the word is set (the game sets it as is)
SUB_SIZE = 0.03
SUB_FONT = os.path.join(media.FONT_DIR, "Inter-Medium.otf")     # the line's lighter face (bundled, OFL)
WORD_LIFT, SUB_LIFT, SUB_DROP = -0.002, 0.002, 0.061
GLYPH_DROP = 0.069             # Pricedown's letters sit this much of the size below the middle of its text box


@functools.lru_cache(maxsize=4)
def _sound_ms(path, mtime):
    from . import sfx
    return round(sfx._duration(path) * 1000) or DEFAULT_LENGTH_MS


def max_length_ms():
    """The ending's longest (and default) length: the fall sound's."""
    from . import sfx
    try:
        p = sfx.path("fall")
        return _sound_ms(p, os.path.getmtime(p)) if p else DEFAULT_LENGTH_MS
    except (OSError, RuntimeError):
        return DEFAULT_LENGTH_MS


def sub_font():
    return SUB_FONT if os.path.isfile(SUB_FONT) else media.font_path("Inter")


def font():
    p = os.environ.get("SAYTHELINE_WASTED_FONT")
    return p if p and os.path.isfile(p) else media.font_path("League Spartan Black")


def spec_of(req, in_ms, out_ms, factor=FACTOR):
    """(start s from the cut's in point, length s on screen, text, sound, sub), or None (also when
    `at_ms` is outside the cut)."""
    s = req.get("wasted")
    if not isinstance(s, dict) or not s.get("on", True):
        return None
    text = " ".join(str(s.get("text") or "wasted").split())[:24] or "wasted"
    sub = " ".join(str(s.get("sub") or "").split())[:80]
    most = max_length_ms()
    length = max(MIN_LENGTH_MS, min(most, int(s.get("length_ms") or most)))
    if s.get("at_ms") is None:
        at = max(in_ms, min(out_ms - MIN_PART_MS, out_ms - round(length / factor)))
    else:
        at = int(s["at_ms"])
        if not in_ms <= at <= out_ms - MIN_PART_MS:      # a preview of another part of the cut: not in it
            return None
    return (at - in_ms) / 1000, length / 1000, text, s.get("sound", True) is not False, sub


def plan(spec, in_ms, out_ms, factor=FACTOR):
    """How the ending is made: (slow part (a, b, factor, "deep") in ms from the in point, or None
    at normal speed; the cut's new out point; the render's (start s, hold s, text, sound, sub)). The
    start is the same on the slowed timeline, since the slow part begins there."""
    start, length, text, sound, sub = spec
    a = round(start * 1000)
    b = min(out_ms - in_ms, a + round(length * 1000 / factor))
    hold = max(0.0, length - (b - a) * factor / 1000)
    part = (a, b, factor, "deep") if factor > 1 else None
    return part, in_ms + b, (start, round(hold, 3), text, sound, sub)


@functools.lru_cache(maxsize=4)
def _hit_s(path, mtime):
    """The sound's second hit: its loudest 10 ms peak from 0.6 s to 4 s in, or None."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-t", "4", "-af",
                        "aresample=48000,asetnsamples=n=480,astats=metadata=1:reset=1,"
                        "ametadata=mode=print:key=lavfi.astats.Overall.Peak_level:file=-", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=60)
    peaks = [float(x) for x in re.findall(r"Peak_level=(-?[\d.]+)", r.stdout)]
    best = max(range(60, len(peaks)), key=lambda i: peaks[i], default=None)
    return round(best * 0.01, 2) if best is not None else None


def banner_at():
    """Seconds from the start to when the word starts its slam, so it lands on the sound's hit."""
    from . import sfx
    try:
        p = sfx.path("fall")
        hit = _hit_s(p, os.path.getmtime(p)) if p else None
    except (OSError, RuntimeError):
        hit = None
    return max(0.3, hit - LAND_S) if hit else BANNER_S


def banner_s(spec):
    """When the word lands, from the cut's in point."""
    return spec[0] + banner_at()


def _draw(name, w, h, fmt, expr):
    """A cached image drawn by geq (once per output size)."""
    out = os.path.join(CACHE, f"v{VERSION}-{name}-{w}x{h}.png")
    if os.path.isfile(out):
        return out
    os.makedirs(CACHE, exist_ok=True)
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", f"color=c=black@0:s={w}x{h}:d=0.04,format={fmt}",
                        "-vf", f"geq={expr}", "-frames:v", "1", out + ".tmp.png"], capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("could not draw the Wasted ending: " + r.stderr.decode(errors="replace").strip()[-300:])
    os.replace(out + ".tmp.png", out)
    return out


def dodge_burn(w, h):
    """The dodge/burn grey (see DODGE): lighter than mid-grey in the middle, dark at the edges and
    darkest in the corners."""
    d = "hypot((X-W/2)/(W/2)\\,(Y-H/2)/(H/2))"
    return _draw("dodge", w, h, "gray", f"lum='{DODGE}-{DODGE - BURN}*min(1\\,max(0\\,({d}-0.2)/1.15))'")


def band(w, h):
    """The word's band: black at BAND_ALPHA, as in the game (it only darkens, adding no grey), faded
    out toward both ends, the top edge rising and the bottom edge falling a
    little to the right, its edges anti-aliased."""
    cy, bh, tilt = h * BAND_Y, h * BAND_H, h * BAND_TILT
    top = f"({cy - bh / 2:.2f}+{tilt:.2f}*(0.5-X/W))"
    bot = f"({cy + bh / 2:.2f}-{tilt:.2f}*(0.5-X/W))"
    inside = f"min(1\\,max(0\\,Y-{top}+0.5))*min(1\\,max(0\\,{bot}-Y+0.5))"
    ends = "min(1\\,max(0\\,(X/W-0.04)/0.16))*min(1\\,max(0\\,(0.985-X/W)/0.15))"     # faded at both ends
    alpha = f"255*{inside}*{ends}*{BAND_ALPHA}"
    return _draw("band", w, h, "rgba", f"r=8:g=8:b=8:a='{alpha}'")


def filters(spec, w, h, tmp, dur_s):
    """The picture filters, after the captions. `dur_s`: the cut's length before the hold."""
    a, hold, text, _, sub = (*spec, "")[:5]
    t0 = a + banner_at()
    on = f"gte(t\\,{a:.3f})"
    since = f"max(0\\,t-{a:.3f})"
    k = f"min(1\\,{since}/{DRAIN_S})"                                                   # 0 -> 1
    flash = f"{on}*exp(-{since}/{FLASH_S})"

    def ramp(t, x, y):          # 0 before x, 1 after y
        return f"min(1\\,max(0\\,({t}-{x:.3f})/{y - x:.3f}))"
    # each tint's weight: the first until the second comes in, and so on
    (_, g1), (s2, g2), (s3, g3) = TINTS
    w2, w3 = ramp(since, s2 * 0.55, s2), ramp(since, s3 * 0.75, s3)
    gam = lambda i: f"if({on}\\,{g1[i]}+({g2[i] - g1[i]:.3f})*{w2}+({g3[i] - g2[i]:.3f})*{w3}\\,1)"
    glow = f"gte(T\\,{a:.3f})*(1-{ramp(f'(T-{a:.3f})', *GLOW_S)})"
    ease = f"(1-cos(PI*min(1,max(0,(t-{a:.3f})/{max(0.5, dur_s + hold - a):.3f}))))/2"   # 0 -> 1 to the end
    tf = os.path.join(tmp, "wasted.txt")
    with open(tf, "w", encoding="utf-8") as f:
        f.write(text)
    sf = os.path.join(tmp, "wasted-sub.txt")
    if sub:
        with open(sf, "w", encoding="utf-8") as f:
            f.write(sub)
    # long words still fit across; with a line under it the word is a little smaller, to leave room
    fs = round(min(h * (WORD_SIZE if not sub else WORD_SIZE_SUB), w * 1.5 / max(4, len(text))))
    ch = round(fs * 1.6)
    word_png = os.path.join(tmp, "wasted-word.png")
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", f"color=c=black@0:s={w}x{ch}:d=0.04,format=rgba",
                        "-vf", f"drawtext=fontfile={media._fpath(font())}:textfile={media._fpath(tf)}:fontcolor={RED}:fontsize={fs}"
                        f":x=(w-text_w)/2:y=(h-text_h)/2,scale=iw:{round(ch * WORD_STRETCH)}",
                        "-frames:v", "1", word_png], capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("could not set the Wasted word: " + r.stderr.decode(errors="replace").strip()[-300:])
    slam = f"(1+0.33*max(0\\,1-(t-{t0:.3f})/0.16))"
    mid = BAND_Y - (WORD_LIFT if not sub else SUB_LIFT)        # where the word's letters sit
    # the dodge/burn blends in with the drain: a multiply where it burns, a lift toward white where it
    # dodges, so even bright edges go dark, as in the game (grey's chroma stays as it is)
    ov = "if(lt(B\\,128)\\,A*B/128\\,A+(255-A)*(B-128)/127)"
    kb = f"min(1\\,max(0\\,(T-{a:.3f})/{DRAIN_S}))"
    return [
        # the flash, and the colour gone at once
        f"hue=s='min(1\\,max(0\\,1-(t-{a:.3f})/{SAT_S}))':b='3*{flash}'",
        # the tints in turn, over a brighter, harder monochrome
        f"eq=eval=frame:gamma_r='{gam(0)}':gamma_g='{gam(1)}':gamma_b='{gam(2)}'"
        f":contrast='1+0.1*{k}':gamma='1+0.3*{k}'",
        # the glow on the highlights as it hits (screened in, fading out)
        f"format=gbrp,split[wzs][wzt];[wzt]gblur=sigma={max(2, min(w, h) / 45):.1f}[wzu];"
        f"[wzs][wzu]blend=all_expr='A+(255-(255-A)*(255-B)/255-A)*0.6*{glow}':enable='between(t\\,{a:.3f}\\,{a + GLOW_S[1]:.3f})'",
        # the push-in (scaled up, cropped back to size)
        f"scale=w='2*trunc(iw*(1+{PUSH}*{ease})/2)':h=-2:eval=frame", f"crop={w}:{h}",
        f"format=yuv420p,null[wz0];movie={media._fpath(dodge_burn(w, h))},format=yuv420p[wzg];"
        f"[wz0][wzg]blend=all_expr='A+({ov}-A)*{kb}':enable='gte(t\\,{a:.3f})'",
        # the band, fading in just before the word lands
        f"null[wz1];movie={media._fpath(band(w, h))}:loop=0,setpts=N/30/TB,format=rgba,"
        f"fade=t=in:st={max(0, t0 - 0.06):.3f}:d=0.1:alpha=1[wzb];[wz1][wzb]overlay=0:0:format=auto:shortest=1",
        # the word slams in: from a third bigger, landing in 0.16 s, flat red, a little see-through
        f"null[wz2];movie={media._fpath(word_png)}:loop=0,setpts=N/30/TB,format=rgba,"
        f"scale=w='iw*{slam}':h='ih*{slam}':eval=frame,fade=t=in:st={t0:.3f}:d=0.08:alpha=1,"
        f"colorchannelmixer=aa={WORD_ALPHA}[wzw];[wz2][wzw]overlay=x=(W-w)/2"
        f":y='H*{mid:.4f}-h*{0.5 + GLYPH_DROP * fs / ch:.4f}':eval=frame:format=auto:shortest=1",
    ] + ([
        # the line under it: small white type, in with the word
        f"drawtext=fontfile={media._fpath(sub_font())}:textfile={media._fpath(sf)}:fontcolor=white"
        f":fontsize=h*{SUB_SIZE}:alpha='min(1\\,max(0\\,(t-{t0 + 0.1:.3f})/0.15))'"
        f":shadowx=0:shadowy={max(1, round(h * 0.003))}:shadowcolor=black@0.6:x=(w-text_w)/2:y=h*{BAND_Y + SUB_DROP:.4f}-text_h/2",
    ] if sub else []) + ([f"tpad=stop_mode=clone:stop_duration={hold:.3f}"] if hold > 0 else [])


def audio_filters(spec, dur_s):
    """The clip's own sound (slowed with the picture): fades out under the fall sound by just after
    the word lands, padded through the hold."""
    a, hold = spec[:2]
    t0 = a + banner_at()
    end = dur_s + hold
    return [f"apad=whole_dur={end:.3f}", f"afade=t=out:st={a:.3f}:d={max(0.3, t0 + 0.5 - a):.3f}:curve=exp"]
