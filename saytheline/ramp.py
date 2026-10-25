"""Speed ramps on the beat: in a Studio cut with a song, the picture speeds up and slows down within
every beat (or bar), easing along a curve, slow between beats and fast on them (or the reverse).

Each beat averages normal speed, so a clip still lasts as long as it did and joins placed on the beat
stay there; within a segment the curve is scaled so the segment covers exactly its own source. The
in-between frames for the slow stretches come from RIFE (as slow motion's do, shot by shot); each
output frame then takes the made-up frame nearest the source moment it should show.

A strength is the fastest and slowest speed. Averaging 1 then fixes how long the fast part lasts:
the faster the peak, the shorter the burst (a whip on the beat for Extreme). The curve is how the
speed gets there: Smooth eases all the way, Sharp holds slow and snaps to fast.

Lines can be kept: the ramp then eases back to normal speed for each spoken line, which plays as it
was, with its sound, and the ramp happens between the lines. Each stretch between lines still
covers its own source exactly, so a kept line sits at the same moment of the cut, in step.

Times here are seconds from the segment's start unless they say otherwise. speed() = JS rampSpeed."""
import bisect
import functools
import math
import os
import subprocess

from . import config, slowmo

STRENGTH = {"subtle": (1.8, 0.55), "strong": (3.5, 0.3), "extreme": (6.0, 0.15)}   # (fastest, slowest)
SHAPES = ("smooth", "sharp")
# how far ahead of the beat the curve's peak lands (s): motion reads as late, so a peak a little
# before the beat feels on it
PEAK_LEAD = {"on": 0.0, "before": 0.08, "earlier": 0.16}
EDGE = 0.08                    # Sharp: each switch between slow and fast takes this share of a beat
OUT_FPS = 30
STEPS = 2000                   # samples per beat for the mean
MAX_DENSE = 10                 # RIFE frames per source frame at most
# kept lines: a little room around each, gaps shorter than this joined, and the ease back to normal
KEEP_PRE_S, KEEP_POST_S, KEEP_GAP_S, EASE_S = 0.08, 0.15, 0.6, 0.25
SOUND_FADE_S = 0.04


def _bump(p, shape, c):
    """0..1 within a beat: 1 on the beat (p = 0 or 1), falling off towards halfway between."""
    if shape == "sharp":
        a, e = c
        d = min(p % 1, 1 - p % 1)
        if d <= a:
            return 1.0
        return (1 + math.cos(math.pi * (d - a) / e)) / 2 if d < a + e else 0.0
    return ((1 + math.cos(2 * math.pi * p)) / 2) ** c


def _mean(shape, c):
    return sum(_bump((i + 0.5) / STEPS, shape, c) for i in range(STEPS)) / STEPS


@functools.lru_cache(maxsize=64)
def _curve(strength, shape, fast_on_beat):
    """(fastest, slowest, shape constant, mean speed) for a choice: the bump whose average makes the
    beat average normal speed."""
    hi, lo = STRENGTH.get(strength, STRENGTH["strong"])
    shape = shape if shape in SHAPES else "smooth"
    m = (1 - lo) / (hi - lo) if fast_on_beat else (hi - 1) / (hi - lo)      # the bump's mean it needs
    if shape == "sharp":
        e = min(EDGE, m)
        c = (min(0.5 - e, (m - e) / 2), e)
    else:                       # the power that gives that mean (mean falls as the power rises)
        a, b = -8.0, 8.0
        for _ in range(60):
            k = (a + b) / 2
            a, b = (k, b) if _mean(shape, math.exp(k)) > m else (a, k)
        c = math.exp((a + b) / 2)
    v = [_speed_raw((i + 0.5) / STEPS, hi, lo, shape, c, fast_on_beat) for i in range(STEPS)]
    return hi, lo, shape, c, sum(v) / STEPS


def _speed_raw(p, hi, lo, shape, c, fast_on_beat):
    g = _bump(p, shape, c)
    return lo + (hi - lo) * g if fast_on_beat else hi - (hi - lo) * g


def speed(p, strength="strong", shape="smooth", fast_on_beat=True):
    """The speed at phase p of a beat, averaging 1 over the beat."""
    hi, lo, shape, c, mean = _curve(strength, shape, bool(fast_on_beat))
    return _speed_raw(p, hi, lo, shape, c, fast_on_beat) / mean


def keep_spans(cues, in_ms, out_ms):
    """The stretches (s from the segment's start) that play at normal speed: each spoken line with a
    little room, near ones joined, and a sliver left at either end taken in too."""
    dur = (out_ms - in_ms) / 1000
    spans = []
    for c in sorted(cues or [], key=lambda c: c["start_ms"]):
        a = max(0.0, (c["start_ms"] - in_ms) / 1000 - KEEP_PRE_S)
        b = min(dur, (c["end_ms"] - in_ms) / 1000 + KEEP_POST_S)
        if b <= a:
            continue
        if spans and a - spans[-1][1] < KEEP_GAP_S:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    if spans and spans[0][0] < KEEP_GAP_S / 2:
        spans[0][0] = 0.0
    if spans and dur - spans[-1][1] < KEEP_GAP_S / 2:
        spans[-1][1] = dur
    return [(round(a, 3), round(b, 3)) for a, b in spans]


