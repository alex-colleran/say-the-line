"""Sound effects over a clip: a hit a moment after each line, after the last line, when the laser
eyes start, or at a mark.

The sounds are a few presets, made here by ffmpeg from formulas (so no recordings ship with Say
the Line), and the owner's own uploads, kept in data/sfx: trimmed of silence at both ends, peaks
brought to -1 dBFS, at most 8 s, as 48 kHz stereo WAV named by a hash of the upload.

A request's `sfx` is {"id": a preset name or an upload's id, "when": "lines" | "last" | "laser" |
"mark", "delay_ms", "volume": "low" | "medium" | "high", "times_ms": [media ms]}. The editor sends
the hit times it plays itself (= JS sfxTimes), so the render sounds like the preview; without
them they are worked out here from the clip's lines (lines and last), the laser part or `mark_ms`.
The hits are mixed in after the picture is made (and after Dialogue only), video copied."""
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time

from . import config

SFX = os.path.join(config.data_dir(), "sfx")
EXTS = ("mp3", "m4a", "aac", "wav", "flac", "ogg", "opus", "webm")
MAX_BYTES = 20 * 1024 * 1024
MAX_SECONDS = 8
MAX_HITS = 60
MIN_GAP_MS = 400                 # two lines ending this close together get one hit
WHEN = ("lines", "last", "laser", "mark")
VOLUMES = {"low": 0.35, "medium": 0.6, "high": 0.9}
_FMT = "aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=stereo"
# name, then the lavfi graph that makes it
PRESETS = {
    # an 808-style sub hit: the pitch falls from ~110 Hz to 38 Hz, driven into soft clipping
    "boom": ("Bass boom", "aevalsrc='0.95*tanh(2.6*sin(2*PI*(38*t+9*(1-exp(-8*t)))))*exp(-2.4*t)"
                          "+0.22*sin(2*PI*1500*t)*exp(-90*t)':s=48000:d=1.6,afade=t=out:st=1.25:d=0.35"),
    "whoosh": ("Whoosh", "anoisesrc=d=0.9:c=pink:r=48000:a=0.9,bandpass=f=1400:width_type=o:w=2,"
                         "afade=t=in:d=0.5:curve=exp,afade=t=out:st=0.5:d=0.4"),
    "ding": ("Ding", "aevalsrc='0.5*sin(2*PI*1760*t)*exp(-3*t)+0.25*sin(2*PI*3520*t)*exp(-6*t)"
                     "+0.12*sin(2*PI*5280*t)*exp(-9*t)':s=48000:d=1.5,afade=t=out:st=1.2:d=0.3"),
    "pop": ("Pop", "aevalsrc='0.9*sin(2*PI*(220*t+12.5*(1-exp(-40*t))))*exp(-25*t)':s=48000:d=0.3"),
}


def _upload_id(sid):
    return bool(re.fullmatch(r"[0-9a-f]{16}", sid or ""))


def path(sid):
    """The sound's WAV (a preset is made the first time it's asked for), or None."""
    if sid in PRESETS:
        p = os.path.join(SFX, "presets", f"{sid}.wav")
        if not os.path.isfile(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", PRESETS[sid][1] + "," + _FMT,
                                "-c:a", "pcm_s16le", p + ".tmp.wav"], capture_output=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError("could not make the sound: " + r.stderr.decode(errors="replace").strip()[-200:])
            os.replace(p + ".tmp.wav", p)
        return p
    p = os.path.join(SFX, f"{sid}.wav") if _upload_id(sid) else None
    return p if p and os.path.isfile(p) else None


def library():
    """The presets, then the uploads (newest first): [{id, name, duration?, preset}]."""
    out = [dict(id=k, name=v[0], preset=True) for k, v in PRESETS.items()]
    ups = []
    if os.path.isdir(SFX):
        for n in os.listdir(SFX):
            if n.endswith(".json") and _upload_id(n[:-5]):
                try:
                    with open(os.path.join(SFX, n)) as f:
                        ups.append(json.load(f))
                except (OSError, ValueError):
                    pass
    return out + sorted(ups, key=lambda u: -u.get("uploaded", 0))


