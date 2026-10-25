"""Slow motion: one part of a cut played 2x or 4x slower, with the missing frames made up by RIFE
(rife-ncnn-vulkan, on the GPU through Vulkan) rather than repeated or blended.

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
                        "-of", "json", src], capture_output=True, text=True, timeout=30)
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


def master(src, in_ms, out_ms, part, chain, size, audio, tmp, progress=None):
    """The slowed cut as tmp/master.mkv: the source from in_ms to out_ms through the geometry chain
    (crop/scale/pad to the output size), with part = (a, b, factor, sound) slowed. `audio` is the
    source's audio track index, or None. Returns (path, fps). progress(done, total) while RIFE runs."""
    a, b, factor, sound = part
    tool = rife()
    if not tool:
        raise RuntimeError("slow motion needs rife-ncnn-vulkan (see README)")
    fps = frame_rate(src)
    w, h = size
    if estimate_s(b - a, factor, w, h, fps) > MAX_WORK_S:
        raise ValueError("that slow part would take over 5 minutes to make: pick a smaller Size or a shorter part")
    dur = (out_ms - in_ms) / 1000
    geo = ",".join(chain) if chain else "null"
    src_in = ["-ss", f"{in_ms / 1000:.3f}", "-t", f"{dur:.3f}", "-i", src]
    # 1. the slow part's frames at the output size, one folder per shot: RIFE interpolating across a
    #    camera cut morphs one shot into the next, so each shot is slowed on its own. Trimmed from
    #    the same -ss point as the master below, so the frames either side of each join are the ones
    #    the trims leave out.
    bounds = [a] + [c - in_ms for c in camera_cuts(src, in_ms + a, in_ms + b, fps)] + [b]
    shots = []
    for k, (x, y) in enumerate(zip(bounds, bounds[1:])):
        d = os.path.join(tmp, f"in{k}")
        os.makedirs(d)
        r = subprocess.run([*config.nice(), "ffmpeg", "-v", "error", "-nostdin", "-y", *src_in, "-map", "0:v:0",
                            "-vf", f"trim=start={x / 1000:.3f}:end={y / 1000:.3f},setpts=PTS-STARTPTS,{geo},setsar=1",
                            "-fps_mode", "passthrough", os.path.join(d, "%06d.png")], capture_output=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError("could not read the slow part's frames: " + r.stderr.decode(errors="replace").strip()[-300:])
        if os.listdir(d):
            shots.append(d)
    if not shots:
        raise RuntimeError("the slow part has no frames")
    # 2. RIFE: each shot's n frames in, n x factor out, evenly spaced in time; then all of them in
    #    order in one folder for the master
    frames_out = os.path.join(tmp, "out")
    os.makedirs(frames_out)
    total, done = sum(len(os.listdir(d)) for d in shots) * factor, 0
    for k, d in enumerate(shots):
        out_k = os.path.join(tmp, f"out{k}")
        os.makedirs(out_k)
        want = len(os.listdir(d)) * factor
        _interpolate(tool, d, out_k, want, tmp, lambda n: progress and progress(done + n, total))
        for f in sorted(os.listdir(out_k)):
            done += 1
            os.replace(os.path.join(out_k, f), os.path.join(frames_out, f"{done:08d}.jpg"))
    if progress:
        progress(total, total)
    # 3. the master: before | RIFE frames (played at the source rate, so `factor` times slower) | after.
    #    Only the parts there are: a slow part can start at the in point or end at the out point.
    sa, sb = a / 1000, b / 1000
    norm = f"setsar=1,fps={fps:.5f},format=yuv420p"
    parts = [("pre", f"trim=end={sa:.3f}", f"atrim=end={sa:.3f}")] if a > 0 else []
    parts.append(("slow", None, f"atrim=start={sa:.3f}:end={sb:.3f}"))
    if b < out_ms - in_ms:
        parts.append(("post", f"trim=start={sb:.3f}", f"atrim=start={sb:.3f}"))
    src_parts = [x for x in parts if x[0] != "slow"]
    graph = []
    if src_parts:
        graph.append("[0:v:0]" + (f"split={len(src_parts)}" if len(src_parts) > 1 else "null")
                     + "".join(f"[s{k}]" for k in range(len(src_parts))))
    for k, (name, trim, _) in enumerate(src_parts):
        graph.append(f"[s{k}]{trim},setpts=PTS-STARTPTS,{geo},{norm}[v{name}]")
    graph.append(f"[1:v]{norm}[vslow]")
    if audio is not None:
        slow_a = {"deep": f"asetrate={48000 // factor},aresample=48000",
                  "pitch": ",".join(["atempo=0.5"] * (2 if factor == 4 else 1)),
                  "mute": f"asetrate={48000 // factor},aresample=48000,volume=0"}[sound]
        fade = "afade=t=in:d=0.01,areverse,afade=t=in:d=0.01,areverse"       # no click at the joins
        graph.append(f"[0:a:{audio}]aresample=48000,aformat=channel_layouts=stereo,"
                     + (f"asplit={len(parts)}" if len(parts) > 1 else "anull") + "".join(f"[x{k}]" for k in range(len(parts))))
        for k, (name, _, atrim) in enumerate(parts):
            graph.append(f"[x{k}]{atrim},asetpts=PTS-STARTPTS,{slow_a + ',' if name == 'slow' else ''}{fade}[a{name}]")
        graph.append("".join(f"[v{n}][a{n}]" for n, _, _ in parts) + f"concat=n={len(parts)}:v=1:a=1[v][a]")
        maps = ["-map", "[v]", "-map", "[a]", "-c:a", "pcm_s16le"]
    else:
        graph.append("".join(f"[v{n}]" for n, _, _ in parts) + f"concat=n={len(parts)}:v=1:a=0[v]")
        maps = ["-map", "[v]"]
    out = os.path.join(tmp, "master.mkv")
    r = subprocess.run([*config.nice(), "ffmpeg", "-v", "error", "-nostdin", "-y", *src_in,
                        "-framerate", f"{fps:.5f}", "-i", os.path.join(frames_out, "%08d.jpg"),
                        "-filter_complex", ";".join(graph), *maps,
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p", out],
                       capture_output=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()[-500:])
    return out, fps
