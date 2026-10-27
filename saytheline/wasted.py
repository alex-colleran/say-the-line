"""The "wasted" ending: the meme's game-over screen, in a look of its own. From a moment near the
end, the picture goes into slow motion, drains to grey and darkens while the camera pushes slowly
in, and the "fall" sound starts with it; then a dark band and a big red word slam in over it, and
the sound plays out over the rest.

The timing is GTA V's own, from Rockstar's scripts: time runs at 0.2 (SET_TIME_SCALE(0.2f)), so the
ending is 5x slow motion, made by RIFE like any slow part (slowmo.py), still at the source's frame
rate; the word lands 1.5 s after the start ("TextHit" 1500 ms after the "ScreenFlash").

A request's `wasted` is {"text": the word (default WASTED), "at_ms": when it starts (media
timeline; default: as late as lets the slow part reach the out point), "length_ms": how long the
ending lasts on screen (default and most: the fall sound's length), "sound": true}. The ending is
the end of the clip: its slow part takes length / 5 of the source from the start, and what the cut
has after that is left out; when the out point comes first, the last frame holds for the rest.
Without RIFE it plays at normal speed. The banner is drawn over the captions. The font is the
bundled League Spartan Black, or any font file set as [tools] wasted_font."""
import functools
import os

from . import media

FACTOR = 5                     # GTA V: SET_TIME_SCALE(0.2f)
DRAIN_S = 0.5                  # colour gone this long after it starts
BANNER_S = 1.5                 # the word lands this long after it starts
PUSH = 0.07                    # the push-in: 7% closer by the end
MIN_LENGTH_MS = 1000
DEFAULT_LENGTH_MS = 9400       # without the fall sound to measure
MIN_PART_MS = 200
RED = "0xD8262E"


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


def font():
    p = os.environ.get("SAYTHELINE_WASTED_FONT")
    return p if p and os.path.isfile(p) else media.font_path("League Spartan Black")


def spec_of(req, in_ms, out_ms, factor=FACTOR):
    """(start s from the cut's in point, length s on screen, text, sound), or None (also when
    `at_ms` is outside the cut)."""
    s = req.get("wasted")
    if not isinstance(s, dict) or not s.get("on", True):
        return None
    text = " ".join(str(s.get("text") or "WASTED").split())[:24] or "WASTED"
    most = max_length_ms()
    length = max(MIN_LENGTH_MS, min(most, int(s.get("length_ms") or most)))
    if s.get("at_ms") is None:
        at = max(in_ms, min(out_ms - MIN_PART_MS, out_ms - round(length / factor)))
    else:
        at = int(s["at_ms"])
        if not in_ms <= at <= out_ms - MIN_PART_MS:      # a preview of another part of the cut: not in it
            return None
    return (at - in_ms) / 1000, length / 1000, text, s.get("sound", True) is not False


def plan(spec, in_ms, out_ms, factor=FACTOR):
    """How the ending is made: (slow part (a, b, factor, "deep") in ms from the in point, or None
    at normal speed; the cut's new out point; the render's (start s, hold s, text, sound)). The
    start is the same on the slowed timeline, since the slow part begins there."""
    start, length, text, sound = spec
    a = round(start * 1000)
    b = min(out_ms - in_ms, a + round(length * 1000 / factor))
    hold = max(0.0, length - (b - a) * factor / 1000)
    part = (a, b, factor, "deep") if factor > 1 else None
    return part, in_ms + b, (start, round(hold, 3), text, sound)


def banner_s(spec):
    """When the word lands, from the cut's in point."""
    return spec[0] + BANNER_S


def filters(spec, w, h, tmp, dur_s):
    """The picture filters, after the captions. `dur_s`: the cut's length before the hold."""
    a, hold, text, _ = spec
    t0 = a + BANNER_S
    ease = f"(1-cos(PI*min(1,max(0,(t-{a:.3f})/{max(0.5, dur_s + hold - a):.3f}))))/2"   # 0 -> 1 to the end
    tf = os.path.join(tmp, "wasted.txt")
    with open(tf, "w", encoding="utf-8") as f:
        f.write(text.upper())
    band_y, band_h = round(h * 0.4), round(h * 0.2)
    size = f"min(h*0.21\\,w*1.5/{max(4, len(text))})"     # long words still fit across
    return [
        # drained of colour and a little darker; the push-in (scaled up, cropped back to size)
        f"hue=s='min(1\\,max(0\\,1-(t-{a:.3f})/{DRAIN_S}))':b='-0.6*min(1\\,max(0\\,(t-{a:.3f})/{DRAIN_S}))'",
        f"scale=w='2*trunc(iw*(1+{PUSH}*{ease})/2)':h=-2:eval=frame", f"crop={w}:{h}",
        # the band: two steps of black, a quick fade
        f"drawbox=x=0:y={band_y}:w=iw:h={band_h}:color=black@0.3:t=fill:enable='gte(t\\,{t0 - 0.05:.3f})'",
        f"drawbox=x=0:y={band_y}:w=iw:h={band_h}:color=black@0.3:t=fill:enable='gte(t\\,{t0 + 0.03:.3f})'",
        # the word slams in: from a third bigger, landing in 0.16 s
        f"drawtext=fontfile={media._fpath(font())}:textfile={media._fpath(tf)}:fontcolor={RED}"
        f":fontsize='{size}*(1+0.33*max(0\\,1-(t-{t0:.3f})/0.16))'"
        f":alpha='min(1\\,max(0\\,(t-{t0:.3f})/0.08))':borderw={max(2, round(h * 0.006))}:bordercolor=black@0.85"
        f":shadowx=0:shadowy={max(1, round(h * 0.006))}:shadowcolor=black@0.5:x=(w-text_w)/2:y=(h-text_h)/2",
    ] + ([f"tpad=stop_mode=clone:stop_duration={hold:.3f}"] if hold > 0 else [])


def audio_filters(spec, dur_s):
    """The clip's own sound (slowed with the picture): fades out under the fall sound by just after
    the word lands, padded through the hold."""
    a, hold, _, _ = spec
    t0 = a + BANNER_S
    end = dur_s + hold
    return [f"apad=whole_dur={end:.3f}", f"afade=t=out:st={a:.3f}:d={max(0.3, t0 + 0.5 - a):.3f}:curve=exp"]
