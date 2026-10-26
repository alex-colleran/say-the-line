"""Meme laser eyes: a red-hot glow and a long lens-flare streak on someone's eyes, following them.

Runs in the Python that has MediaPipe (tools.laser_python), as a child process, with the spec as
JSON on stdin:
  python -I laser_worker.py < spec.json
  {"model": face_landmarker.task, "style": "flare" | "beams", "pick": [x, y] px or null,
   "src", "ss", "t", "vf": ffmpeg filters to the output size, "w", "h", "fps", "audio": track or null,
   "out": the cut with lasers (.mkv), "a_ms", "b_ms": the part with lasers (from the cut's start),
   "cuts": [camera cuts, ms from the cut's start]}
or, for one picture, {"model", "style", "pick", "image": in.png, "out": out.png}.
Prints "frames done total" lines while it works.

Faces come from MediaPipe's Face Landmarker: the iris centres place the glow, the eye corners size
it, and the head's pose points the beams. `pick` chooses whose eyes (the face nearest it, tried
again zoomed in when that face is small); without one it is the largest face. Each camera shot
picks again, so the lasers never jump onto someone else mid-shot.
"""
import json
import subprocess
import sys

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import BaseOptions, vision

R_IRIS, L_IRIS = 468, 473                  # iris centres
R_EYE, L_EYE = (33, 133), (362, 263)       # eye corners: the eye's width sets the glow's size
TURNED = 0.4                               # head turned this far: beams go where it looks
FADE_S = 0.35                              # in and out at the ends of the part
LOST = 4                                   # frames without the face before the glow is gone
REPICK = 12                                # frames without it before another face may take over
ZOOM = 0.45                                # a small face is looked for again in a square this tall


def landmarker(model):
    return vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=model), num_faces=4, output_facial_transformation_matrixes=True,
        min_face_detection_confidence=0.4, min_face_presence_confidence=0.4))


def detect(lm, rgb, box=None):
    """Faces in the picture (or in box (x, y, side) of it, zoomed in), in picture pixels."""
    x0, y0 = 0, 0
    if box:
        x0, y0, side = box
        rgb = rgb[y0:y0 + side, x0:x0 + side]
    h, w = rgb.shape[:2]
    res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb)))
    out = []
    for k, pts in enumerate(res.face_landmarks):
        def P(i):
            return np.array([x0 + pts[i].x * w, y0 + pts[i].y * h])
        eye_w = (np.linalg.norm(P(R_EYE[0]) - P(R_EYE[1])) + np.linalg.norm(P(L_EYE[0]) - P(L_EYE[1]))) / 2
        m = np.array(res.facial_transformation_matrixes[k]) if res.facial_transformation_matrixes else np.eye(4)
        fwd = m[:3, :3] @ np.array([0, 0, 1.0])             # out of the face, camera space (y up)
        eyes = [P(R_IRIS), P(L_IRIS)]
        out.append(dict(eyes=eyes, mid=(eyes[0] + eyes[1]) / 2, eye_w=max(2.0, eye_w), dir=np.array([fwd[0], -fwd[1]])))
    return out


def zoom_box(at, w, h):
    side = int(min(w, h, max(64, h * ZOOM)))
    x = int(min(w - side, max(0, at[0] - side / 2)))
    y = int(min(h - side, max(0, at[1] - side / 2)))
    return x, y, side


def find(lm, rgb, ref, near):
    """The face nearest `ref` within `near` px (looked for zoomed in if the whole picture misses
    it), or None. Without a ref: the largest face."""
    fs = detect(lm, rgb)
    if ref is None:
        return max(fs, key=lambda f: f["eye_w"], default=None)

    def best(fs):
        f = min(fs, key=lambda f: np.linalg.norm(f["mid"] - ref), default=None)
        return f if f is not None and np.linalg.norm(f["mid"] - ref) <= near else None
    h, w = rgb.shape[:2]
    return best(fs) or best(detect(lm, rgb, zoom_box(ref, w, h)))