def phase(t, beats):
    """Where t falls between beats (0 = on a beat). Beyond the grid, the nearest spacing repeats."""
    if len(beats) < 2:
        return 0.0
    i = bisect.bisect_right(beats, t) - 1
    if i < 0:
        step = beats[1] - beats[0]
        return ((t - beats[0]) / step) % 1
    if i >= len(beats) - 1:
        step = beats[-1] - beats[-2]
        return ((t - beats[-1]) / step) % 1
    return (t - beats[i]) / (beats[i + 1] - beats[i])


def _kept(t, keep):
    return any(a <= t < b for a, b in keep)


def source_times(length_out, length_src, beats, opts, fps=OUT_FPS, keep=()):
    """For each output frame of a segment, the source time it shows (s from the segment's in point).
    beats: the song's beats on the segment's own output timeline. keep: keep_spans(), shown at
    normal speed (the ramp eases back to it over EASE_S). Each stretch between them is scaled so it
    covers exactly its own source; without any, the whole segment is one stretch."""
    n = max(1, round(length_out * fps))
    scale = length_src / length_out if length_out else 1.0
    fast = opts.get("direction", "beat") == "beat"
    lead = PEAK_LEAD.get(opts.get("peak"), PEAK_LEAD["before"])
    mids = [(i + 0.5) / fps for i in range(n)]
    v = []
    for t in mids:
        x = speed(phase(t + lead, beats), opts.get("strength"), opts.get("shape"), fast)
        if keep:
            d = min(min(abs(t - a), abs(t - b)) for a, b in keep)
            x = 1.0 if _kept(t, keep) else 1 + (x - 1) * min(1.0, d / EASE_S)
        v.append(x)
    kept = [bool(keep) and _kept(t, keep) for t in mids]
    out, i = [0.0] * n, 0
    while i < n:
        j = i
        while j < n and kept[j] == kept[i]:
            j += 1
        if kept[i]:
            for f in range(i, j):
                out[f] = mids[f] * scale
        else:            # frames i..j-1 cover source [i / fps, j / fps] (x scale)
            acc, cum = 0.0, []
            for f in range(i, j):
                cum.append(acc + v[f] / 2)          # the middle of each frame
                acc += v[f]
            k = (j - i) / fps * scale / acc if acc else 0
            for f, c in zip(range(i, j), cum):
                out[f] = i / fps * scale + c * k
        i = j
    return out


def slowest(opts):
    return STRENGTH.get(opts.get("strength"), STRENGTH["strong"])[1] / _curve(
        opts.get("strength"), opts.get("shape"), opts.get("direction", "beat") == "beat")[4]


def map_time(t_src, src_times, fps=OUT_FPS):
    """Output time (s) at which a source time is shown: the inverse of source_times, for captions."""
    i = bisect.bisect_left(src_times, t_src)
    if i <= 0:
        return 0.0
    if i >= len(src_times):
        return len(src_times) / fps
    a, b = src_times[i - 1], src_times[i]
    return (i - 1 + 0.5 + (t_src - a) / max(1e-6, b - a)) / fps


def remap_cues(cues, in_ms, src_times):
    def m(t):
        return round(1000 * map_time((t - in_ms) / 1000, src_times))
    out = []
    for c in cues:
        d = dict(c, start_ms=m(c["start_ms"]), end_ms=m(c["end_ms"]))
        if c.get("words"):
            d["words"] = [dict(w, s=m(w["s"]), e=m(w["e"])) for w in c["words"]]
        out.append(d)
    return out


def dense_factor(opts, src_fps):
    """How many frames RIFE makes per source frame so the slowest stretch still gets a new frame
    every output frame."""
    return max(1, min(MAX_DENSE, math.ceil(OUT_FPS / slowest(opts) / src_fps - 1e-9)))


def estimate_s(length_s, opts, w, h, src_fps=24, kept_s=0.0):
    """Seconds of RIFE work: kept lines need none."""
    return max(0.0, length_s - kept_s) * src_fps * dense_factor(opts, src_fps) * (w * h / 1e6) * slowmo.SECS_PER_MPIX_FRAME


