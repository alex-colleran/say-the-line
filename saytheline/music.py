"""A song under a Studio cut: uploads, their beats, joins snapped to the beat, and the final mix.

Uploads live in data/uploads (never next to the media), named by a hash of their bytes so the
same song uploaded twice is kept once. Each gets <id>.json beside it: its name, length and beats
(beat_worker.py, run in the speech Python because it needs numpy).

The mix ducks the clips' music under speech the usual way: the song goes through a sidechain
compressor keyed on the clips' own audio, so it dips while someone talks and comes back between
lines. Levels are set by loudness (one-pass loudnorm on the song) rather than raw gain, so a quiet
recording and a loud master sit the same."""
import bisect
import hashlib
import json
import os
import re
import subprocess
import tempfile

from . import config

UPLOADS = os.path.join(config.data_dir(), "uploads")
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beat_worker.py")
EXTS = ("mp3", "m4a", "aac", "wav", "flac", "ogg", "opus", "webm")
MAX_BYTES = 80 * 1024 * 1024
MAX_SECONDS = 20 * 60
ANALYSE_SECONDS = 12 * 60                  # beats of the first 12 min are plenty for a Studio cut
# song loudness (LUFS) under the clips (which the supercut renderer evens out to -16)
LEVELS = {"low": -27, "medium": -22, "high": -17}
ALONE_LUFS = -14                           # the song when the clips are muted
CLIP_SOUND = ("duck", "mute", "keep")
FADE_OUT_S = 1.5


def _path(sid):
    if not re.fullmatch(r"[0-9a-f]{16}", sid or ""):
        return None
    for ext in EXTS:
        p = os.path.join(UPLOADS, f"{sid}.{ext}")
        if os.path.isfile(p):
            return p
    return None


def path(sid):
    return _path(sid)


def _duration(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type:format=duration",
                        "-of", "json", p], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    d = json.loads(r.stdout or "{}")
    if not d.get("streams"):
        return None
    return float((d.get("format") or {}).get("duration") or 0)


def save(name, data):
    """Keep an uploaded song and find its beats. Returns its info (see info()). Raises ValueError for
    something that isn't a usable audio file."""
    ext = (name.rsplit(".", 1)[-1] if "." in name else "").lower()
    if ext not in EXTS:
        raise ValueError(f"that isn't a song file Say the Line takes ({', '.join(EXTS)})")
    if len(data) > MAX_BYTES:
        raise ValueError(f"songs can be up to {MAX_BYTES // 1024 // 1024} MB")
    sid = hashlib.sha1(data).hexdigest()[:16]
    os.makedirs(UPLOADS, exist_ok=True)
    p = os.path.join(UPLOADS, f"{sid}.{ext}")
    if not os.path.exists(p):
        with open(p + ".tmp", "wb") as f:
            f.write(data)
        os.replace(p + ".tmp", p)
    dur = _duration(p)
    if not dur:
        os.remove(p)
        raise ValueError("no sound found in that file")
    if dur > MAX_SECONDS:
        os.remove(p)
        raise ValueError(f"songs can be up to {MAX_SECONDS // 60} minutes")
    meta = os.path.join(UPLOADS, f"{sid}.json")
    if os.path.exists(meta):
        with open(meta) as f:
            return json.load(f)
    beats = analyse(p)
    info = dict(id=sid, name=os.path.basename(name)[:120], ext=ext, duration=round(dur, 2), **beats)
    with open(meta, "w") as f:
        json.dump(info, f)
    return info


def info(sid):
    p = os.path.join(UPLOADS, f"{sid}.json") if re.fullmatch(r"[0-9a-f]{16}", sid or "") else None
    if not p or not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def analyse(p):
    with tempfile.TemporaryDirectory(prefix="saytheline-beats-") as tmp:
        wav = os.path.join(tmp, "a.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-t", str(ANALYSE_SECONDS), "-i", p,
                        "-ac", "1", "-ar", "22050", "-c:a", "pcm_s16le", wav], check=True, capture_output=True, timeout=120)
        r = subprocess.run([*config.nice(), config.stt_python(), "-I", WORKER, wav], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        if r.returncode != 0:
            raise RuntimeError("couldn't find the beat: " + r.stderr.strip()[-200:])
        d = json.loads(r.stdout)
    return dict(bpm=d["bpm"], beats=d["beats"], downbeats=d["downbeats"])


def grid(song, bpm=None, bars=False):
    """Beat times (s, on the song's own timeline): the detected beats, or for a BPM set by hand an
    even grid at that tempo through the detected first downbeat. bars: every 4th (bar starts)."""
    if bpm and abs(bpm - song["bpm"]) > 0.05:
        anchor = (song.get("downbeats") or song["beats"] or [0])[0]
        step = 60 / bpm
        first = anchor - step * int(anchor / step)
        n = int((song["duration"] - first) / step) + 1
        beats = [round(first + k * step, 3) for k in range(n)]
        return beats[(round((anchor - first) / step)) % 4::4] if bars else beats
    return list(song["downbeats"] if bars else song["beats"])


def snap_joins(lengths, beats, offset_s, max_shift_s):
    """Move each join between clips (and the end) onto the nearest beat, by at most max_shift_s:
    returns how much to change each clip's length (s, + = longer). lengths: each clip's length on
    screen (s); beats: on the song's timeline; offset_s: where in the song the cut starts. A clip
    is never made shorter than half a second."""
    on_video = [b - offset_s for b in beats if b >= offset_s]
    deltas, t = [], 0.0
    for L in lengths:
        end = t + L
        i = bisect.bisect_left(on_video, end)
        near = [b for b in on_video[max(0, i - 1):i + 1] if b > t + 0.5]
        d = 0.0
        if near:
            b = min(near, key=lambda x: abs(x - end))
            if abs(b - end) <= max_shift_s:
                d = b - end
        deltas.append(round(d, 3))
        t = end + d
    return deltas


def mix(video, song_path, opts, out):
    """The cut's audio with the song under it, as `out` (video copied as is). opts: offset_s (where in
    the song to start), level (low/medium/high), clips (duck/mute/keep)."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    dur = float(r.stdout.strip() or 0)
    if dur <= 0:
        raise RuntimeError("the cut has no length")
    clips = opts.get("clips") if opts.get("clips") in CLIP_SOUND else "duck"
    lufs = ALONE_LUFS if clips == "mute" else LEVELS.get(opts.get("level"), LEVELS["medium"])
    fade = min(FADE_OUT_S, dur / 3)
    song = (f"[1:a]atrim=0:{dur:.3f},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
            f"loudnorm=I={lufs}:TP=-2:LRA=11,aresample=48000,afade=t=in:d=0.05,"
            f"afade=t=out:st={max(0, dur - fade):.3f}:d={fade:.3f},apad=whole_dur={dur:.3f}")
    if clips == "mute":
        graph = song + ",alimiter=limit=0.95[out]"
    elif clips == "keep":
        graph = song + "[m];[0:a][m]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[out]"
    else:   # duck: the song dips while the clips' own sound is loud
        graph = (song + "[m];[0:a]asplit=2[a][key];"
                 "[m][key]sidechaincompress=threshold=0.015:ratio=9:attack=20:release=450:makeup=1[md];"
                 "[a][md]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[out]")
    r = subprocess.run([*config.nice(), "ffmpeg", "-v", "error", "-nostdin", "-y", "-i", video,
                        "-ss", f"{max(0, float(opts.get('offset_s') or 0)):.3f}", "-i", song_path,
                        "-filter_complex", graph, "-map", "0:v:0", "-map", "[out]", "-c:v", "copy",
                        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", out],
                       capture_output=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()[-400:])
