"""Say the Line web UI: search, result cards and thumbnails (step 1); a preview window with in/out
handles (step 2); render and download (step 3).

Standard library only (ThreadingHTTPServer), like the rest of Say the Line. There is no login: keep
it on 127.0.0.1 (the default) or a private network such as a tailnet, never the open internet.

  python3 -m saytheline.web            # http://$SAYTHELINE_HOST:$SAYTHELINE_PORT/
"""
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
from . import library
from . import media
from . import search as srch
from . import shots as shotcuts
from . import snap as snapper

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.environ.get("SAYTHELINE_INDEX", os.path.join(config.data_dir(), "cues.db"))
THUMBS = os.environ.get("SAYTHELINE_THUMBS", os.path.join(os.path.dirname(INDEX), "thumbs"))
HOST = os.environ.get("SAYTHELINE_HOST", "127.0.0.1")
PORT = int(os.environ.get("SAYTHELINE_PORT", "8765"))
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
                    request=side.get("request"), result=side.get("result"), supercut="supercut" in side)
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
    # ".t": frame-exact timestamps (media.preview); older previews are left for the pruning to drop
    out = os.path.join(PREVIEWS, str(item_id), f"{start_ms}-{end_ms}.t.mp4")
    if os.path.exists(out):
        try:
            os.utime(out)                             # recently used: pruned last
        except OSError:
            pass
        return out
    row = item_row(item_id)
    if not row or not os.path.exists(row["path"]):
        return None
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
        if req.get("caption_source") == "speech" or (req.get("subs") or {}).get("style") in media.LOOKS:
            cues = caption_cues(req)
        else:
            cues = subtitle_cues(row["id"], in_ms, out_ms, req)
        cues = [c for c in cues if c["end_ms"] > in_ms and c["start_ms"] < out_ms]
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
        if req.get("caption_source") == "speech" or (req.get("subs") or {}).get("style") in media.LOOKS:
            cues = caption_cues(req)
        else:
            cues = subtitle_cues(row["id"], in_ms, out_ms, req)
        cues = [c for c in cues if c["end_ms"] > in_ms and c["start_ms"] < out_ms]
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
            res = snapper.snap(row["path"], in_ms, out_ms, cues, context=near)
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
                subs = item_cues(row["id"], a, b, int(req.get("offset_ms") or 0))
                cues = snapper.subtitle_words(row["path"], a, b, subs)
            else:
                cues = snapper.transcribe_cues(row["path"], a, b)
        job.update(state="done", secs=round(time.time() - t, 1), from_ms=a, to_ms=b, cues=cues)
    except Exception as e:
        job.update(state="error", error=str(e)[:500])


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
        words = [dict(w=str(w["w"])[:60], s=int(w["s"]), e=int(w["e"])) for w in (c.get("words") or [])[:80]]
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
    """Poster layout for the editor's live preview: the same poster.layout() the render uses,
    in output pixels, plus the output size so the browser can scale it onto the preview."""
    job = JOBS[job_id]
    try:
        from . import poster
        row = item_row(int(req["item"]))
        in_ms, out_ms = int(req["in_ms"]), int(req["out_ms"])
        w, h = media.output_size(row["path"], req)
        words = media.caption_words(caption_cues(req), in_ms, out_ms)
        job["state"] = "laying out"
        job.update(state="done", w=w, h=h, posters=poster.layout(words, w, h, req.get("subs") or {}))
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
    near = [c for c in item_cues(row["id"], a - 45000, b + 45000, off) if c["end_ms"] <= a or c["start_ms"] >= b]
    res = snapper.snap(row["path"], a, b, cues, wide=wide, context=near)
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
        if facts and not facts.get("found") and req.get("unheard") != "keep":
            return None                                   # a wrong clip is worse than a missing one
        off = (facts or {}).get("offset_ms", OFFSETS.get(row["id"], 0))
        a, b = int(hit["start_ms"]) + off, int(hit["end_ms"]) + off
        how = "subtitle timing"
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


def hit_json(h):
    it = h["item"]
    return dict(
        item_id=h["item_id"], idx=h["idx"], kind=it["kind"], series=it["series"],
        season=it["season"], episode=it["episode"], title=it["title"], year=it["year"],
        label=srch.label(it), start_ms=h["start_ms"], end_ms=h["end_ms"],
        time=srch.fmt_ms(h["start_ms"]), text=h["text"], before=h["before"], after=h["after"],
        thumb=f"/thumb/{h['item_id']}/{(h['start_ms'] + h['end_ms']) // 2}.jpg")


class Handler(BaseHTTPRequestHandler):
    server_version = "saytheline"

    def log_message(self, fmt, *args):  # quiet: the journal does not need every thumbnail
        if not self.path.startswith(("/thumb/", "/preview/", "/api/job/")):
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
                       "/api/supercut/check": run_sc_check,
                       "/api/transcribe": run_transcribe, "/api/poster": run_poster, "/api/still": run_still}
            if u.path in runners:
                job_id = new_job(u.path.rsplit("/", 1)[-1])
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
                                       exclude=multi.get("ex"), short=q.get("short") == "1")
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
