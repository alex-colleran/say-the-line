"""Speed ramps on the beat: in a Studio cut with a song, the picture speeds up and slows down within
every beat (or bar), easing along a curve, slow between beats and fast on them (or the reverse).

Each beat averages normal speed, so a clip still lasts as long as it did and joins placed on the beat
stay there; within a segment the curve is scaled so the segment covers exactly its own source. The
in-between frames for the slow stretches come from RIFE (as slow motion's do, shot by shot); each
output frame then takes the made-up frame nearest the source moment it should show.

Times here are seconds from the segment's start unless they say otherwise. speed() = JS rampSpeed."""
import bisect
import math
import os
import subprocess

from . import config, slowmo

STRENGTH = {"subtle": 2.0, "strong": 4.0, "extreme": 8.0}      # fastest / slowest speed
SHAPES = {"smooth": 1, "sharp": 4}                             # how tightly the fast part hugs the beat
OUT_FPS = 30
STEPS = 2000                                                   # samples per beat for the mean


def weight(p, shape):
    """0..1 within a beat: 1 on the beat (p = 0 or 1), 0 halfway between."""
    return ((1 + math.cos(2 * math.pi * p)) / 2) ** SHAPES.get(shape, 1)


def _mean_weight(shape):
    return sum(weight((i + 0.5) / STEPS, shape) for i in range(STEPS)) / STEPS


def speed(p, strength="strong", shape="smooth", fast_on_beat=True):
    """The speed at phase p of a beat, averaging 1 over the beat."""
    r = STRENGTH.get(strength, 4.0)
    m = _mean_weight(shape)
    w = weight(p, shape) if fast_on_beat else 1 - weight(p, shape)
    mean_w = m if fast_on_beat else 1 - m
    slow = 1 / (1 + (r - 1) * mean_w)
    return slow * (1 + (r - 1) * w)


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


def source_times(length_out, length_src, beats, opts, fps=OUT_FPS):
    """For each output frame of a segment, the source time it shows (s from the segment's in point).
    beats: the song's beats on the segment's own output timeline. The cumulative speed is scaled so
    the last frame lands at the end of the source."""
    n = max(1, round(length_out * fps))
    fast = opts.get("direction", "beat") == "beat"
    v = [speed(phase((i + 0.5) / fps, beats), opts.get("strength"), opts.get("shape"), fast) for i in range(n)]
    acc, cum = 0.0, []
    for x in v:
        cum.append(acc + x / 2)          # the middle of each frame
        acc += x
    k = length_src / acc if acc else 0          # frame-speeds summed -> source seconds
    return [c * k for c in cum]


def slowest(opts):
    fast = opts.get("direction", "beat") == "beat"
    return min(speed(i / 200, opts.get("strength"), opts.get("shape"), fast) for i in range(200))


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
    return max(1, min(8, math.ceil(OUT_FPS * slowest(opts) ** -1 / src_fps - 1e-9)))


def estimate_s(length_s, opts, w, h, src_fps=24):
    return length_s * src_fps * dense_factor(opts, src_fps) * (w * h / 1e6) * slowmo.SECS_PER_MPIX_FRAME


def master(src, in_ms, out_ms, length_out, beats, opts, chain, size, tmp, progress=None):
    """The ramped segment as tmp/ramp.mkv (silent, OUT_FPS, at the output size) and its source times."""
    tool = slowmo.rife()
    if not tool:
        raise RuntimeError("speed ramps need rife-ncnn-vulkan (see README)")
    fps = slowmo.frame_rate(src)
    k = dense_factor(opts, fps)
    geo = ",".join(chain) if chain else "null"
    dur = (out_ms - in_ms) / 1000
    src_in = ["-ss", f"{in_ms / 1000:.3f}", "-t", f"{dur:.3f}", "-i", src]
    # the source's frames at the output size, k times as many, one camera shot at a time
    bounds = [0] + [c - in_ms for c in slowmo.camera_cuts(src, in_ms, out_ms, fps)] + [out_ms - in_ms]
    dense = os.path.join(tmp, "dense")
    os.makedirs(dense)
    jobs = []
    for j, (p, q) in enumerate(zip(bounds, bounds[1:])):
        d = os.path.join(tmp, f"rin{j}")
        os.makedirs(d)
        r = subprocess.run([*config.nice(), "ffmpeg", "-v", "error", "-nostdin", "-y", *src_in, "-map", "0:v:0",
                            "-vf", f"trim=start={p / 1000:.3f}:end={q / 1000:.3f},setpts=PTS-STARTPTS,{geo},setsar=1",
                            "-fps_mode", "passthrough", os.path.join(d, "%06d.png")], capture_output=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError("could not read the frames: " + r.stderr.decode(errors="replace").strip()[-300:])
        if os.listdir(d):
            jobs.append((d, len(os.listdir(d)) * k))
    total, done = sum(want for _, want in jobs), 0
    for j, (d, want) in enumerate(jobs):
        out_j = os.path.join(tmp, f"rout{j}")
        os.makedirs(out_j)
        if k > 1:
            slowmo._interpolate(tool, d, out_j, want, tmp, lambda n: progress and progress(done + n, total))
        else:
            for f in sorted(os.listdir(d)):
                os.replace(os.path.join(d, f), os.path.join(out_j, f))
        for f in sorted(os.listdir(out_j)):
            done += 1
            os.replace(os.path.join(out_j, f), os.path.join(dense, f"{done:08d}{os.path.splitext(f)[1]}"))
    if not done:
        raise RuntimeError("no frames in that clip")
    frames = sorted(os.listdir(dense))
    times = source_times(length_out, dur, beats, opts)
    step = dur / len(frames)                       # each dense frame covers this much source time
    seq = os.path.join(tmp, "rseq")
    os.makedirs(seq)
    ext = os.path.splitext(frames[0])[1]
    for i, t in enumerate(times):
        f = frames[min(len(frames) - 1, max(0, int(t / step)))]
        os.link(os.path.join(dense, f), os.path.join(seq, f"{i + 1:08d}{ext}"))
    out = os.path.join(tmp, "ramp.mkv")
    r = subprocess.run([*config.nice(), "ffmpeg", "-v", "error", "-nostdin", "-y", "-framerate", str(OUT_FPS),
                        "-i", os.path.join(seq, f"%08d{ext}"), "-vf", "setsar=1,format=yuv420p",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", out], capture_output=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()[-400:])
    if progress:
        progress(total, total)
    return out, times
