"""Slow motion and smooth playback: one part of a cut played 2x or 4x slower, and/or the whole cut at
60 fps, with the missing frames made up by RIFE (rife-ncnn-vulkan, on the GPU through Vulkan)
rather than repeated or blended.

The slowed cut is built first as an intermediate video (`master`): the part before at normal
speed, the slow part from RIFE's frames, the part after, already cropped and scaled to the output
size, sound included. media.render then makes the clip from it exactly as from any source, with
the captions moved onto its timeline by `remap_cues`, so formats, caption looks and dialogue
separation all work unchanged.

Times here are ms from the cut's in point unless they say otherwise."""
import json
import os
import shutil
import subprocess
import time

from . import config

FACTORS = (2, 4)
SOUNDS = ("deep", "pitch", "mute")   # pitched down like slowed tape / stretched at the same pitch / silent
MAX_PART_MS = 5000                   # of source time; 4x makes that 20 s on screen
MIN_PART_MS = 200
# RIFE's cost is about this many seconds per megapixel per frame it writes on the Radeon 680M
# (Vulkan): 0.24 s a 1920x800 frame, 0.07 s a 1152x480 one. Used for the estimate and the limit.
SECS_PER_MPIX_FRAME = 0.15
MAX_WORK_S = 300                     # a slow part estimated to take longer than this is refused
SMOOTH_FPS = (60,)                   # "smooth": every frame in between made up, at normal speed
SMOOTH_MIN_GAIN = 1.15               # a source within this of the target (50 fps for 60) is left alone


def rife():
    """(binary, model dir) or None. SAYTHELINE_RIFE names the binary, else rife-ncnn-vulkan on PATH;
    the model is SAYTHELINE_RIFE_MODEL, else rife-v4.6 beside the binary."""
    b = os.environ.get("SAYTHELINE_RIFE") or shutil.which("rife-ncnn-vulkan")
    if not b or not os.path.isfile(b):
        return None
    m = os.environ.get("SAYTHELINE_RIFE_MODEL") or os.path.join(os.path.dirname(os.path.realpath(b)), "rife-v4.6")
    return (b, m) if os.path.isdir(m) else None


def part_of(req, in_ms, out_ms):
    """The request's slow part as (a, b, factor, sound), times from the in point, or None when the
    clip is not slowed. Raises ValueError for a part that cannot be made."""
    s = req.get("slow") or {}
    factor = int(s.get("factor") or 1)
    if factor == 1:
        return None
    if factor not in FACTORS:
        raise ValueError("slow motion is 2x or 4x")
    a = max(in_ms, int(s.get("a_ms", in_ms))) - in_ms
    b = min(out_ms, int(s.get("b_ms", out_ms))) - in_ms
    if b - a < MIN_PART_MS:
        raise ValueError("the slow part is too short")
    if b - a > MAX_PART_MS:
        raise ValueError(f"slow motion works on up to {MAX_PART_MS // 1000} s at a time: mark a shorter part")
    sound = s.get("sound") if s.get("sound") in SOUNDS else "deep"
    return a, b, factor, sound


def timemap(t, a, b, factor):
    """Where time t of the cut lands once [a, b) plays `factor` times slower."""
    if t <= a:
        return t
    if t <= b:
        return a + (t - a) * factor
    return t + (b - a) * (factor - 1)


def length(dur, a, b, factor):
    return timemap(dur, a, b, factor)


def remap_cues(cues, in_ms, a, b, factor):
    """Caption lines (media timeline, words included) moved onto the slowed cut's timeline, which
    starts at 0. A word inside the slow part lasts `factor` times as long, like the picture."""
    def m(t):
        return round(timemap(t - in_ms, a, b, factor))
    out = []
    for c in cues:
        d = dict(c, start_ms=m(c["start_ms"]), end_ms=m(c["end_ms"]))
        if c.get("words"):
            d["words"] = [dict(w, s=m(w["s"]), e=m(w["e"])) for w in c["words"]]
        out.append(d)
    return out


def estimate_s(part_ms, factor, w, h, fps):
    """Seconds RIFE should take for a part at an output size."""
    return part_ms / 1000 * fps * factor * (w * h / 1e6) * SECS_PER_MPIX_FRAME


def frame_rate(src):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate,r_frame_rate",
                        "-of", "json", src], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    s = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
    for k in ("avg_frame_rate", "r_frame_rate"):
        n, _, d = (s.get(k) or "0/0").partition("/")
        try:
            f = float(n) / float(d or 1)
        except (ValueError, ZeroDivisionError):
            continue
        if 5 <= f <= 120:
            return f
    return 24000 / 1001


