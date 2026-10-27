"""The "wasted" ending: the meme's game-over screen, in a look of its own. From a moment near the
end, the picture drains to grey and darkens while the camera pushes slowly in; then a dark band
and a big red word slam in over it, and the last frame holds with them for a beat while the sound
falls away under a low "fall" hit (sfx preset).

A request's `wasted` is {"text": the word (default WASTED), "at_ms": when it starts (media
timeline; default 1.5 s before the out point), "hold_ms": how long the last frame holds,
"sound": true}. Plain ffmpeg filters on the picture, so it works for clips, GIFs and stills; the
banner is drawn over the captions. The font is the bundled League Spartan Black, or any font file
set as [tools] wasted_font (Pricedown, say, which can't be shipped here)."""
import os

from . import media

LEAD_MS = 1500                 # by default it starts this long before the out point
DRAIN_S = 0.8                  # colour gone this long after it starts
BANNER_S = 0.9                 # the word lands this long after it starts
PUSH = 0.07                    # the push-in: 7% closer by the end
MAX_HOLD_MS = 4000
RED = "0xD8262E"


def font():
    p = os.environ.get("SAYTHELINE_WASTED_FONT")
    return p if p and os.path.isfile(p) else media.font_path("League Spartan Black")


def spec_of(req, in_ms, out_ms):
    """(start s from the cut's in point, hold s, text, sound), or None (also when `at_ms` is
    outside the cut)."""
    s = req.get("wasted")
    if not isinstance(s, dict) or not s.get("on", True):
        return None
    text = " ".join(str(s.get("text") or "WASTED").split())[:24] or "WASTED"
    if s.get("at_ms") is None:
        at = max(in_ms, out_ms - LEAD_MS)
    else:
        at = int(s["at_ms"])
        if not in_ms <= at <= out_ms - 200:      # a preview of another part of the cut: not in it
            return None
    hold = max(0, min(MAX_HOLD_MS, int(s.get("hold_ms", 1500))))
    return (at - in_ms) / 1000, hold / 1000, text, s.get("sound", True) is not False


def banner_s(spec):
    """When the word lands, from the cut's in point (the fall hit goes here)."""
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
    """The sound: falls away from the banner on, padded through the hold."""
    a, hold, _, _ = spec
    t0 = a + BANNER_S
    end = dur_s + hold
    return [f"apad=whole_dur={end:.3f}", f"afade=t=out:st={t0:.3f}:d={max(0.3, end - t0):.3f}:curve=exp"]
