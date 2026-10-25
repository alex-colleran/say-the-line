"""Say the Line web UI: search, result cards and thumbnails (step 1); a preview window with in/out
handles (step 2); render and download (step 3).

Standard library only (ThreadingHTTPServer), like the rest of Say the Line. There is no login: keep
it on 127.0.0.1 (the default) or a private network such as a tailnet, never the open internet.

  python3 -m saytheline.web            # http://$SAYTHELINE_HOST:$SAYTHELINE_PORT/
"""
import contextlib
import functools
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import config
from . import jellyfin
from . import library
from . import media
from . import sayany
from . import search as srch
from . import shots as shotcuts
from . import snap as snapper

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.environ.get("SAYTHELINE_INDEX", os.path.join(config.data_dir(), "cues.db"))
THUMBS = os.environ.get("SAYTHELINE_THUMBS", os.path.join(os.path.dirname(INDEX), "thumbs"))
HOST = os.environ.get("SAYTHELINE_HOST", "127.0.0.1")
PORT = int(os.environ.get("SAYTHELINE_PORT", "8765"))
COVERS = os.path.join(os.path.dirname(INDEX), "covers")
PREVIEWS = os.environ.get("SAYTHELINE_PREVIEWS", os.path.join(os.path.dirname(INDEX), "previews"))
CLIPS = os.environ.get("SAYTHELINE_CLIPS", os.path.join(os.path.dirname(INDEX), "clips"))

# Thumbnails are one ffmpeg seek each. Keyframe-only (-noaccurate_seek, -skip_frame nokey): a
# preview frame a second or two early is fine, and decoding up to the exact frame was taking
# several seconds on old AVIs. A results page asks for 30 at once; two at a time keeps the disks
# and the CPU calm.
FFMPEG_SLOTS = threading.Semaphore(2)


LIB = library.Library(os.environ.get("SAYTHELINE_LIBRARY", os.path.join(os.path.dirname(INDEX), "library.db")))
PREVIEW_SLOT = threading.Semaphore(1)
# Heavy work is serialised through ONE slot: renders, supercuts and snap-to-speech (Whisper)
# all wait their turn, so the web UI never stacks CPU jobs (small boxes stay cool and responsive).
RENDER_SLOT = threading.Semaphore(1)
SHOTS = shotcuts.Cache(os.environ.get("SAYTHELINE_SHOTS", os.path.join(os.path.dirname(INDEX), "shots")))
SUPERCUT_MAX_SEGMENTS, SUPERCUT_MAX_SECONDS = 80, 600
# Subtitle offset per item (ms, + = subtitles early), learnt whenever a snap or a supercut check
# hears a line. One show's subtitles were ~28 s late in every episode: learn it once, reuse it.
OFFSETS_PATH = os.path.join(os.path.dirname(INDEX), "offsets.json")
try:
    OFFSETS = {int(k): v for k, v in json.load(open(OFFSETS_PATH)).items()}
except (OSError, ValueError):
    OFFSETS = {}
OFFSETS_LOCK = threading.Lock()


def remember_offset(item_id, offset_ms):
    with OFFSETS_LOCK:
        if abs(offset_ms) < 250:
            if OFFSETS.pop(item_id, None) is None:
                return
        else:
            OFFSETS[item_id] = int(offset_ms)
        tmp = OFFSETS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(OFFSETS, f)
        os.replace(tmp, OFFSETS_PATH)
JOBS = {}                                  # job id -> state dict (in memory; renders are short)
JOBS_LOCK = threading.Lock()