def light(h, w, face, style):
    """The glow to add over the picture: (y0, y1, x0, x1) and that box's float32 light, HxWx3.
    Drawn at a smaller scale inside a box around the eyes (the whole picture for beams), blurred,
    then scaled up: it is all glow, so nothing sharp is lost, and it is many times quicker."""
    s = face["eye_w"]
    d = face["dir"]
    n = np.linalg.norm(d)
    beams = style == "beams" and n > TURNED
    streak = s * (5 if beams else 9)
    if beams:
        y0, y1, x0, x1 = 0, h, 0, w
    else:
        xs, ys = [e[0] for e in face["eyes"]], [e[1] for e in face["eyes"]]
        mx, my = streak + s * 2, s * 2.4                   # the streak, the halo's blur
        x0, x1 = int(max(0, min(xs) - mx)), int(min(w, max(xs) + mx))
        y0, y1 = int(max(0, min(ys) - my)), int(min(h, max(ys) + my))
        if x1 - x0 < 2 or y1 - y0 < 2:
            return None
    q = min(1.0, 24 / s)                                   # ~24 px across an eye is plenty for a blur
    bw, bh = max(2, int((x1 - x0) * q)), max(2, int((y1 - y0) * q))
    core, mid, wide = (np.zeros((bh, bw), np.float32) for _ in range(3))
    sh = 16                                                # cv2's fixed-point: 4 bits of sub-pixel

    def pt(p):
        return (int(round((p[0] - x0) * q * sh)), int(round((p[1] - y0) * q * sh)))

    def poly(img, ps, v):
        cv2.fillPoly(img, [np.array([pt(p) for p in ps], np.int32)], v, cv2.LINE_AA, shift=4)

    def disc(img, c, r, v):
        cv2.circle(img, pt(c), max(1, int(round(r * q * sh))), v, -1, cv2.LINE_AA, shift=4)
    r = s * 0.22
    for e in face["eyes"]:
        x, y = e
        disc(core, e, r, 1.0)
        disc(mid, e, 2 * r, 0.78)
        # the anamorphic streak, long and thin: the meme's flare
        poly(mid, [(x - streak, y), (x, y - r * 0.5), (x + streak, y), (x, y + r * 0.5)], 0.9)
        poly(core, [(x - streak * 0.5, y), (x, y - r * 0.18), (x + streak * 0.5, y), (x, y + r * 0.18)], 1.0)
        for a in (np.pi / 4, -np.pi / 4):                  # star spikes
            v = np.array([np.cos(a), np.sin(a)]) * s * 1.6
            cv2.line(mid, pt(e - v), pt(e + v), 0.59, max(1, int(r * 0.25 * q)), cv2.LINE_AA, shift=4)
        if beams:                                          # out where the face points, off the picture
            u = d / n
            far = e + u * (w + h) * 2
            perp = np.array([-u[1], u[0]])
            for img, wid in ((wide, r * 1.6), (mid, r * 0.9), (core, r * 0.35)):
                poly(img, [e + perp * wid * 0.6, far + perp * wid * 2.5, far - perp * wid * 2.5, e - perp * wid * 0.6], 1.0)

    def blur(img, k):
        return cv2.GaussianBlur(img, (0, 0), max(0.6, s * k * q))
    c, m, wd, halo = blur(core, 0.04), blur(mid, 0.12), blur(wide, 0.35), blur(mid, 0.6)
    lt = (c[..., None] * np.array((1.6, 1.52, 1.44), np.float32) + m[..., None] * np.array((1.5, 0.27, 0.075), np.float32)
          + wd[..., None] * np.array((0.9, 0.09, 0.045), np.float32) + halo[..., None] * np.array((1.2, 0.12, 0), np.float32))
    if q < 1:
        lt = cv2.resize(lt, (x1 - x0, y1 - y0), interpolation=cv2.INTER_LINEAR)
    return (y0, y1, x0, x1), lt


def glow(rgb, face, style, strength=1.0):
    """The picture with the face's lasers added: a screen-like add that never clips harshly."""
    h, w = rgb.shape[:2]
    got = light(h, w, face, style)
    if not got or strength <= 0.01:
        return rgb
    (y0, y1, x0, x1), lt = got
    out = rgb.copy()
    a = rgb[y0:y1, x0:x1].astype(np.float32) / 255
    out[y0:y1, x0:x1] = (np.clip(1 - (1 - a) * np.exp(-lt * strength), 0, 1) * 255).astype(np.uint8)
    return out