def camera_cuts(src, a_ms, b_ms, fps):
    """Camera cuts strictly inside [a_ms, b_ms) on the media timeline (shots.find_cuts), at least
    half a frame from either end. Best effort: none if the scan fails."""
    from . import shots
    half = 500 / fps
    try:
        _, _, cuts = shots.find_cuts(shots.decode(src, max(0, a_ms - shots.LEAD_MS), b_ms + shots.TAIL_MS))
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return []
    return sorted(c for c, _ in cuts if a_ms + half < c < b_ms - half)


def _interpolate(tool, frames_in, frames_out, want, tmp, progress):
    """RIFE from one folder of PNG frames to `want` JPEG frames. A one-frame shot is just repeated."""
    n = len(os.listdir(frames_in))
    if n == 1:
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-loop", "1", "-i", os.path.join(frames_in, "000001.png"),
                            "-frames:v", str(want), "-q:v", "2", os.path.join(frames_out, "%08d.jpg")], capture_output=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode(errors="replace").strip()[-300:])
        return
    log = os.path.join(tmp, "rife.log")       # a file, not a pipe: nothing reads it until the end
    with open(log, "wb") as lf:
        p = subprocess.Popen([*config.nice(), tool[0], "-i", frames_in, "-o", frames_out, "-m", tool[1], "-n", str(want),
                              "-f", "jpg"], stdout=subprocess.DEVNULL, stderr=lf)
        t0 = time.time()
        while p.poll() is None:
            time.sleep(0.5)
            progress(len(os.listdir(frames_out)))
            if time.time() - t0 > MAX_WORK_S * 2:
                p.kill()
                p.wait()
                raise RuntimeError("frame interpolation took too long")
    with open(log, "rb") as lf:
        err = lf.read()[-2000:].decode(errors="replace")
    made = len(os.listdir(frames_out))
    if p.returncode != 0 or made < want:
        raise RuntimeError(f"frame interpolation failed ({made} of {want} frames): " + err.strip()[-300:])


def smooth_of(req, src_fps=None):
    """The output frame rate asked for (`smooth`: 60), or 0 for the source's own. A source already
    near it (a 50 or 60 fps file) is left as it is."""
    want = int(req.get("smooth") or 0)
    if want not in (0,) + SMOOTH_FPS:
        raise ValueError(f"smooth playback is {' or '.join(map(str, SMOOTH_FPS))} fps")
    if want and src_fps and src_fps * SMOOTH_MIN_GAIN >= want:
        return 0
    return want


def work_s(parts, fps, out_fps, w, h):
    """Seconds RIFE should take for these parts [(a, b, factor)] at an output size and frame rate."""
    return sum(estimate_s(b - a, f * out_fps / fps, w, h, fps) for a, b, f in parts if f * out_fps / fps > 1.001)