def db():
    c = sqlite3.connect(f"file:{INDEX}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def clip_id(name):
    """Short stable id for a rendered file, for agents and links ("c3f9a1b2")."""
    return "c" + hashlib.sha1(name.encode()).hexdigest()[:8]


def find_clip(cid):
    """A rendered file and its recipe (the render's JSON sidecar) by clip id."""
    if not re.fullmatch(r"c[0-9a-f]{8}", cid or "") or not os.path.isdir(CLIPS):
        return None
    for f in os.listdir(CLIPS):
        if f.endswith(".json") or ".tmp." in f or clip_id(f) != cid:
            continue
        p = os.path.join(CLIPS, f)
        side = {}
        try:
            with open(p + ".json") as fh:
                side = json.load(fh)
        except (OSError, ValueError):
            pass
        item = side.get("item") or {}
        return dict(id=cid, file=f, url="/clips/" + quote(f), path=os.path.abspath(p), bytes=os.path.getsize(p),
                    item=item, label=srch.label(item) if item.get("kind") else None,
                    request=side.get("request"), result=side.get("result"),
                    supercut="supercut" in side or "sayanything" in side)     # many sources: not one editor cut
    return None


SHEET_W, SHEET_H = 320, 180


def item_frames(frames):
    """[(item_id, ms)] -> [(file path, ms)] for contact_sheet."""
    out = []
    for item_id, ms in frames[:12]:
        row = item_row(item_id)
        if not row or not os.path.exists(row["path"]):
            raise ValueError(f"item {item_id} has no readable file")
        out.append((row["path"], ms))
    return out


def contact_sheet(frames, cell=None):
    """One JPEG grid of numbered frames: frames = [(file path, ms)]. Lets a model look at several
    candidates, or at several moments of one rendered clip, in a single image. Accurate seeks;
    cached by path + times + file mtime."""
    frames = frames[:12]
    cw, ch = cell or (SHEET_W, SHEET_H)
    key = hashlib.sha1(json.dumps([(p, ms, os.path.getmtime(p)) for p, ms in frames] + [cw, ch]).encode()).hexdigest()[:16]
    out = os.path.join(THUMBS, "sheets", key + ".jpg")
    if os.path.exists(out):
        return out
    n = len(frames)
    cols = 1 if n == 1 else 2 if n <= 4 else 3
    rows_ = -(-n // cols)
    cmd, chains = ["ffmpeg", "-v", "error", "-nostdin", "-y"], []
    font = media._fpath(media.font_path("Inter"))
    for i, (path, ms) in enumerate(frames):
        cmd += ["-ss", f"{max(0, ms) / 1000:.3f}", "-i", path]
        chains.append(f"[{i}:v:0]scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
                      f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2,setsar=1,trim=end_frame=1,"
                      f"drawtext=fontfile={font}:text='{i + 1}':x=8:y=6:fontsize=30:fontcolor=white:"
                      f"box=1:boxcolor=black@0.65:boxborderw=6[v{i}]")
    blanks = cols * rows_ - n
    for j in range(blanks):
        cmd += ["-f", "lavfi", "-i", f"color=c=black:s={cw}x{ch}:d=1"]
        chains.append(f"[{n + j}:v:0]trim=end_frame=1[v{n + j}]")
    total = n + blanks
    if total == 1:
        graph = ";".join(chains) + ";[v0]null[out]"
    else:
        layout = "|".join(f"{(k % cols) * cw}_{(k // cols) * ch}" for k in range(total))
        graph = ";".join(chains) + ";" + "".join(f"[v{k}]" for k in range(total)) + f"xstack=inputs={total}:layout={layout}[out]"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with FFMPEG_SLOTS:
        r = subprocess.run(cmd + ["-filter_complex", graph, "-map", "[out]", "-frames:v", "1", "-q:v", "4", out + ".tmp.jpg"],
                           capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()[-300:])
    os.replace(out + ".tmp.jpg", out)
    return out


def thumbnail(item_id, ms):
    out = os.path.join(THUMBS, str(item_id), f"{ms}.jpg")
    if os.path.exists(out):
        return out
    row = db().execute("SELECT path FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row or not os.path.exists(row["path"]):
        return None
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp.jpg"
    with FFMPEG_SLOTS:
        if os.path.exists(out):
            return out
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-y", "-noaccurate_seek", "-skip_frame", "nokey",
             "-ss", f"{ms / 1000:.3f}", "-i", row["path"],
             "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "5", tmp],
            capture_output=True, timeout=60)
    if r.returncode != 0 or not os.path.exists(tmp):
        return None
    os.replace(tmp, out)
    return out




NO_COVER = set()


def cover_image(item_id):
    """The show's or movie's poster from Jellyfin, scaled for a phone and kept in data/covers
    (one per show, since its episodes share it). None when Jellyfin has none."""
    row = db().execute("SELECT jf_id, kind, series FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return None
    key = ("s-" + hashlib.sha1(row["series"].encode()).hexdigest()[:16]) if row["kind"] == "episode" and row["series"] else f"m-{item_id}"
    out = os.path.join(COVERS, key + ".jpg")
    if os.path.exists(out):
        return out
    if key in NO_COVER:
        return None
    try:
        src = jellyfin.cover(row["jf_id"])
    except Exception:
        src = None
    if not src:
        NO_COVER.add(key)
        return None
    os.makedirs(COVERS, exist_ok=True)
    tmp = out + ".tmp.jpg"
    with FFMPEG_SLOTS:
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", "pipe:0" if isinstance(src, bytes) else src,
                            "-frames:v", "1", "-vf", "scale='min(600,iw)':-2", "-q:v", "4", tmp],
                           input=src if isinstance(src, bytes) else None, capture_output=True, timeout=60)
    if r.returncode != 0 or not os.path.exists(tmp):
        NO_COVER.add(key)
        return None
    os.replace(tmp, out)
    return out


def font_file(family):
    """The font file the render uses for this family, so the browser preview shows the same face."""
    return media.font_path(family) or None


@functools.lru_cache(maxsize=128)
def cached_facets(query, mode, kind, stamp):
    return srch.facets(INDEX, query, mode, kind)


def index_stamp():
    """Changes whenever the indexer writes, so cached facet counts never go stale."""
    try:
        return max(os.path.getmtime(INDEX), os.path.getmtime(INDEX + "-wal") if os.path.exists(INDEX + "-wal") else 0)
    except OSError:
        return 0


def item_row(item_id):
    return db().execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


PREVIEW_CACHE_BYTES = int(float(os.environ.get("SAYTHELINE_PREVIEW_CACHE_GB", "2")) * 1e9)


def prune_previews():
    """Previews are ~2 MB each and only a cache: past the size limit, drop the least recently used."""
    files = []
    for d, _, names in os.walk(PREVIEWS):
        for n in names:
            p = os.path.join(d, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
    total = sum(f[1] for f in files)
    for _, size, p in sorted(files):
        if total <= PREVIEW_CACHE_BYTES * 0.8:      # trim to 80 % so this doesn't run on every preview
            break
        try:
            os.remove(p)
            total -= size
        except OSError:
            pass


def preview_file(item_id, start_ms, end_ms):
    # ".t": frame-exact timestamps (media.preview); ".a<n>": audio track n, when it isn't the first.
    # Older previews are left for the pruning to drop.
    row = item_row(item_id)
    if not row or not os.path.exists(row["path"]):
        return None
    track = media.audio_track(row["path"])
    out = os.path.join(PREVIEWS, str(item_id), f"{start_ms}-{end_ms}{f'.a{track}' if track else ''}.t.mp4")
    if os.path.exists(out):
        try:
            os.utime(out)                             # recently used: pruned last
        except OSError:
            pass
        return out
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with PREVIEW_SLOT:
        if not os.path.exists(out):
            tmp = out + ".tmp.mp4"
            media.preview(row["path"], start_ms, end_ms, tmp)
            os.replace(tmp, out)
    threading.Thread(target=prune_previews, daemon=True).start()
    return out


def clip_name(row, in_ms, text, fmt, aspect="original", audio="original"):
    if row["kind"] == "episode":
        who = f"{row['series']} - S{row['season'] or 0:02d}E{row['episode'] or 0:02d}"
    else:
        who = f"{row['title']} ({row['year']})"
    s = in_ms // 1000
    text = re.sub(r"(^|\s)[-–—]+\s+", r"\1", text).strip()     # "- Hi. - Hello." speaker dashes
    words = text[:48].rsplit(" ", 1)[0] if len(text) > 48 else text
    base = media.safe_name(f"{who} - {words} - {s // 60}m{s % 60:02d}s", 120)
    shape = "" if aspect in (None, "original") else f" ({aspect.replace(':', 'x')})"
    shape += {"dialogue": " [dialogue]", "nodialogue": " [no dialogue]"}.get(audio, "")
    return f"{base}{shape}.{fmt}"


def version_name(name, req):
    """A refinement (`parent` set) never replaces an existing clip: a style-only change gives the
    same name, so it becomes "… v2.gif", "… v3.gif". Editor re-renders keep replacing their file."""
    if not req.get("parent"):
        return name
    stem, ext = name.rsplit(".", 1)
    n, out = 1, name
    while os.path.exists(os.path.join(CLIPS, out)):
        n += 1
        out = f"{stem} v{n}.{ext}"
    return out


def run_render(job_id, req):
    job = JOBS[job_id]
    try:
        row = item_row(int(req["item"]))
        in_ms, out_ms = int(req["in_ms"]), int(req["out_ms"])
        if not row or out_ms <= in_ms or out_ms - in_ms > 180_000:
            raise ValueError("clip must be between 0 and 180 seconds")
        cues = clip_captions(row, in_ms, out_ms, req)
        fmt = req.get("format") if req.get("format") in ("mp4", "webm", "gif") else "mp4"
        os.makedirs(CLIPS, exist_ok=True)
        name = version_name(clip_name(row, in_ms, " ".join(c["text"] for c in cues) or "clip", fmt, req.get("aspect"),
                                      req.get("audio")), req)
        out = os.path.join(CLIPS, name)
        job["state"] = "queued"
        with RENDER_SLOT:
            job["state"] = "rendering"
            t = time.time()
            info = media.render(row["path"], in_ms, out_ms, {**req, "format": fmt}, cues, out + ".tmp." + fmt)
            os.replace(out + ".tmp." + fmt, out)
        with open(out + ".json", "w") as f:       # sidecar: re-render later with other options
            json.dump({"item": dict(row), "request": req, "result": info}, f, indent=1)
        job.update(state="done", file=name, url="/clips/" + name, secs=round(time.time() - t, 1),
                   item_id=row["id"], in_ms=in_ms, out_ms=out_ms, **info)
    except Exception as e:
        job.update(state="error", error=str(e)[:500])


def run_still(job_id, req):
    """A captioned frame at `at_ms` of the cut, as a JPEG next to the rendered clips."""
    job = JOBS[job_id]
    try:
        row = item_row(int(req["item"]))
        at, in_ms, out_ms = int(req["at_ms"]), int(req["in_ms"]), int(req["out_ms"])
        if not row or out_ms <= in_ms or out_ms - in_ms > 180_000:
            raise ValueError("clip must be between 0 and 180 seconds")
        cues = clip_captions(row, in_ms, out_ms, req)
        said = " ".join(c["text"] for c in cues if c["start_ms"] <= at < c["end_ms"]) or "still"
        os.makedirs(CLIPS, exist_ok=True)
        name = version_name(clip_name(row, at, said, "jpg", req.get("aspect")).replace(".jpg", " (still).jpg"), req)
        out = os.path.join(CLIPS, name)
        job["state"] = "queued"
        with RENDER_SLOT:
            job["state"] = "rendering"
            t = time.time()
            info = media.still(row["path"], at, in_ms, out_ms, req, cues, out + ".tmp.jpg")
            os.replace(out + ".tmp.jpg", out)
        with open(out + ".json", "w") as f:       # sidecar: the recipe, so it can be refined later
            json.dump({"item": dict(row), "request": req, "result": info}, f, indent=1)
        job.update(state="done", file=name, url="/clips/" + name, secs=round(time.time() - t, 1),
                   item_id=row["id"], in_ms=in_ms, out_ms=out_ms, at_ms=at, **info)
    except Exception as e:
        job.update(state="error", error=str(e)[:500])


def new_job(kind):
    job_id = os.urandom(6).hex()
    with JOBS_LOCK:
        JOBS[job_id] = {"id": job_id, "kind": kind, "state": "queued"}
    return job_id


def item_shots(item_id, a_ms, b_ms):
    """(camera cuts as boundary ms, margin_ms) in [a_ms, b_ms], or ([], 0) when the file can't be
    scanned: a failed scan never stops a render or a supercut, the clip just keeps its padding."""
    row = item_row(item_id)
    if not row or not os.path.exists(row["path"]):
        return [], 0
    try:
        r = SHOTS.cuts(row["id"], row["path"], a_ms, b_ms)
        return [c["ms"] for c in r["cuts"]], r["margin_ms"]
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
        return [], 0


def item_cues(item_id, a, b, offset=0):
    """Subtitle cues overlapping [a, b] on the MEDIA timeline. `offset` (ms, + = subtitles are
    early) is the correction measured by snap, so a file whose subtitles run 28 s late still
    gets the right lines once one snap has found them."""
    rows = db().execute("SELECT idx, start_ms, end_ms, text FROM cues WHERE item_id = ? AND end_ms > ? "
                        "AND start_ms < ? ORDER BY idx", (item_id, a - offset, b - offset)).fetchall()
    return [dict(r, start_ms=r["start_ms"] + offset, end_ms=r["end_ms"] + offset) for r in rows]


def run_snap(job_id, req):
    job = JOBS[job_id]
    try:
        row = item_row(int(req["item"]))
        in_ms, out_ms = int(req["in_ms"]), int(req["out_ms"])
        if not row or not 0 < out_ms - in_ms <= 60_000:
            raise ValueError("snap works on cuts up to 60 s")
        touching = item_cues(row["id"], in_ms, out_ms, int(req.get("offset_ms") or 0))
        # Listen for the lines mostly inside the cut. The default padding clips the edge of the
        # neighbouring lines, and snapping to those would grow the cut to include them.
        def inside(c):
            return (min(c["end_ms"], out_ms) - max(c["start_ms"], in_ms)) / max(1, c["end_ms"] - c["start_ms"])
        cues = [c for c in touching if inside(c) >= 0.7] or touching
        near = [c for c in item_cues(row["id"], in_ms - 45000, out_ms + 45000, int(req.get("offset_ms") or 0))
                if c not in cues]
        with RENDER_SLOT:
            job["state"] = "listening"
            t = time.time()
            res = cached_snap(row["path"], in_ms, out_ms, cues, True, near)
        if res.get("found"):
            base = int(req.get("offset_ms") or 0)
            remember_offset(row["id"], base + (res["offset_ms"] if abs(res["offset_ms"]) >= 250 else 0))
        res.pop("words", None)
        job.update(state="done", secs=round(time.time() - t, 1), **res)
    except Exception as e:
        job.update(state="error", error=str(e)[:500])


def run_transcribe(job_id, req):
    job = JOBS[job_id]
    try:
        row = item_row(int(req["item"]))
        a, b = int(req["from_ms"]), int(req["to_ms"])
        if not row or not 0 < b - a <= 120_000:
            raise ValueError("transcribe works on up to 120 s")
        with RENDER_SLOT:
            job["state"] = "listening"
            t = time.time()
            if req.get("source") == "subs":     # keep the subtitle text, take word times from speech
                reach = snapper.SUB_REACH_MS        # the lines either side too: subtitles can run early or late
                subs = item_cues(row["id"], a - reach, b + reach, int(req.get("offset_ms") or 0))
                cues = snapper.subtitle_words(row["path"], a, b, subs)
            else:
                cues = snapper.transcribe_cues(row["path"], a, b)
        job.update(state="done", secs=round(time.time() - t, 1), from_ms=a, to_ms=b, cues=cues)
    except Exception as e:
        job.update(state="error", error=str(e)[:500])


def caption_shift(req):
    """The editor's caption Earlier / Later for this clip, in ms (+ = later), at most 5 s."""
    return max(-5000, min(5000, int(req.get("caption_shift_ms") or 0)))


def shift_cues(cues, ms):
    """Captions moved `ms` later (= JS shiftCues), their word times with them."""
    if not ms:
        return cues
    return [dict(c, start_ms=c["start_ms"] + ms, end_ms=c["end_ms"] + ms,
                 **({"words": [dict(w, s=w["s"] + ms, e=w["e"] + ms) for w in c["words"]]} if c.get("words") else {}))
            for c in cues]


def clip_captions(row, in_ms, out_ms, req):
    """The captions a clip burns in: the client's (a transcript, or word-timed subtitles) or the
    subtitle track with this clip's edits, moved by its caption shift, cut to the clip."""
    shift = caption_shift(req)
    if req.get("caption_source") == "speech" or (req.get("subs") or {}).get("style") in media.LOOKS:
        cues = caption_cues(req)
    else:                       # lines just outside the cut can be shifted into it
        cues = subtitle_cues(row["id"], in_ms - shift, out_ms - shift, req)
    return [c for c in shift_cues(cues, shift) if c["end_ms"] > in_ms and c["start_ms"] < out_ms]


def subtitle_cues(item_id, in_ms, out_ms, req):
    """The cut's subtitle lines with the editor's per-clip edits, `sub_text` {idx: text, or null =
    hidden}: a neighbouring line that spills into the cut, or a line worded differently on screen."""
    edits = req.get("sub_text") or {}
    out = []
    for c in item_cues(item_id, in_ms, out_ms, int(req.get("offset_ms") or 0)):
        t = edits.get(str(c["idx"]), c["text"])
        if t is not None and str(t).strip():
            out.append(dict(c, text=str(t)[:300]))
    return out


def caption_cues(req):
    """Captions sent by the client (speech transcript, or subtitle text with word times) instead
    of the subtitle track. Words are kept for word-synced styles. Subtitle lines (they carry their
    `idx`) take the same `sub_text` edits as plain subtitles."""
    edits = (req.get("sub_text") or {}) if req.get("caption_source") != "speech" else {}
    out = []
    for c in req.get("caption_cues") or []:
        a, b, t = int(c["start_ms"]), int(c["end_ms"]), str(c["text"])[:300]
        words = [dict(w=str(w["w"])[:60], s=int(w["s"]), e=int(w["e"]),
                      **({"loud": float(w["loud"])} if isinstance(w.get("loud"), (int, float)) else {}))
                 for w in (c.get("words") or [])[:80]]
        idx = c.get("idx")
        if idx is not None and str(idx) in edits:
            new = edits[str(idx)]
            if new is None:
                continue
            t, words = str(new)[:300], reword(words, str(new), a, b)
        if b > a and t.strip():
            out.append(dict(start_ms=a, end_ms=b, text=t, words=words, **({} if idx is None else {"idx": idx})))
    return out


def reword(words, text, a, b):
    """Word times for a reworded line: the heard times when the word count is unchanged, else the
    new words spread evenly over the time the old ones took."""
    new = text.split()[:80]
    if words and len(words) == len(new):
        return [dict(w, w=n[:60]) for w, n in zip(words, new)]
    s, e = (words[0]["s"], words[-1]["e"]) if words else (a, b)
    step = (e - s) / max(1, len(new))
    return [dict(w=n[:60], s=round(s + i * step), e=round(s + (i + 1) * step)) for i, n in enumerate(new)]


def run_poster(job_id, req):
    """Poster (or, with "look": "kinetic", Kinetic) layout for the editor's live preview: the same
    layout() the render uses, in output pixels, plus the output size so the browser can scale it
    onto the preview."""
    job = JOBS[job_id]
    try:
        from . import kinetic, poster
        row = item_row(int(req["item"]))
        in_ms, out_ms = int(req["in_ms"]), int(req["out_ms"])
        w, h = media.output_size(row["path"], req)
        words = media.caption_words(shift_cues(caption_cues(req), caption_shift(req)), in_ms, out_ms)
        job["state"] = "laying out"
        lay = kinetic.layout if req.get("look") == "kinetic" else poster.layout
        job.update(state="done", w=w, h=h, posters=lay(words, w, h, req.get("subs") or {}))
    except Exception as e:
        job.update(state="error", error=str(e)[:500])


# ---- supercut --------------------------------------------------------------------------------
SC_PAD = {"tight": (150, 250), "normal": (450, 550), "loose": (900, 1000)}


def dedupe_key(row, text):
    t = re.sub(r"[^a-z0-9 ]", "", (text or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    if row["kind"] == "episode":
        return ("e", (row["series"] or "").lower(), row["season"], row["episode"], t)
    return ("m", (row["title"] or "").lower(), row["year"], t)


def sc_label(row):
    if row["kind"] == "episode":
        return f"{row['series']} · S{row['season'] or 0:02d}E{row['episode'] or 0:02d}"
    return f"{row['title']} ({row['year']})"


# ---- listen cache ------------------------------------------------------------------------------
# Checking a line by ear is the slow step (a few seconds of Whisper), and the same lines come up
# again and again: common words in Say Anything, a supercut re-rendered, a take swapped back.
# Each snap result is kept, keyed on everything it depends on: the file (and its mtime), the
# window, the subtitle lines listened for and their neighbours, and LISTEN_V.
LISTEN_DB = os.path.join(os.path.dirname(INDEX), "listen.db")
LISTEN_V = 1          # bump whenever snap.py or stt_worker.py timing changes: old results are re-heard


def _listen_key(path, a, b, cues, wide, near):
    try:
        mt = os.path.getmtime(path)
    except OSError:
        mt = 0
    sig = [LISTEN_V, path, mt, a, b, bool(wide), [(c["start_ms"], c["end_ms"], c["text"]) for c in cues],
           [(c["start_ms"], c["text"]) for c in near]]
    return hashlib.sha1(json.dumps(sig).encode()).hexdigest()


def cached_snap(path, a, b, cues, wide, near):
    key = _listen_key(path, a, b, cues, wide, near)
    try:
        with contextlib.closing(sqlite3.connect(LISTEN_DB, timeout=10)) as c:
            c.execute("CREATE TABLE IF NOT EXISTS snaps (key TEXT PRIMARY KEY, res TEXT NOT NULL, at REAL NOT NULL)")
            row = c.execute("SELECT res FROM snaps WHERE key = ?", (key,)).fetchone()
        if row:
            return json.loads(row[0])
    except sqlite3.Error:
        pass                          # a broken cache only costs the listen
    res = snapper.snap(path, a, b, cues, wide=wide, context=near)
    try:
        with contextlib.closing(sqlite3.connect(LISTEN_DB, timeout=10)) as c:
            c.execute("INSERT OR REPLACE INTO snaps VALUES (?, ?, ?)", (key, json.dumps(res), time.time()))
            c.commit()
    except sqlite3.Error:
        pass
    return res


def sc_check_hit(row, hit, query, offset=None, wide=True):
    """Listen to one hit. Returns raw facts (not a cut), so padding and cut style can change later
    without listening again: where the line's words were heard, where the searched phrase is, the
    subtitle offset, and the word timings around it for captions."""
    off = OFFSETS.get(row["id"], 0) if offset is None else offset
    cues = item_cues(row["id"], int(hit["start_ms"]) + off, int(hit["end_ms"]) + off, off)
    cues = [c for c in cues if c["start_ms"] >= int(hit["start_ms"]) + off - 50] or cues
    if not cues:
        return dict(found=False, status="no subtitle line")
    a, b = min(c["start_ms"] for c in cues), max(c["end_ms"] for c in cues)
    if lang := media.foreign_audio(row["path"]):
        # English subtitles over other speech: nothing to listen for, so the subtitles' own timing
        # stands, and word looks get each line's words spread evenly across it
        words = [dict(w=w, s=round(c["start_ms"] + i * (c["end_ms"] - c["start_ms"]) / len(c["text"].split())),
                      e=round(c["start_ms"] + (i + 1) * (c["end_ms"] - c["start_ms"]) / len(c["text"].split())))
                 for c in cues for i, w in enumerate(c["text"].split())]
        return dict(found=False, foreign=lang, status=f"{lang} audio: subtitle timing", offset_ms=off,
                    sub_in=a, sub_out=b, words=words)
    near = [c for c in item_cues(row["id"], a - 45000, b + 45000, off) if c["end_ms"] <= a or c["start_ms"] >= b]
    res = cached_snap(row["path"], a, b, cues, wide, near)
    if not res.get("found"):
        return dict(found=False, status="not heard", offset_ms=off, sub_in=a, sub_out=b)
    total = off + (res["offset_ms"] if abs(res["offset_ms"]) >= 250 else 0)
    ph = snapper.phrase_span(res["words"], query, res["first_s"], res["last_e"])
    words = [w for w in res["words"] if w["e"] > res["first_s"] - 3000 and w["s"] < res["last_e"] + 3000]
    return dict(found=True, status="ok", offset_ms=total, line_s=res["first_s"], line_e=res["last_e"],
                phrase_s=ph[0] if ph else None, phrase_e=ph[1] if ph else None,
                heard=res["heard"], words=words, sub_in=a, sub_out=b)


def check_hits(hits, query, results, progress=None):
    """Check hits (skipping ones already in `results`), then make each episode consistent.

    A subtitle file is off by one constant amount, so every hit in the same item must agree on
    the offset. A wide search can latch onto the wrong repeat of a line ("we need to talk" twice, 30 s
    apart, with subtitles 28 s late), which shows up as hits that disagree or that land on the same
    spoken moment. Those are re-checked narrowly at the offset most hits in that item agree on."""
    todo = [h for h in hits if f"{h['item']}:{h['start_ms']}" not in results]
    for i, h in enumerate(todo):
        row = item_row(int(h["item"]))
        key = f"{h['item']}:{h['start_ms']}"
        with RENDER_SLOT:
            if progress:
                progress(i, len(todo), key)
            try:
                results[key] = sc_check_hit(row, h, query) if row else dict(found=False, status="missing")
            except Exception as e:  # one bad file must not stop the check
                results[key] = dict(found=False, status="error: " + str(e)[:120])
    by_item = {}
    for h in hits:
        r = results.get(f"{h['item']}:{h['start_ms']}")
        if r and r.get("found"):
            by_item.setdefault(int(h["item"]), []).append((h, r))
    for item, pairs in by_item.items():
        votes = {}
        for _, r in pairs:                               # cluster offsets within 1.5 s
            k = next((v for v in votes if abs(v - r["offset_ms"]) <= 1500), r["offset_ms"])
            votes[k] = votes.get(k, 0) + 1
        best = max(votes, key=lambda v: (votes[v], -abs(v)))
        if len(votes) > 1 and len(set(votes.values())) == 1:      # a tie: trust whichever offset the
            def ctx(v):                                          # neighbouring lines agree with
                total = 0
                for h, r in pairs:
                    near = item_cues(item, r["sub_in"] - 45000, r["sub_out"] + 45000, r["offset_ms"])
                    total += snapper._context_score(r["words"], near, v - r["offset_ms"])
                return total
            best = max(votes, key=ctx)
        seen = {}
        for h, r in pairs:
            dup = any(abs(r["line_s"] - s0) < 400 for s0 in seen.values())
            seen[h["start_ms"]] = r["line_s"]
            if abs(r["offset_ms"] - best) > 1500 or dup:
                row = item_row(item)
                with RENDER_SLOT:
                    again = sc_check_hit(row, h, query, offset=best, wide=False)
                key = f"{h['item']}:{h['start_ms']}"
                if again.get("found"):
                    again["status"] = "ok (re-checked against this episode's offset)"
                    results[key] = again
                else:
                    results[key] = dict(again, status="not heard at this episode's offset")
        remember_offset(item, best)
    return results


class Cancelled(Exception):
    pass


def stop_if_cancelled(job):
    """Long jobs call this between steps; POST /api/job/<id>/cancel sets the flag."""
    if job.get("cancel"):
        raise Cancelled()


def run_sc_check(job_id, req):
    """Check every selected hit against the speech, one at a time, reporting as it goes."""
    job = JOBS[job_id]
    # A new check supersedes an older one still going (the sheet was reopened, or Check tapped
    # again): two would only queue up behind each other and double the wait.
    with JOBS_LOCK:
        for j in JOBS.values():
            if j is not job and j["kind"] == "check" and j["state"] in ("queued", "checking"):
                j["cancel"] = True
    try:
        hits = (req.get("hits") or [])[:SUPERCUT_MAX_SEGMENTS]
        job.update(state="checking", done=0, total=len(hits), results=dict(req.get("checks") or {}))

        def progress(i, n, key):
            stop_if_cancelled(job)
            job.update(state="checking", done=i, total=n, current=key)
        check_hits(hits, req.get("query", ""), job["results"], progress)
        job.update(state="done", done=len(hits))
    except Cancelled:
        job.update(state="cancelled")
    except Exception as e:
        job.update(state="error", error=str(e)[:500])


def sc_segment(row, hit, facts, req):
    """Turn one hit (+ its check facts, if any) into a cut, honouring the current settings."""
    pad_in, pad_out = SC_PAD.get(req.get("padding"), SC_PAD["normal"])
    if facts and facts.get("found"):
        if req.get("cut") == "phrase" and facts.get("phrase_s") is not None:
            a, b = facts["phrase_s"], facts["phrase_e"]
        else:
            a, b = facts["line_s"], facts["line_e"]
        off, how = facts["offset_ms"], "checked"
    else:
        foreign = (facts or {}).get("foreign")
        if facts and not facts.get("found") and not foreign and req.get("unheard") != "keep":
            return None                                   # a wrong clip is worse than a missing one
        off = (facts or {}).get("offset_ms", OFFSETS.get(row["id"], 0))
        a, b = int(hit["start_ms"]) + off, int(hit["end_ms"]) + off
        how = f"subtitle timing ({foreign} audio)" if foreign else "subtitle timing"
    in_ms, out_ms = max(0, a - pad_in), b + pad_out
    if req.get("shots", True):              # start on the line's own shot, end before the next
        cuts, margin = item_shots(row["id"], in_ms, out_ms)
        in_ms, out_ms = shotcuts.fit(in_ms, out_ms, a, b, cuts, margin)
    return dict(item=row["id"], src=row["path"], in_ms=in_ms, out_ms=out_ms, offset_ms=off,
                words=(facts or {}).get("words") or [], how=how, label=sc_label(row) if req.get("label") else None,
                key=f"{hit['item']}:{hit['start_ms']}")


def sc_captions(seg, req):
    """Caption cues for one segment: subtitle lines (classic) or heard words (word looks)."""
    st = req.get("subs") or {}
    if not st.get("on"):
        return []
    if st.get("style") in media.LOOKS:
        ws = [w for w in seg["words"] if w["e"] > seg["in_ms"] and w["s"] < seg["out_ms"]]
        return [dict(start_ms=ws[0]["s"], end_ms=ws[-1]["e"], text=" ".join(w["w"] for w in ws), words=ws)] if ws else []
    return item_cues(seg["item"], seg["in_ms"], seg["out_ms"], seg["offset_ms"])


def run_supercut(job_id, req):
    """req: {"query", "hits": [{"item","start_ms","end_ms"}] in final order, "checks": {key: facts},
    "check": bool (listen to unchecked hits first), "cut": line|phrase, "padding", "unheard": skip|keep,
    "label", "transition": cut|fade, "loudnorm", "aspect", "res", "subs": {...}}"""
    job = JOBS[job_id]
    try:
        with JOBS_LOCK:
            busy = [j for j in JOBS.values() if j is not job and j["kind"] == "supercut"
                    and j["state"] in ("queued", "checking", "shots", "rendering")]
        if busy:
            raise ValueError("another supercut is still being made: stop it first, or wait for it in Library > All rendered")
        hits = (req.get("hits") or [])[:SUPERCUT_MAX_SEGMENTS]
        if not hits:
            raise ValueError("no clips selected")
        checks = dict(req.get("checks") or {})
        need_check = req.get("check") or req.get("cut") == "phrase" or ((req.get("subs") or {}).get("style") in media.LOOKS
                                                                         and (req.get("subs") or {}).get("on"))
        rows = {i: item_row(int(h["item"])) for i, h in enumerate(hits)}
        if need_check:
            def progress(i, n, key):
                stop_if_cancelled(job)
                job.update(state="checking", done=i, total=n)
            check_hits(hits, req.get("query", ""), checks, progress)
        segs, report, total = [], [], 0
        for i, h in enumerate(hits):
            key = f"{h['item']}:{h['start_ms']}"
            if req.get("shots", True):
                stop_if_cancelled(job)
                job.update(state="shots", done=i, total=len(hits))
            if not rows[i]:
                report.append(dict(key=key, status="missing file"))
                continue
            seg = sc_segment(rows[i], h, checks.get(key), req)
            if not seg:
                report.append(dict(key=key, status="skipped: " + checks[key].get("status", "not heard")))
                continue
            # the same moment twice (repeated line, overlapping hits): merge into one cut
            if segs and segs[-1]["item"] == seg["item"] and seg["in_ms"] < segs[-1]["out_ms"] + 300 \
                    and seg["out_ms"] > segs[-1]["in_ms"] - 300:
                segs[-1]["in_ms"] = min(segs[-1]["in_ms"], seg["in_ms"])
                segs[-1]["out_ms"] = max(segs[-1]["out_ms"], seg["out_ms"])
                segs[-1]["words"] = sorted({(w["s"], w["e"], w["w"]): w for w in segs[-1]["words"] + seg["words"]}.values(),
                                           key=lambda w: w["s"])
                report.append(dict(key=key, status="merged with the previous clip"))
                continue
            total += seg["out_ms"] - seg["in_ms"]
            if total > SUPERCUT_MAX_SECONDS * 1000:
                report.append(dict(key=key, status=f"left out: over {SUPERCUT_MAX_SECONDS // 60} min"))
                break
            segs.append(seg)
            report.append(dict(key=key, status=seg["how"]))
        if not segs:
            raise ValueError("nothing left to render (every clip was skipped)")
        for sg in segs:
            sg["cues"] = sc_captions(sg, req)
        os.makedirs(CLIPS, exist_ok=True)
        q = media.safe_name(req.get("query") or "supercut", 50)
        stamp = time.strftime("%m%d-%H%M")
        name = f"Supercut - {q} - {len(segs)} clips ({stamp}).mp4"
        out = os.path.join(CLIPS, name)

        def progress(i, n):
            stop_if_cancelled(job)
            job.update(state="rendering", done=i, total=n)
        t = time.time()
        with RENDER_SLOT:
            info = media.supercut(segs, req, out + ".tmp.mp4", progress)
            os.replace(out + ".tmp.mp4", out)
        with open(out + ".json", "w") as f:
            json.dump({"supercut": {k: v for k, v in req.items() if k != "checks"}, "report": report, "result": info}, f, indent=1)
        first = segs[0]
        job.update(state="done", file=name, url="/clips/" + name, secs=round(time.time() - t, 1), report=report,
                   checks=checks, item_id=first["item"], in_ms=first["in_ms"], out_ms=first["out_ms"], **info)
    except Cancelled:
        job.update(state="cancelled", checks=checks)
    except Exception as e:
        job.update(state="error", error=str(e)[:500])


# ---- Say Anything ----------------------------------------------------------------------------
SA_MAX_WORDS = 40


def sa_find(req):
    """Exact-phrase subtitle hits for Say Anything: short lines first (cleaner takes), then any line
    with the phrase, narrowed by sayany.pick to the exact words and a spread of titles."""
    kind = req.get("kind") or None

    def find(q):
        want = q.split()
        pq = sayany.phrase_query(want)
        hits = srch.search(INDEX, pq, exact=True, limit=sayany.POOL, short=True, kind=kind)
        seen = {(h["item_id"], h["start_ms"]) for h in hits}
        hits += [h for h in srch.search(INDEX, pq, exact=True, limit=sayany.POOL, kind=kind)
                 if (h["item_id"], h["start_ms"]) not in seen]
        return [hit_json(h) for h in sayany.pick(hits, want)]
    return find


def sa_listen(job):
    def listen(hit, text):
        stop_if_cancelled(job)
        row = item_row(int(hit["item_id"]))
        if not row or media.foreign_audio(row["path"]):
            return dict(found=False, status="no English audio")
        with RENDER_SLOT:
            try:
                return sc_check_hit(row, hit, text, wide=False)
            except Exception as e:  # one bad file must not stop the sentence
                return dict(found=False, status="error: " + str(e)[:120])
    return listen


def sa_piece_json(p):
    t = p["take"]
    take = None
    if t:
        h = t["hit"]
        row = item_row(int(h["item_id"]))
        if row and "cuts" not in t:
            try:
                t["cuts"] = sayany.cuts(t, row["path"])
            except (OSError, subprocess.SubprocessError):
                pass                      # the padding alone still cuts it
        take = dict(item_id=h["item_id"], label=h["label"], start_ms=h["start_ms"], end_ms=h["end_ms"],
                    line=h["text"], thumb=h["thumb"], s=t["s"], e=t["e"], before=t["before"], after=t["after"],
                    score=t["score"], words=t["words"], cuts=t.get("cuts"))
    return dict(text=p["text"], want=p["want"], take=take, alts=p["alts"], why=p.get("why"))


def run_sa_plan(job_id, req):
    """req: {"sentence", "kind"}. Finds and listens to a take for every piece of the sentence."""
    job = JOBS[job_id]
    try:
        sentence = " ".join((req.get("sentence") or "").split()[:SA_MAX_WORDS])
        if not sayany.words(sentence):
            raise ValueError("type a sentence first")
        job.update(state="listening", listened=0, current="")

        def progress(text, n):
            job.update(state="listening", listened=n, current=text)
        pieces = sayany.plan(sentence, sa_find(req), sa_listen(job), progress=progress)
        job.update(state="done", sentence=sentence, pieces=[sa_piece_json(p) for p in pieces])
    except Cancelled:
        job.update(state="cancelled")
    except Exception as e:
        job.update(state="error", error=str(e)[:500])


def run_sa_take(job_id, req):
    """req: {"piece": {text, want, alts}}. Listens down the piece's other candidates for a new take."""
    job = JOBS[job_id]
    try:
        piece = req.get("piece") or {}
        alts, want, text = list(piece.get("alts") or []), piece.get("want") or [], piece.get("text") or ""
        listen = sa_listen(job)
        while alts:
            h = alts.pop(0)
            job.update(state="listening", left=len(alts))
            t = sayany.take_from(listen(h, text), want)
            if t:
                job.update(state="done", piece=sa_piece_json(dict(text=text, want=want, take=dict(t, hit=h), alts=alts)))
                return
        job.update(state="done", piece=None)
    except Cancelled:
        job.update(state="cancelled")
    except Exception as e:
        job.update(state="error", error=str(e)[:500])


def run_sa_render(job_id, req):
    """req: {"sentence", "pieces": [{text, take: {item_id, s, e, before, after, words}, nudge_in, nudge_out}],
    "padding", "aspect", "res", "fit", "loudnorm", "subs"}"""
    job = JOBS[job_id]
    try:
        st = req.get("subs") or {}
        segs, said = [], []
        for p in req.get("pieces") or []:
            t = p.get("take")
            if not t:
                continue
            row = item_row(int(t["item_id"]))
            if not row:
                continue
            a, b = sayany.cut(t, req.get("padding"))
            a, b = max(0, a + int(p.get("nudge_in") or 0)), b + int(p.get("nudge_out") or 0)
            if b - a < 80:
                continue
            ws = [dict(w, s=max(w["s"], a), e=min(w["e"], b)) for w in sayany.caption_words(p, t)]
            said.append(p["text"])
            cues = []
            if st.get("on"):
                # word looks: this piece's words as they are said; subtitles: the sentence so far
                cues = ([dict(start_ms=a, end_ms=b, text=" ".join(w["w"] for w in ws), words=ws)]
                        if st.get("style") in media.LOOKS else [dict(start_ms=a, end_ms=b, text=sayany.so_far(said))])
            segs.append(dict(src=row["path"], in_ms=a, out_ms=b, cues=cues, label=None))
        if not segs:
            raise ValueError("no piece of the sentence has a take")
        os.makedirs(CLIPS, exist_ok=True)
        name = f"Say Anything - {media.safe_name(req.get('sentence') or 'sentence', 50)} ({time.strftime('%m%d-%H%M')}).mp4"
        out = os.path.join(CLIPS, name)

        def progress(i, n):
            stop_if_cancelled(job)
            job.update(state="rendering", done=i, total=n)
        t0 = time.time()
        opts = dict(req, transition="cut")
        with RENDER_SLOT:
            info = media.supercut(segs, opts, out + ".tmp.mp4", progress)
            os.replace(out + ".tmp.mp4", out)
        with open(out + ".json", "w") as f:
            json.dump({"sayanything": req, "result": info}, f, indent=1)
        job.update(state="done", file=name, url="/clips/" + name, secs=round(time.time() - t0, 1), **info)
    except Cancelled:
        job.update(state="cancelled")
    except Exception as e:
        job.update(state="error", error=str(e)[:500])


# ---- script view ------------------------------------------------------------------------------
def script_lines(item_id):
    """(body, cache) for /api/lines: the item, every subtitle line in order, and for an episode the
    ids of the episodes either side (‹ › in the script view)."""
    c = db()
    row = c.execute("SELECT id, kind, series, season, episode, title, year FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return {"error": "no such title"}, None
    item = dict(row, label=srch.label(row))
    lines = [list(r) for r in c.execute("SELECT idx, start_ms, end_ms, text FROM cues WHERE item_id = ? ORDER BY idx",
                                        (item_id,))]
    prev = nxt = None
    if row["kind"] == "episode":
        key = (row["series"], row["season"] or 0, row["episode"] or 0)
        prev = c.execute("SELECT id FROM items WHERE kind = 'episode' AND series = ? AND (ifnull(season, 0), ifnull(episode, 0)) < (?, ?) "
                         "ORDER BY ifnull(season, 0) DESC, ifnull(episode, 0) DESC LIMIT 1", key).fetchone()
        nxt = c.execute("SELECT id FROM items WHERE kind = 'episode' AND series = ? AND (ifnull(season, 0), ifnull(episode, 0)) > (?, ?) "
                        "ORDER BY ifnull(season, 0), ifnull(episode, 0) LIMIT 1", key).fetchone()
    return dict(item=item, lines=lines, prev=prev and prev[0], next=nxt and nxt[0]), "max-age=60"


def browse(text, series=None, kind=None):
    """Titles that have a script: shows and movies whose name matches `text` (for main search,
    above the matching lines), or one show's episodes. Each title gets a frame to show."""
    c = db()
    if series:
        return [dict(r) for r in c.execute("SELECT id, season, episode, title FROM items WHERE kind = 'episode' AND series = ? "
                                           "ORDER BY ifnull(season, 0), ifnull(episode, 0)", (series,))]
    text = text.strip().strip('"')
    if len(text) < 2:
        return []
    like = f"%{text}%"
    out = []

    def frame(item_id, runtime_s):         # a frame a third of the way in: past the titles
        return f"/thumb/{item_id}/{int((runtime_s or 900) * 1000 * 0.33)}.jpg"
    if kind != "movie":
        for name, n, seasons in c.execute("SELECT series, COUNT(*), COUNT(DISTINCT ifnull(season, 0)) FROM items "
                                          "WHERE kind = 'episode' AND series LIKE ? GROUP BY series LIMIT 30", (like,)).fetchall():
            first = c.execute("SELECT id, runtime_s FROM items WHERE kind = 'episode' AND series = ? "
                              "ORDER BY ifnull(season, 0) = 0, season, episode LIMIT 1", (name,)).fetchone()
            out.append(dict(kind="series", name=name, n=n, seasons=seasons, thumb=frame(*first)))
    if kind != "episode":
        for i, title, year, rt in c.execute("SELECT id, title, year, runtime_s FROM items WHERE kind = 'movie' AND title LIKE ? "
                                            "LIMIT 30", (like,)).fetchall():
            out.append(dict(kind="movie", id=i, label=f"{title} ({year})" if year and not title.endswith(f"({year})") else title,
                            year=year, thumb=frame(i, rt)))
    # names that start with the text first ("Friends" before "Friends with Benefits" before "Best Friends")
    low = text.lower()

    def rank(x):
        name = (x.get("name") or x.get("label") or "").lower()
        return (0 if name == low else 1 if name.startswith(low) else 2, name)
    return sorted(out, key=rank)[:40]


def hit_json(h):
    it = h["item"]
    return dict(
        item_id=h["item_id"], idx=h["idx"], kind=it["kind"], series=it["series"],
        season=it["season"], episode=it["episode"], title=it["title"], year=it["year"],
        label=srch.label(it), start_ms=h["start_ms"], end_ms=h["end_ms"],
        time=srch.fmt_ms(h["start_ms"]), text=h["text"], before=h["before"], after=h["after"], tier=h.get("tier", 1),
        thumb=f"/thumb/{h['item_id']}/{(h['start_ms'] + h['end_ms']) // 2}.jpg")


class Handler(BaseHTTPRequestHandler):
    server_version = "saytheline"

    def log_message(self, fmt, *args):  # quiet: the journal does not need every thumbnail
        if not self.path.startswith(("/thumb/", "/cover/", "/preview/", "/api/job/")):
            super().log_message(fmt, *args)

    def send(self, code, body, ctype="application/json", cache=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype, download=None):
        """Static file with HTTP Range support (iOS Safari will not play <video> without it)."""
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = size - int(m.group(2))
            end = min(end, size - 1)
        self.send_response(206 if m else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if m:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(download)}")
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            m = re.fullmatch(r"/api/job/([0-9a-f]+)/cancel", u.path)
            if m:
                job = JOBS.get(m.group(1))
                if job:
                    job["cancel"] = True
                return self.send(200 if job else 404, {"cancelled": bool(job)})
            if u.path == "/api/library":
                return self.send(200, LIB.save(req))
            m = re.fullmatch(r"/api/library/(\d+)(/delete)?", u.path)
            if m:
                if m.group(2):
                    LIB.delete(int(m.group(1)))
                    return self.send(200, {"deleted": int(m.group(1))})
                row = LIB.update(int(m.group(1)), req)
                return self.send(200 if row else 404, row or {"error": "not found"})
            runners = {"/api/render": run_render, "/api/snap": run_snap, "/api/supercut": run_supercut,
                       "/api/supercut/check": run_sc_check, "/api/sayany/plan": run_sa_plan,
                       "/api/sayany/take": run_sa_take, "/api/sayany/render": run_sa_render,
                       "/api/transcribe": run_transcribe, "/api/poster": run_poster, "/api/still": run_still}
            if u.path in runners:
                job_id = new_job(("sa-" if u.path.startswith("/api/sayany/") else "") + u.path.rsplit("/", 1)[-1])
                threading.Thread(target=runners[u.path], args=(job_id, req), daemon=True).start()
                return self.send(202, JOBS[job_id])
            return self.send(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.send(500, {"error": str(e)})

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                with open(os.path.join(HERE, "static", "index.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8", "no-cache")
            if u.path == "/api/facets":
                mode = q.get("mode", "phrase") if q.get("mode") in ("phrase", "any", "raw") else "phrase"
                kind = q.get("kind") if q.get("kind") in ("episode", "movie") else None
                try:
                    return self.send(200, cached_facets(q.get("q", ""), mode, kind, index_stamp()))
                except sqlite3.OperationalError as e:
                    return self.send(400, {"error": str(e)})
            if u.path == "/api/search":
                t = time.time()
                limit = min(int(q.get("limit", 30)), 300)      # supercuts pull a few hundred
                offset = max(int(q.get("offset", 0)), 0)
                mode = q.get("mode", "phrase") if q.get("mode") in ("phrase", "any", "raw") else "phrase"
                kind = q.get("kind") if q.get("kind") in ("episode", "movie") else None
                multi = parse_qs(u.query)
                only = multi.get("only") if "only" in multi or q.get("none") else None
                try:
                    hits = srch.search(INDEX, q.get("q", ""), mode, q.get("series") or None,
                                       limit + 1, offset, kind, only=[] if q.get("none") else only,
                                       exclude=multi.get("ex"), short=q.get("short") == "1", exact=q.get("exact") == "1")
                except sqlite3.OperationalError as e:  # bad FTS5 syntax in raw mode
                    return self.send(400, {"error": str(e)})
                return self.send(200, {"hits": [hit_json(h) for h in hits[:limit]],
                                       "more": len(hits) > limit,
                                       "ms": round((time.time() - t) * 1000)})
            if u.path == "/api/context":
                return self.send(200, srch.context(INDEX, int(q["item"]), int(q["idx"])))
            if u.path == "/api/item":
                row = item_row(int(q["id"]))
                if not row:
                    return self.send(404, {"error": "no such item"})
                d = dict(row)
                d["label"] = srch.label(row)
                return self.send(200, d)
            if u.path == "/fonts.css":
                css = "".join(f"@font-face{{font-family:'{f}';font-weight:700;src:url('/font/{quote(f)}')}}\n"
                              for f in media.FONTS if font_file(f))
                return self.send(200, css, "text/css", "max-age=86400")
            if u.path.startswith("/font/"):
                p = font_file(unquote(u.path[6:]))
                if unquote(u.path[6:]) not in media.FONTS or not p:
                    return self.send(404, {"error": "no font"})
                ext = p.rsplit(".", 1)[-1].lower()
                return self.send_file(p, {"otf": "font/otf", "woff2": "font/woff2"}.get(ext, "font/ttf"))
            if u.path == "/api/cues":
                return self.send(200, item_cues(int(q["item"]), int(q["from_ms"]), int(q["to_ms"]),
                                                int(q.get("offset_ms") or 0)))
            if u.path == "/api/options":
                return self.send(200, dict(fonts=media.FONTS, sizes=list(media.SIZES), stems=bool(config.demucs()),
                                           colours=list(media.COLOURS), aspects=["original", *media.ASPECTS]),
                                 cache="max-age=300")
            if u.path.startswith("/api/job/"):
                job = JOBS.get(u.path.rsplit("/", 1)[-1])
                return self.send(200 if job else 404, job or {"error": "unknown job"})
            if u.path == "/api/clips":
                os.makedirs(CLIPS, exist_ok=True)
                files = sorted((f for f in os.listdir(CLIPS) if not f.endswith(".json") and ".tmp." not in f),
                               key=lambda f: -os.path.getmtime(os.path.join(CLIPS, f)))[:50]
                return self.send(200, [dict(id=clip_id(f), file=f, url="/clips/" + quote(f),
                                            bytes=os.path.getsize(os.path.join(CLIPS, f)),
                                            mtime=int(os.path.getmtime(os.path.join(CLIPS, f)))) for f in files])
            m = re.fullmatch(r"/api/clip/(c[0-9a-f]{8})/sheet", u.path)
            if m:
                c = find_clip(m.group(1))
                if not c:
                    return self.send(404, {"error": "no clip with that id"})
                if c["file"].endswith(".jpg"):
                    return self.send_file(c["path"], "image/jpeg")
                secs = float((c.get("result") or {}).get("seconds") or media.probe(c["path"])["duration"] or 1)
                n = max(1, min(int(q.get("n", 3)), 9))
                times = [int(secs * 1000 * (k + 0.5) / n) for k in range(n)]
                try:
                    res = c.get("result") or {}
                    w, h = res.get("width") or 16, res.get("height") or 9
                    cell = (max(120, min(400, round(240 * w / h / 2) * 2)), 240)     # cells in the clip's own shape
                    return self.send_file(contact_sheet([(c["path"], t) for t in times], cell), "image/jpeg")
                except RuntimeError as e:
                    return self.send(400, {"error": str(e)})
            m = re.fullmatch(r"/api/clip/(c[0-9a-f]{8})", u.path)
            if m:
                c = find_clip(m.group(1))
                return self.send(200 if c else 404, c or {"error": "no clip with that id"})
            if u.path == "/api/sheet":
                try:
                    frames = [tuple(int(x) for x in f.split(":")[:2]) for f in parse_qs(u.query).get("f", [])]
                    if not frames:
                        return self.send(400, {"error": "give f=<item>:<ms> once per frame (up to 12)"})
                    return self.send_file(contact_sheet(item_frames(frames)), "image/jpeg")
                except (ValueError, RuntimeError) as e:
                    return self.send(400, {"error": str(e)})
            m = re.fullmatch(r"/preview/(\d+)/(\d+)-(\d+)\.mp4", u.path)
            if m:
                a, b = int(m.group(2)), int(m.group(3))
                if not 0 < b - a <= 180_000:
                    return self.send(400, {"error": "preview window must be 1-180 s"})
                p = preview_file(int(m.group(1)), a, b)
                return self.send_file(p, "video/mp4") if p else self.send(404, {"error": "no file"})
            if u.path.startswith("/clips/"):
                name = os.path.basename(unquote(u.path[len("/clips/"):]))
                p = os.path.join(CLIPS, name)
                if not name or not os.path.isfile(p) or name.endswith(".json"):
                    return self.send(404, {"error": "no such clip"})
                ctype = {"mp4": "video/mp4", "webm": "video/webm", "gif": "image/gif", "jpg": "image/jpeg"}.get(name.rsplit(".", 1)[-1], "application/octet-stream")
                return self.send_file(p, ctype, download=name if q.get("dl") else None)
            if u.path == "/api/library":
                return self.send(200, LIB.list(q.get("tag") or None, q.get("kind") or None))
            if u.path == "/api/draft":
                row = LIB.get(int(q["id"])) if q.get("id") else LIB.draft(int(q["item"]), int(q["start_ms"]))
                return self.send(200, row or {})
            if u.path == "/api/shots":
                row = item_row(int(q["item"]))
                if not row or not os.path.exists(row["path"]):
                    return self.send(404, {"error": "no readable file for that item"})
                try:
                    return self.send(200, SHOTS.cuts(row["id"], row["path"], int(q["from_ms"]), int(q["to_ms"])))
                except ValueError as e:
                    return self.send(400, {"error": str(e)})
                except (RuntimeError, subprocess.TimeoutExpired) as e:
                    return self.send(500, {"error": "couldn't scan for camera cuts: " + str(e)[:200]})
            if u.path == "/api/offset":
                return self.send(200, {"offset_ms": OFFSETS.get(int(q["item"]), 0)})
            if u.path == "/api/tags":
                return self.send(200, LIB.tags())
            if u.path == "/api/busy":             # jobs still running: check before restarting the server
                with JOBS_LOCK:
                    run = [dict(id=k, kind=j["kind"], state=j["state"]) for k, j in JOBS.items()
                           if j["state"] not in ("done", "error", "cancelled")]
                return self.send(200, run)
            if u.path == "/api/lines":            # a title's whole subtitle, for the script view
                body, cache = script_lines(int(q["item"]))
                return self.send(404 if "error" in body else 200, body, cache=cache)
            if u.path == "/api/browse":           # the script view's title picker
                return self.send(200, browse(q.get("q", ""), q.get("series"), q.get("kind")))
            if u.path == "/api/titles":           # every show and movie, for the title filter's "no hits" note
                rows = db().execute("SELECT DISTINCT series AS name FROM items WHERE kind = 'episode' "
                                    "UNION SELECT title || CASE WHEN year THEN ' (' || year || ')' ELSE '' END "
                                    "FROM items WHERE kind = 'movie' ORDER BY 1").fetchall()
                return self.send(200, [r[0] for r in rows if r[0]], cache="max-age=300")
            if u.path == "/api/series":
                rows = db().execute("SELECT series, COUNT(*) n FROM items WHERE kind = 'episode' "
                                    "GROUP BY series ORDER BY series").fetchall()
                return self.send(200, [dict(r) for r in rows], cache="max-age=300")
            if u.path == "/api/stats":
                c = db()
                return self.send(200, dict(
                    episodes=c.execute("SELECT COUNT(*) FROM items WHERE kind='episode'").fetchone()[0],
                    movies=c.execute("SELECT COUNT(*) FROM items WHERE kind='movie'").fetchone()[0],
                    shows=c.execute("SELECT COUNT(DISTINCT series) FROM items WHERE kind='episode'").fetchone()[0],
                    cues=c.execute("SELECT COUNT(*) FROM cues").fetchone()[0]), cache="max-age=300")
            m = re.fullmatch(r"/cover/(\d+)\.jpg", u.path)
            if m:
                p = cover_image(int(m.group(1)))
                if not p:
                    return self.send(404, b"", "image/jpeg")
                with open(p, "rb") as f:
                    return self.send(200, f.read(), "image/jpeg", "max-age=86400")
            m = re.fullmatch(r"/thumb/(\d+)/(\d+)\.jpg", u.path)
            if m:
                p = thumbnail(int(m.group(1)), int(m.group(2)))
                if not p:
                    return self.send(404, b"", "image/jpeg")
                with open(p, "rb") as f:
                    return self.send(200, f.read(), "image/jpeg", "max-age=2592000, immutable")
            return self.send(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.send(500, {"error": str(e)})


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"Say the Line web on http://{HOST}:{PORT}/  (index {INDEX})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
