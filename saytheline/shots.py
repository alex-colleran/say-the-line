"""Camera cuts (shot changes) near a moment, so a clip can start on the line's own shot and end
before the next one, instead of opening on half a second of whatever came before.

How a cut is found, from frames shrunk to 32x18 grey (decoded on the GPU when VAAPI is there, so
a 20 s scan costs about a second and little CPU):
- each frame is normalised to its own brightness and contrast, so a cut between two dark shots
  counts as much as one between bright ones (ffmpeg's scene score misses those);
- the change is the MEDIAN pixel difference, so a mouth or a hand moving in a still frame (anime
  above all) doesn't count: most pixels have to change;
- it must stand out from the frames either side of it (fast motion changes every frame);
- and it must survive the best small shift of one frame against the other, which rules out a
  camera shake or a whip pan.
These were tuned by eye on 141 frame pairs from 18 titles (sitcoms, dramas lit very dark, anime,
cartoons, action films): every labelled cut found; the only false ones were dust whiteouts.

A cut is reported as the boundary half a frame before the new shot's first frame. A render that
starts there (input-side -ss, re-encoded) begins exactly on the new shot, and one that ends there
ends on the outgoing shot's last frame, however ffmpeg rounds an exact frame time. Files whose
frames carry no timestamps of their own (old XviD AVIs with packed B-frames) get their frame times
guessed from the decode order, which shifts by a frame depending on where decoding starts: there a
cut is used one frame inward (margin_ms), giving up one frame of the shot to never show another.

Fades and dissolves have no single changed frame and are not reported: a clip then keeps its
ordinary padding."""
import json
import os
import re
import statistics
import subprocess
import threading

from . import config

W, H = 32, 18
CHANGE_MIN = 0.28        # median normalised pixel change of a cut, after the best small shift
STANDOUT = 6             # ...and how many times the calmer side's typical change it must be
SHIFT = 2                # px (of 32) tried each way: camera shake, slow pans
FLASH_FRAMES = 3         # two "cuts" this close are a flash, a strobe or a glitch: neither counts
SPEECH_TOL_MS = 120      # a cut this close inside the heard words still counts as where the line starts
                         # or ends: word times are that loose, and editors cut on the first syllable
LEAD_MS, TAIL_MS = 400, 200   # scanned around a range, so its edge frames have neighbours
MAX_SCAN_MS = 180_000
SCAN_SLOT = threading.Semaphore(1)
_locks, _locks_guard = {}, threading.Lock()
_SHOWINFO = re.compile(rb"\] n: *\d+ pts: *-?\d+ pts_time:(-?[\d.]+)")