def master(src, in_ms, out_ms, part, chain, size, audio, tmp, progress=None, smooth=0):
    """The cut as tmp/master.mkv, ready for media.render: the source from in_ms to out_ms through the
    geometry chain (crop/scale/pad to the output size), with part = (a, b, factor, sound) slowed
    (or None), at `smooth` fps when that is set (0: the source's rate). Every part that needs frames
    the source doesn't have goes through RIFE. `audio` is the source's audio track index, or None.
    Returns (path, fps). progress(done, total) while RIFE runs."""
    tool = rife()
    if not tool:
        raise RuntimeError("slow motion and smooth playback need rife-ncnn-vulkan (see README)")
    fps = frame_rate(src)
    out_fps = smooth or fps
    w, h = size
    dur_ms = out_ms - in_ms
    a, b, factor, sound = part or (0, dur_ms, 1, "deep")
    spans = [x for x in ((0, a, 1), (a, b, factor), (b, dur_ms, 1)) if x[1] - x[0] > 0]
    if work_s(spans, fps, out_fps, w, h) > MAX_WORK_S:
        raise ValueError("that would take over 5 minutes to make: pick a smaller Size, a shorter clip or a shorter slow part")
    geo = ",".join(chain) if chain else "null"
    src_in = ["-ss", f"{in_ms / 1000:.3f}", "-t", f"{dur_ms / 1000:.3f}", "-i", src]
    # Each span is either trimmed straight from the source (normal speed at its own rate) or made of
    # frames: read at the output size, one folder per camera shot (RIFE across a cut morphs one shot
    # into the next), then RIFE makes n x factor x out_fps / fps of them, played at out_fps. All are
    # trimmed from the same -ss point, so the frames either side of each join are the ones the trims
    # leave out.
    made, jobs = [], []          # made: (span index, frames folder); jobs: (shot folder, frames wanted, span index)
    for i, (x, y, f) in enumerate(spans):
        k = f * out_fps / fps
        if k <= 1.001:
            continue
        bounds = [x] + [c - in_ms for c in camera_cuts(src, in_ms + x, in_ms + y, fps)] + [y]
        for j, (p, q) in enumerate(zip(bounds, bounds[1:])):
            d = os.path.join(tmp, f"in{i}_{j}")
            os.makedirs(d)
            r = subprocess.run([*config.nice(), "ffmpeg", "-v", "error", "-nostdin", "-y", *src_in, "-map", "0:v:0",
                                "-vf", f"trim=start={p / 1000:.3f}:end={q / 1000:.3f},setpts=PTS-STARTPTS,{geo},setsar=1",
                                "-fps_mode", "passthrough", os.path.join(d, "%06d.png")], capture_output=True, timeout=300)
            if r.returncode != 0:
                raise RuntimeError("could not read the frames: " + r.stderr.decode(errors="replace").strip()[-300:])
            n = len(os.listdir(d))
            if n:
                jobs.append((d, max(1, round(n * k)), i))
        made.append(i)
    if not jobs and made:
        raise RuntimeError("no frames to slow down")
    total, done = sum(want for _, want, _ in jobs), 0
    folders = {}
    for k, (d, want, i) in enumerate(jobs):
        out_k = os.path.join(tmp, f"out{k}")
        os.makedirs(out_k)
        _interpolate(tool, d, out_k, want, tmp, lambda n: progress and progress(done + n, total))
        dest = folders.setdefault(i, os.path.join(tmp, f"span{i}"))
        os.makedirs(dest, exist_ok=True)
        for fname in sorted(os.listdir(out_k)):
            done += 1
            os.replace(os.path.join(out_k, fname), os.path.join(dest, f"{len(os.listdir(dest)) + 1:08d}.jpg"))
    if progress and total:
        progress(total, total)
    # the master: the spans in order, each from the source or from its frames, at out_fps
    norm = f"setsar=1,fps={out_fps:.5f},format=yuv420p"
    inputs, graph = [], []
    straight = [i for i in range(len(spans)) if i not in folders]
    if straight:
        graph.append("[0:v:0]" + (f"split={len(straight)}" if len(straight) > 1 else "null")
                     + "".join(f"[s{i}]" for i in straight))
    for i, (x, y, f) in enumerate(spans):
        if i in folders:
            inputs += ["-framerate", f"{out_fps:.5f}", "-i", os.path.join(folders[i], "%08d.jpg")]
            graph.append(f"[{len(inputs) // 4}:v]{norm}[v{i}]")
        else:
            graph.append(f"[s{i}]trim=start={x / 1000:.3f}:end={y / 1000:.3f},setpts=PTS-STARTPTS,{geo},{norm}[v{i}]")
    if audio is not None:
        slow_a = {"deep": f"asetrate={48000 // max(1, factor)},aresample=48000",
                  "pitch": ",".join(["atempo=0.5"] * (2 if factor == 4 else 1)) or "anull",
                  "mute": f"asetrate={48000 // max(1, factor)},aresample=48000,volume=0"}[sound]
        fade = "afade=t=in:d=0.01,areverse,afade=t=in:d=0.01,areverse"       # no click at the joins
        graph.append(f"[0:a:{audio}]aresample=48000,aformat=channel_layouts=stereo,"
                     + (f"asplit={len(spans)}" if len(spans) > 1 else "anull") + "".join(f"[x{i}]" for i in range(len(spans))))
        for i, (x, y, f) in enumerate(spans):
            graph.append(f"[x{i}]atrim=start={x / 1000:.3f}:end={y / 1000:.3f},asetpts=PTS-STARTPTS,"
                         f"{slow_a + ',' if f > 1 else ''}{fade}[a{i}]")
        graph.append("".join(f"[v{i}][a{i}]" for i in range(len(spans))) + f"concat=n={len(spans)}:v=1:a=1[v][a]")
        maps = ["-map", "[v]", "-map", "[a]", "-c:a", "pcm_s16le"]
    else:
        graph.append("".join(f"[v{i}]" for i in range(len(spans))) + f"concat=n={len(spans)}:v=1:a=0[v]")
        maps = ["-map", "[v]"]
    out = os.path.join(tmp, "master.mkv")
    r = subprocess.run([*config.nice(), "ffmpeg", "-v", "error", "-nostdin", "-y", *src_in, *inputs,
                        "-filter_complex", ";".join(graph), *maps,
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p", out],
                       capture_output=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()[-500:])
    return out, out_fps