class Follow:
    """Whose eyes, frame to frame: the pick (or the largest face) at the start of each shot, then
    the same face, smoothed so the glow doesn't shiver with the landmarks."""
    def __init__(self, lm, pick, w, h):
        self.lm, self.pick, self.diag = lm, None if pick is None else np.array(pick, float), np.hypot(w, h)
        self.face, self.miss = None, 99

    def cut(self):
        self.face, self.miss = None, 99

    def step(self, rgb):
        if self.face is not None and self.miss < REPICK:
            f = find(self.lm, rgb, self.face["mid"], self.face["eye_w"] * 4)
        else:
            f = find(self.lm, rgb, self.pick, self.diag * 0.18)
        if f is None:
            self.miss += 1
            return self.face if self.miss < LOST else None
        if self.face is None or self.miss >= LOST:
            self.face = f
        else:
            al = 0.55
            g = self.face
            self.face = dict(eyes=[g["eyes"][i] * (1 - al) + f["eyes"][i] * al for i in range(2)],
                             eye_w=g["eye_w"] * (1 - al) + f["eye_w"] * al, dir=g["dir"] * 0.8 + f["dir"] * 0.2)
            self.face["mid"] = (self.face["eyes"][0] + self.face["eyes"][1]) / 2
        self.miss = 0
        return self.face


def image(spec):
    bgr = cv2.imread(spec["image"])
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    lm = landmarker(spec["model"])
    h, w = rgb.shape[:2]
    f = find(lm, rgb, None if spec.get("pick") is None else np.array(spec["pick"], float), np.hypot(w, h) * 0.18)
    if f is not None:
        rgb = glow(rgb, f, spec.get("style", "flare"))
    cv2.imwrite(spec["out"], cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    print(json.dumps({"face": f is not None}), flush=True)


def video(spec):
    w, h, fps = int(spec["w"]), int(spec["h"]), float(spec["fps"])
    src_in = ["-ss", f"{spec['ss']:.3f}", "-t", f"{spec['t']:.3f}", "-i", spec["src"]]
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-nostdin", *src_in, "-map", "0:v:0", "-vf", spec["vf"] + ",format=rgb24",
                            "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           stdout=subprocess.PIPE)
    audio = ["-map", f"1:a:{spec['audio']}", "-c:a", "flac"] if spec.get("audio") is not None else []
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
                            "-r", f"{fps:.5f}", "-i", "-", *src_in, "-map", "0:v", *audio,
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p", "-shortest", spec["out"]],
                           stdin=subprocess.PIPE)
    lm = landmarker(spec["model"])
    follow = Follow(lm, spec.get("pick"), w, h)
    a, b = spec["a_ms"], spec["b_ms"]
    cuts = sorted(spec.get("cuts") or [])
    total = max(1, int(round(spec["t"] * fps)))
    fade = max(1e-3, min(FADE_S * 1000, (b - a) / 4))
    k, shown, size = 0, 0.0, w * h * 3
    try:
        while True:
            buf = dec.stdout.read(size)
            if len(buf) < size:
                break
            rgb = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            t = k * 1000 / fps
            if a <= t < b:
                while cuts and cuts[0] <= t:               # a new shot: pick again
                    cuts.pop(0)
                    follow.cut()
                face = follow.step(rgb)
                shown = min(1.0, shown + 0.25) if face is not None else max(0.0, shown - 0.25)
                env = shown * min(1.0, (t - a) / fade, (b - t) / fade)
                env *= 1 + 0.08 * np.sin(k * 1.7) * np.sin(k * 0.6)      # a little flicker
                if face is not None and env > 0.01:
                    rgb = glow(rgb, face, spec.get("style", "flare"), env)
            enc.stdin.write(np.ascontiguousarray(rgb).tobytes())
            k += 1
            if k % 12 == 0:
                print(f"frames {k} {total}", flush=True)
    finally:
        enc.stdin.close()
        enc.wait()
        dec.wait()
    if enc.returncode or dec.returncode or not k:
        sys.exit("could not add the lasers: ffmpeg failed")
    print(f"frames {k} {k}", flush=True)


def main():
    spec = json.load(sys.stdin)
    (image if spec.get("image") else video)(spec)


if __name__ == "__main__":
    main()