def _decode(src, a_ms, b_ms, gpu):
    """[(ms, 32x18 grey bytes)] for every frame in [a_ms, b_ms) on the media timeline. The frame
    times come from showinfo in the same pass, and -fps_mode passthrough stops ffmpeg inserting
    duplicate frames (it does on files with uneven timestamps), which would shift the pairing."""
    cmd = [*config.nice(), "ffmpeg", "-hide_banner", "-nostats", "-v", "info", "-nostdin"]
    if gpu:
        cmd += ["-hwaccel", "vaapi", "-hwaccel_device", gpu, "-hwaccel_output_format", "vaapi"]
        vf = f"scale_vaapi=w=320:h=180:format=nv12,hwdownload,format=nv12,scale={W}:{H},format=gray,showinfo"
    else:
        cmd += ["-threads", "2"]            # a short burst on two cores rather than all of them
        vf = f"scale={W}:{H},format=gray,showinfo"
    cmd += ["-ss", f"{a_ms / 1000:.3f}", "-t", f"{(b_ms - a_ms) / 1000:.3f}", "-i", src,
            "-map", "0:v:0", "-an", "-sn", "-dn", "-fps_mode", "passthrough", "-vf", vf, "-f", "rawvideo", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()[-300:] or "ffmpeg failed")
    times = [a_ms + float(t) * 1000 for t in _SHOWINFO.findall(r.stderr)]
    n = W * H
    frames = [r.stdout[i * n:(i + 1) * n] for i in range(len(r.stdout) // n)]
    if len(times) != len(frames):
        raise RuntimeError(f"frame timing mismatch ({len(times)} times, {len(frames)} frames)")
    return list(zip(times, frames))


def decode(src, a_ms, b_ms):
    """_decode on the GPU first (when configured), then the CPU."""
    gpu = config.vaapi()
    for dev in ((gpu, None) if gpu else (None,)):
        try:
            frames = _decode(src, a_ms, b_ms, dev)
            if frames:
                return frames
        except (RuntimeError, subprocess.TimeoutExpired):
            if dev is None:
                raise
    return []


def _norm(f):
    n = len(f)
    m = sum(f) / n
    s = (sum((p - m) ** 2 for p in f) / n) ** 0.5
    return [(p - m) / (s + 4) for p in f]


def _change(a, b):
    return statistics.median(abs(p - q) for p, q in zip(a, b))


def _moved(a, b):
    """The change left after the best shift of up to SHIFT px each way (overlap only)."""
    best = None
    for dy in range(-SHIFT, SHIFT + 1):
        for dx in range(-SHIFT, SHIFT + 1):
            d = [abs(a[y * W + x] - b[(y - dy) * W + x - dx])
                 for y in range(max(0, dy), min(H, H + dy)) for x in range(max(0, dx), min(W, W + dx))]
            m = statistics.median(d)
            best = m if best is None or m < best else best
    return best


def find_cuts(frames):
    """(frame_ms, anchor_ms, [(boundary_ms, score)]) from [(ms, grey bytes)]. Frame times are
    printed to the ms, so the frame length is the average over the scan (41.708 at 23.976 fps,
    where single steps read 41 or 42); anchor_ms is one frame's start, which fixes the grid."""
    if len(frames) < 2:
        return None, None, []
    frame_ms = (frames[-1][0] - frames[0][0]) / (len(frames) - 1)
    z = [_norm(f) for _, f in frames]
    ch = [0.0] + [_change(z[i - 1], z[i]) for i in range(1, len(z))]
    hits = []
    for i in range(1, len(z)):
        if ch[i] < CHANGE_MIN:
            continue
        before, after = ch[max(1, i - 3):i], ch[i + 1:i + 4]
        calm = min(statistics.median(s) for s in (before, after) if s) if (before or after) else 0
        if ch[i] < STANDOUT * max(calm, 0.05):
            continue
        moved = _moved(z[i - 1], z[i])
        if moved >= CHANGE_MIN:
            hits.append((i, moved))
    idx = [i for i, _ in hits]
    flash = {i for i in idx for j in idx if i != j and abs(i - j) <= FLASH_FRAMES}
    cuts = [(round(frames[i][0] - frame_ms / 2), round(sc, 3)) for i, sc in hits if i not in flash]
    return frame_ms, frames[0][0], cuts


def _merge(spans):
    out = []
    for a, b, *_ in sorted(spans):
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _gaps(spans, a, b):
    gaps, at = [], a
    for s0, s1 in _merge(spans):
        if s1 <= at or s0 >= b:
            continue
        if s0 > at:
            gaps.append((at, s0))
        at = max(at, s1)
    if at < b:
        gaps.append((at, b))
    return gaps


def _dedupe(cuts, frame_ms):
    """One cut per camera change. Scans from different start points only disagree on files with
    guessed frame times (by a frame); the earlier estimate is kept, and margin_ms covers the rest."""
    out = {}
    for ms in sorted(cuts, key=int):
        prev = max(out, key=int, default=None)
        if prev is not None and int(ms) - int(prev) <= 1.5 * frame_ms:
            out[prev] = max(out[prev], cuts[ms])
        else:
            out[ms] = cuts[ms]
    return out


def loose_timing(src):
    """True when the video's packets have no presentation times (see the module docstring)."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#60",
                            "-show_entries", "packet=pts", "-of", "csv=p=0", src], capture_output=True, text=True, timeout=30)
        return "N/A" in r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


class Cache:
    """Cuts per item in <dir>/<item id>.json, with the spans already scanned, so the editor, the
    supercut and agents scan each stretch of an episode once. Dropped when the file changes.
    Each span keeps its own frame grid ([from, to, anchor_ms, frame_ms]): a grid measured
    minutes away would drift by more than a frame."""

    def __init__(self, folder):
        self.folder = folder

    def _path(self, item_id):
        return os.path.join(self.folder, f"{int(item_id)}.json")

    def _load(self, item_id, stamp):
        try:
            with open(self._path(item_id)) as f:
                d = json.load(f)
            if d.get("stamp") == stamp:
                return d
        except (OSError, ValueError):
            pass
        return {"stamp": stamp, "spans": [], "cuts": {}, "loose": None}

    def _save(self, item_id, d):
        os.makedirs(self.folder, exist_ok=True)
        tmp = self._path(item_id) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, self._path(item_id))

    def cuts(self, item_id, src, a_ms, b_ms):
        """{"frame_ms", "anchor_ms", "cuts": [{"ms", "score"}], "from_ms", "to_ms"} for [a_ms, b_ms],
        scanning only the parts not seen before."""
        a_ms, b_ms = max(0, int(a_ms)), int(b_ms)
        if b_ms <= a_ms:
            raise ValueError("empty range")
        if b_ms - a_ms > MAX_SCAN_MS:
            raise ValueError(f"scan at most {MAX_SCAN_MS // 1000} s at a time")
        st = os.stat(src)
        stamp = [int(st.st_mtime), st.st_size]
        with _locks_guard:
            lock = _locks.setdefault(int(item_id), threading.Lock())
        with lock:
            d = self._load(item_id, stamp)
            if d.get("loose") is None:
                d["loose"] = loose_timing(src)
            gaps = _gaps(d["spans"], a_ms, b_ms)
            if gaps:
                g0, g1 = gaps[0][0], gaps[-1][1]             # one ffmpeg run over all of them
                with SCAN_SLOT:
                    frames = decode(src, max(0, g0 - LEAD_MS), g1 + TAIL_MS)
                frame_ms, anchor, found = find_cuts(frames)
                if len(frames) >= 2:
                    for ms, sc in found:                     # the lead-in only gives frame 1 a
                        if g0 <= ms < g1:                    # neighbour: trust cuts from g0 on
                            d["cuts"][str(ms)] = sc
                    d["spans"].append([g0, g1, round(anchor, 3), round(frame_ms, 4)])
                    d["cuts"] = _dedupe(d["cuts"], frame_ms)
                    self._save(item_id, d)
        cuts = sorted((int(k), v) for k, v in d["cuts"].items() if a_ms <= int(k) < b_ms)
        # the frame grid of the scan overlapping this range most
        grid = max(d["spans"], key=lambda sp: min(sp[1], b_ms) - max(sp[0], a_ms), default=None)
        frame_ms = grid[3] if grid else None
        return dict(frame_ms=frame_ms, anchor_ms=grid[2] if grid else None,
                    margin_ms=round(frame_ms or 1000 / 24) if d.get("loose") else 0,
                    from_ms=a_ms, to_ms=b_ms, cuts=[dict(ms=k, score=v) for k, v in cuts])


def fit(in_ms, out_ms, keep_a, keep_b, cuts, margin=0):
    """Trim a cut's padding to the camera cuts in it (= JS fitShots): start at the last cut
    before the line (or heard words) [keep_a, keep_b] begins, end at the first cut after it ends.
    Only ever trims, and not into [keep_a, keep_b] beyond SPEECH_TOL_MS. `margin`: see margin_ms."""
    pre = [c + margin for c in cuts if in_ms < c + margin <= keep_a + SPEECH_TOL_MS]
    post = [c - margin for c in cuts if keep_b - SPEECH_TOL_MS <= c - margin < out_ms]
    a, b = (max(pre) if pre else in_ms), (min(post) if post else out_ms)
    return (a, b) if b - a >= 300 else (in_ms, out_ms)


def whole_shot(keep_a, keep_b, cuts, margin=0):
    """The shot(s) holding [keep_a, keep_b] (= JS shotTargets): from the last cut at or before its
    start to the first at or after its end (None for a side with no cut found)."""
    pre = [c + margin for c in cuts if c + margin <= keep_a + SPEECH_TOL_MS]
    post = [c - margin for c in cuts if c - margin >= keep_b - SPEECH_TOL_MS]
    a, b = (max(pre) if pre else None), (min(post) if post else None)
    return (a, b) if a is None or b is None or b - a >= 300 else (None, None)