def _gated_sound(src, in_ms, dur, track, keep, out):
    """The segment's own sound, heard only in the kept stretches (faded a few ms at each edge)."""
    f = SOUND_FADE_S
    gate = "+".join(f"clip((t-{a:.3f})/{f},0,1)*clip(({b:.3f}-t)/{f},0,1)" for a, b in keep)
    r = subprocess.run([*config.nice(), "ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{in_ms / 1000:.3f}",
                        "-t", f"{dur:.3f}", "-i", src, "-map", f"0:a:{track}", "-vn", "-af",
                        f"asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
                        f"aeval=exprs='val(0)*({gate})|val(1)*({gate})',apad=whole_dur={dur:.3f}",
                        "-t", f"{dur:.3f}", "-c:a", "flac", out], capture_output=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError("could not keep the lines' sound: " + r.stderr.decode(errors="replace").strip()[-300:])


def master(src, in_ms, out_ms, length_out, beats, opts, chain, size, tmp, progress=None, keep=(), track=None):
    """The ramped segment as tmp/ramp.mkv (at OUT_FPS and the output size) and its source times.
    Silent, unless lines are kept (keep, with the source's audio `track`): then it carries their sound."""
    tool = slowmo.rife()
    if not tool:
        raise RuntimeError("speed ramps need rife-ncnn-vulkan (see README)")
    fps = slowmo.frame_rate(src)
    k = dense_factor(opts, fps)
    geo = ",".join(chain) if chain else "null"
    dur = (out_ms - in_ms) / 1000
    src_in = ["-ss", f"{in_ms / 1000:.3f}", "-t", f"{dur:.3f}", "-i", src]
    # the source's frames at the output size, k times as many: one camera shot at a time, and a kept
    # line just repeats its frames (it plays at normal speed, so it needs no made-up ones)
    cuts = [c - in_ms for c in slowmo.camera_cuts(src, in_ms, out_ms, fps)]
    edges = {round(x * 1000) for a, b in keep for x in (a, b)}
    bounds = sorted({0, out_ms - in_ms, *cuts, *(e for e in edges if 0 < e < out_ms - in_ms)})
    dense = os.path.join(tmp, "dense")
    os.makedirs(dense)
    jobs = []
    for j, (p, q) in enumerate(zip(bounds, bounds[1:])):
        d = os.path.join(tmp, f"rin{j}")
        os.makedirs(d)
        plain = k == 1 or _kept((p + q) / 2000, keep)
        r = subprocess.run([*config.nice(), "ffmpeg", "-v", "error", "-nostdin", "-y", *src_in, "-map", "0:v:0",
                            "-vf", f"trim=start={p / 1000:.3f}:end={q / 1000:.3f},setpts=PTS-STARTPTS,{geo},setsar=1",
                            "-fps_mode", "passthrough", *(["-q:v", "2", os.path.join(d, "%06d.jpg")] if plain and k > 1
                                                         else [os.path.join(d, "%06d.png")])],
                           capture_output=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError("could not read the frames: " + r.stderr.decode(errors="replace").strip()[-300:])
        if os.listdir(d):
            jobs.append((d, len(os.listdir(d)) * k, plain))
    total = sum(want for _, want, plain in jobs if not plain) or 1
    done = made = 0
    for j, (d, want, plain) in enumerate(jobs):
        out_j = os.path.join(tmp, f"rout{j}")
        os.makedirs(out_j)
        if plain:
            for f in sorted(os.listdir(d)):
                for r_ in range(k):
                    os.link(os.path.join(d, f), os.path.join(out_j, f"{f}.{r_:02d}{os.path.splitext(f)[1]}"))
        else:
            slowmo._interpolate(tool, d, out_j, want, tmp, lambda n: progress and progress(done + n, total))
            done += want
        for f in sorted(os.listdir(out_j)):
            made += 1
            os.replace(os.path.join(out_j, f), os.path.join(dense, f"{made:08d}{os.path.splitext(f)[1]}"))
    if not made:
        raise RuntimeError("no frames in that clip")
    frames = sorted(os.listdir(dense))
    times = source_times(length_out, dur, beats, opts, keep=keep)
    step = dur / len(frames)                       # each dense frame covers this much source time
    seq = os.path.join(tmp, "rseq")
    os.makedirs(seq)
    ext = os.path.splitext(frames[0])[1]
    for i, t in enumerate(times):
        f = frames[min(len(frames) - 1, max(0, int(t / step)))]
        os.link(os.path.join(dense, f), os.path.join(seq, f"{i + 1:08d}{ext}"))
    out = os.path.join(tmp, "ramp.mkv")
    sound = []
    if keep and track is not None:
        wav = os.path.join(tmp, "kept.flac")
        _gated_sound(src, in_ms, len(times) / OUT_FPS, track, keep, wav)
        sound = ["-i", wav, "-map", "0:v", "-map", "1:a", "-c:a", "copy"]
    r = subprocess.run([*config.nice(), "ffmpeg", "-v", "error", "-nostdin", "-y", "-framerate", str(OUT_FPS),
                        "-i", os.path.join(seq, f"%08d{ext}"), *sound, "-vf", "setsar=1,format=yuv420p",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", out], capture_output=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()[-400:])
    if progress:
        progress(total, total)
    return out, times
