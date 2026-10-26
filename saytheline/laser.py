"""Laser eyes: the meme's red-hot glare on someone's eyes, for a part of the cut or a still.

Optional, like stem audio: it needs MediaPipe (pip install mediapipe) in some Python, and its face
model, face_landmarker.task (README). The work happens in laser_worker.py, run in that Python;
this side works out what to ask it for.

A request's `laser` is {"style": "flare" | "beams", "a_ms", "b_ms": the part with lasers (media
timeline), "pick": {"x", "y"} or null}. `pick` is where the owner tapped the face, as a fraction of
the source picture; without it the lasers go on the largest face in each shot."""
import functools
import json
import os
import subprocess

from . import config, media

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "laser_worker.py")
STYLES = ("flare", "beams")
MIN_PART_MS = 200


@functools.lru_cache(maxsize=1)
def available():
    """True when the configured Python can import MediaPipe and the face model is there."""
    py, model = config.laser_python(), config.laser_model()
    if not (py and os.path.exists(py) and model and os.path.isfile(model)):
        return False
    try:
        r = subprocess.run([py, "-I", "-c", "import mediapipe, cv2"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def need():
    if not available():
        raise ValueError("laser eyes need MediaPipe and its face model on the server (see README)")


def part_of(req, in_ms, out_ms):
    """The request's laser part as (a, b, style, pick), a and b from the in point, or None."""
    las = req.get("laser")
    if not isinstance(las, dict) or not las.get("style"):
        return None
    style = las["style"]
    if style not in STYLES:
        raise ValueError("laser eyes are Flare or Beams")
    a = max(in_ms, int(las.get("a_ms", in_ms))) - in_ms
    b = min(out_ms, int(las.get("b_ms", out_ms))) - in_ms
    if b - a < MIN_PART_MS:
        raise ValueError("the laser part is too short")
    p = las.get("pick")
    pick = (min(1.0, max(0.0, float(p["x"]))), min(1.0, max(0.0, float(p["y"])))) if isinstance(p, dict) else None
    return a, b, style, pick


def to_output(src_w, src_h, opts, w, h, pick):
    """A point given as a fraction of the source picture, in pixels of the output media.geometry
    makes from it (crop, bars or a straight scale), or None."""
    if pick is None:
        return None
    x, y = pick[0] * src_w, pick[1] * src_h
    aspect, fit = opts.get("aspect", "original"), opts.get("fit", "crop")
    target = media.ASPECTS.get(aspect, src_w / src_h)
    if aspect == "original" or abs(target - src_w / src_h) < 0.01 and not media.moved_crop(opts.get("crop")):
        return [x * w / src_w, y * h / src_h]
    if fit == "pad":
        k = min(w / src_w, h / src_h)
        return [(w - src_w * k) / 2 + x * k, (h - src_h * k) / 2 + y * k]
    if media.moved_crop(opts.get("crop")):
        cw, ch, cx, cy = media.crop_box(src_w, src_h, target, opts.get("crop"))
    elif target < src_w / src_h:
        cw, ch = src_h * target, src_h
        cx, cy = (src_w - cw) / 2, 0
    else:
        cw, ch = src_w, src_w / target
        cx, cy = 0, (src_h - ch) / 2
    return [(x - cx) * w / cw, (y - cy) * h / ch]


def _run(spec, progress=None, timeout=900):
    """The worker, with progress(done, total) from its "frames" lines."""
    spec = dict(spec, model=config.laser_model())
    p = subprocess.Popen([*config.nice(), config.laser_python(), "-I", WORKER], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    p.stdin.write(json.dumps(spec))
    p.stdin.close()
    last = ""
    for line in p.stdout:
        last = line.strip()
        bits = last.split()
        if progress and len(bits) == 3 and bits[0] == "frames":
            progress(int(bits[1]), int(bits[2]))
    err = p.stderr.read()
    if p.wait(timeout=timeout) != 0:
        raise RuntimeError("laser eyes: " + (err.strip().splitlines() or ["the worker failed"])[-1][:300])
    return last


def master(src, in_ms, dur_ms, chain, size, track, part, cuts, tmp, progress=None):
    """The cut as tmp/laser.mkv, ready for media.render: the source from in_ms for dur_ms through the
    geometry chain, with lasers over part = (a, b, style, pick in output pixels), its times and
    the camera `cuts` from the cut's start. `track`: the source's audio track index, or None."""
    from . import slowmo
    out = os.path.join(tmp, "laser.mkv")
    a, b, style, pick = part
    _run(dict(src=src, ss=in_ms / 1000, t=dur_ms / 1000, vf=",".join(chain) if chain else "null",
              w=size[0], h=size[1], fps=slowmo.frame_rate(src), audio=track, out=out,
              a_ms=a, b_ms=b, cuts=list(cuts), style=style, pick=pick), progress)
    return out


def still(png, style, pick, out):
    """One picture with lasers, if it has a face: True when one was found."""
    last = _run(dict(image=png, out=out, style=style, pick=pick), timeout=120)
    try:
        return bool(json.loads(last).get("face"))
    except ValueError:
        return False