def _peak_db(p):
    r = subprocess.run(["ffmpeg", "-v", "info", "-nostdin", "-i", p, "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    m = re.search(r"max_volume: (-?[\d.]+) dB", r.stderr)
    return float(m.group(1)) if m else None


def save(name, data):
    """Keep an uploaded sound. Returns its library entry. Raises ValueError for a file that isn't
    a usable sound."""
    ext = (name.rsplit(".", 1)[-1] if "." in name else "").lower()
    if ext not in EXTS:
        raise ValueError(f"that isn't a sound file Say the Line takes ({', '.join(EXTS)})")
    if len(data) > MAX_BYTES:
        raise ValueError(f"sounds can be up to {MAX_BYTES // 1024 // 1024} MB")
    sid = hashlib.sha1(data).hexdigest()[:16]
    meta = os.path.join(SFX, f"{sid}.json")
    if os.path.exists(meta) and path(sid):
        with open(meta) as f:
            return json.load(f)
    os.makedirs(SFX, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="saytheline-sfx-") as tmp:
        raw, trimmed = os.path.join(tmp, "in." + ext), os.path.join(tmp, "t.wav")
        with open(raw, "wb") as f:
            f.write(data)
        # silence off the start, then (reversed) off the end; at most MAX_SECONDS of what's left
        trim = "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.01"
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", raw, "-vn", "-sn", "-map", "0:a:0",
                            "-af", f"{trim},areverse,{trim},areverse,atrim=end={MAX_SECONDS},{_FMT}",
                            "-c:a", "pcm_s16le", trimmed], capture_output=True, timeout=120)
        peak = _peak_db(trimmed) if r.returncode == 0 and os.path.exists(trimmed) else None
        if peak is None or peak < -80:
            raise ValueError("no sound found in that file")
        out = os.path.join(SFX, f"{sid}.wav")
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", trimmed, "-af", f"volume={-1 - peak:.2f}dB",
                            "-c:a", "pcm_s16le", out + ".tmp.wav"], capture_output=True, timeout=60)
        if r.returncode != 0:
            raise ValueError("could not read that sound: " + r.stderr.decode(errors="replace").strip()[-200:])
        os.replace(out + ".tmp.wav", out)
    dur = _duration(out)
    stem = os.path.basename(name).rsplit(".", 1)[0]
    entry = dict(id=sid, name=stem[:60] or "Sound", duration=round(dur, 2), uploaded=int(time.time()), preset=False)
    with open(meta, "w") as f:
        json.dump(entry, f)
    return entry


def delete(sid):
    if not _upload_id(sid):
        return False
    gone = False
    for ext in ("wav", "json"):
        p = os.path.join(SFX, f"{sid}.{ext}")
        if os.path.exists(p):
            os.remove(p)
            gone = True
    return gone


def _duration(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def spec_of(req):
    """The request's sound effect: (id, times or None, when, delay_ms, gain, mark_ms), or None.
    Raises ValueError for one that can't be made."""
    s = req.get("sfx")
    if not isinstance(s, dict) or not s.get("id"):
        return None
    if not path(s["id"]):
        raise ValueError("that sound effect isn't on the server any more: pick another")
    when = s.get("when", "lines")
    if when not in WHEN:
        raise ValueError("a sound effect plays after lines, after the last line, with the lasers or at a mark")
    delay = min(1000, max(0, int(s.get("delay_ms") or 0)))
    gain = VOLUMES.get(s.get("volume"), VOLUMES["medium"])
    times = s.get("times_ms")
    times = [int(t) for t in times[:MAX_HITS]] if isinstance(times, list) else None
    return s["id"], times, when, delay, gain, s.get("mark_ms")


def line_end(c):
    """When a line is over: its last word's end when it has word times, else the subtitle's."""
    return c["words"][-1]["e"] if c.get("words") else c["end_ms"]


def times(spec, cues, in_ms, out_ms, laser_a=None):
    """Hit times on the media timeline, inside the cut (= JS sfxTimes): a line counts once it ends
    inside the cut, and lines ending within MIN_GAP_MS of the last hit share it."""
    _, given, when, delay, _, mark = spec
    if given is not None:
        ts = given
    elif when in ("lines", "last"):
        ends = sorted(line_end(c) for c in cues if in_ms < line_end(c) <= out_ms)
        ts = [e + delay for e in (ends[-1:] if when == "last" else ends)]
    elif when == "laser":
        ts = [] if laser_a is None else [laser_a + delay]
    else:
        ts = [] if mark is None else [int(mark)]
    out = []
    for t in sorted(t for t in ts if in_ms <= t < out_ms):
        if not out or t - out[-1] >= MIN_GAP_MS:
            out.append(t)
    return out[:MAX_HITS]


def mix(clip, fmt, sound, hits_ms, gain, has_audio, dur_s):
    """Lay the sound over the clip at each hit (ms from its start), in place: video copied, the
    audio re-encoded through a limiter so a hit over loud dialogue doesn't clip."""
    if not hits_ms:
        return
    n = len(hits_ms)
    base = "[0:a]" if has_audio else "[2:a]"
    graph = [f"[1:a]{_FMT},volume={gain:.2f},asplit={n}" + "".join(f"[s{i}]" for i in range(n)) if n > 1
             else f"[1:a]{_FMT},volume={gain:.2f}[s0]"]
    graph += [f"[s{i}]adelay=delays={max(0, int(t))}:all=1[d{i}]" for i, t in enumerate(hits_ms)]
    graph.append(f"{base}{_FMT}" + "[b];[b]" + "".join(f"[d{i}]" for i in range(n))
                 + f"amix=inputs={n + 1}:duration=first:normalize=0,alimiter=limit=0.95:level=false[a]")
    tmp = clip + ".sfx." + fmt
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", clip, "-i", sound]
    if not has_audio:
        cmd += ["-f", "lavfi", "-t", f"{dur_s:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-filter_complex", ";".join(graph), "-map", "0:v:0", "-map", "[a]", "-c:v", "copy"]
    cmd += ["-c:a", "libopus", "-b:a", "128k"] if fmt == "webm" else ["-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]
    r = subprocess.run([*config.nice(), *cmd, tmp], capture_output=True, timeout=300)
    if r.returncode != 0:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise RuntimeError("could not add the sound effect: " + r.stderr.decode(errors="replace").strip()[-300:])
    os.replace(tmp, clip)
