"""Say the Line as an MCP server, so an AI agent can search your library and make clips, reaction GIFs
and stills, then refine them from your feedback.

  python -m saytheline mcp            # stdio; needs `pip install -r requirements-mcp.txt`

A thin adapter over the running Say the Line web server's HTTP API (`python -m saytheline serve`), so
agents and the web UI share one job queue (one heavy job at a time), one library and one set of
learnt subtitle offsets, and what an agent makes shows up in the app too.

Designed for a model rather than a person (see docs/mcp.md):
  - few workflow-level tools: search, look, make, refine, not one per endpoint
  - readable ids: a moment is "<item>:<line>" ("51:99"), a rendered clip is "c" + 8 hex
  - concise text by default, structured fields for chaining, an image so the model can check
    what it made before showing anyone
  - errors say what to change
"""
import asyncio
import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from . import config  # noqa: F401  (loads saytheline.toml into the environment)
from . import shots as shotcuts       # the pure fit / whole_shot helpers (no scanning here)

BASE = os.environ.get("SAYTHELINE_URL") or \
    f"http://{os.environ.get('SAYTHELINE_HOST', '127.0.0.1').replace('0.0.0.0', '127.0.0.1')}:{os.environ.get('SAYTHELINE_PORT', '8765')}"
PUBLIC = (os.environ.get("SAYTHELINE_PUBLIC_URL") or BASE).rstrip("/")

FORMATS = ("gif", "mp4", "webm", "still")
LOOKS = ("classic", "tiktok", "popin", "bigword", "poster")
SHAPES = ("original", "16:9", "9:16", "1:1", "4:3")
WORD_LOOKS = LOOKS[1:]

INSTRUCTIONS = """Say the Line searches everything said in the user's own movie and TV library (by subtitles) and
renders clips from it: GIFs, MP4/WebM clips, captioned stills and supercuts.

Workflow:
1. saytheline_search for a line. For reactions, set short_lines_only=true: the line is little more
   than the words, which makes clean clips. Each hit has a moment id like "51:99".
2. Optional: saytheline_look at a few candidates (one numbered image) to judge faces and framing.
3. saytheline_make_clip with the moment id. Defaults suit a reaction GIF: snapped to the spoken words
   (subtitle timing is often off by seconds), tight padding that stops between lines (no half
   sentence either side) and at the camera cuts (so it neither opens nor ends on a sliver of
   another shot), plain subtitles burned in. whole_shot=true
   takes the entire shot the line is in: often the best reaction face.
4. Check the preview image and the subtitle lines listed in the result (a neighbouring line that
   is cut off at an edge and on screen under a second is hidden for you; hide_lines / line_text handle the rest). Then give the user the file: the result has its local
   path (in Claude Code, send it with SendUserFile) and a link.
5. For feedback ("start earlier", "bigger text", "no captions", "make it vertical"), use
   saytheline_refine_clip with the clip id; it renders a new version and keeps the old one.
   edit_url opens the clip in the web editor if the user wants to fine-tune it by hand.

Keep reaction GIFs short (1-4 s): one line, maybe the beat after it. Prefer well-known scenes.
This is the user's personal library: don't list its contents beyond what the task needs."""

mcp = MCPServer("say-the-line", title="Say the Line", instructions=INSTRUCTIONS, version="1.0")


# ---------------------------------------------------------------- HTTP to the Say the Line server
def _http(method, path, body=None, raw=False, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except ValueError:
            msg = None
        raise ToolError(msg or f"Say the Line answered {e.code} for {path}")
    except (urllib.error.URLError, OSError):
        raise ToolError(f"the Say the Line server is not reachable at {BASE}. Start it with `python -m saytheline serve` "
                        "(or the saytheline-web service), then retry.")
    return payload if raw else json.loads(payload or b"{}")


async def api(method, path, body=None, raw=False, timeout=120):
    return await asyncio.to_thread(_http, method, path, body, raw, timeout)


async def run_job(ctx, path, body, label):
    """Start a Say the Line job and wait for it, passing its progress on to the client."""
    job = await api("POST", path, body)
    step, last = 0, None
    while job.get("state") not in ("done", "error", "cancelled"):
        step += 1
        done, total = job.get("done"), job.get("total")
        msg = {"queued": "waiting for the renderer (one heavy job at a time)"}.get(job.get("state"), job.get("state"))
        now = (job.get("state"), done)
        try:
            if now != last:                         # report changes, not every poll
                if done is not None and total:
                    await ctx.report_progress(done, total, f"{label}: {msg} {done + 1}/{total}")
                else:
                    await ctx.report_progress(step, None, f"{label}: {msg}")
        except Exception:
            pass                                    # progress is best-effort
        last = now
        await asyncio.sleep(0.6 if job.get("state") == "queued" else 0.4)
        job = await api("GET", f"/api/job/{job['id']}")
    if job["state"] != "done":
        raise ToolError(f"{label} failed: {job.get('error') or job['state']}")
    return job


# ---------------------------------------------------------------- helpers
def _mid(moment):
    m = re.fullmatch(r"\s*(\d+):(\d+)\s*", moment or "")
    if not m:
        raise ToolError(f'"{moment}" is not a moment id. Use the id from saytheline_search, like "51:99" (item:line).')
    return int(m.group(1)), int(m.group(2))


def _fmt(ms):
    s = max(0, int(ms)) // 1000
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


async def _moment(moment):
    """The line, its neighbours, the item and the learnt subtitle offset for a moment id."""
    item_id, idx = _mid(moment)
    lines = await api("GET", f"/api/context?item={item_id}&idx={idx}")
    line = next((c for c in lines if c["idx"] == idx), None)
    if not line:
        raise ToolError(f"moment {moment} not found (the index may have changed): search again.")
    item = await api("GET", f"/api/item?id={item_id}")
    off = (await api("GET", f"/api/offset?item={item_id}")).get("offset_ms", 0)
    return dict(item_id=item_id, idx=idx, line=line, lines=lines, item=item, offset=off)


def _label(item):
    if item.get("kind") == "episode":
        return f"{item.get('series')} S{item.get('season') or 0:02d}E{item.get('episode') or 0:02d}"
    t, y = item.get("title") or "", item.get("year")
    return t if not y or t.endswith(f"({y})") else f"{t} ({y})"


async def _title_keys(query, mode, names):
    """Show / movie names -> filter keys, matched against the titles that have hits."""
    if not names:
        return []
    rows = await api("GET", "/api/facets?" + urllib.parse.urlencode({"q": query, "mode": mode}))
    keys = []
    for name in names:
        n = name.strip().lower()
        exact = [r for r in rows if (r["label"] or "").lower() == n]
        part = exact or [r for r in rows if (r["label"] or "").lower().startswith(n)] or \
            [r for r in rows if n in (r["label"] or "").lower()]
        if not part:
            near = ", ".join(r["label"] for r in rows[:8])
            raise ToolError(f'no title "{name}" has hits for this search. Titles with hits: {near}')
        keys += [r["key"] for r in part[:5]]
    return keys


def _image(jpeg):
    return ImageContent(type="image", data=base64.b64encode(jpeg).decode(), mime_type="image/jpeg")


def _result(text, structured, image=None):
    content = [TextContent(type="text", text=text)]
    if image:
        content.append(_image(image))
    return CallToolResult(content=content, structured_content=structured)


def _even_words(text, a, b):
    ws = text.split()
    step = (b - a) / max(1, len(ws))
    return [dict(w=w, s=round(a + i * step), e=round(a + (i + 1) * step)) for i, w in enumerate(ws)]


def _caption_text_cues(text, cues):
    """Replace the caption text, keeping the heard word times when the word count matches."""
    words = [w for c in cues for w in (c.get("words") or [])]
    new = text.split()
    a = cues[0]["start_ms"] if cues else 0
    b = cues[-1]["end_ms"] if cues else 0
    if words and len(words) == len(new):
        ws = [dict(w, w=n) for w, n in zip(words, new)]
    else:
        ws = _even_words(text, words[0]["s"] if words else a, words[-1]["e"] if words else b)
    return [dict(start_ms=ws[0]["s"] if ws else a, end_ms=max(b, ws[-1]["e"] if ws else b), text=text, words=ws)]


def clip_id(name):
    return "c" + hashlib.sha1(name.encode()).hexdigest()[:8]       # = web.clip_id


async def _clip_payload(job, req, notes):
    """The common result of make / refine: summary text, ids, paths and a look at the output."""
    clip = await api("GET", f"/api/clip/{clip_id(job['file'])}")
    sheet = await api("GET", f"/api/clip/{clip['id']}/sheet?n={1 if clip['file'].endswith('.jpg') else 3}", raw=True)
    fmt = clip["file"].rsplit(".", 1)[-1]
    res = clip.get("result") or {}
    secs = res.get("seconds")
    w, h = job.get("width") or res.get("width"), job.get("height") or res.get("height")
    src = "several titles (supercut)" if clip.get("supercut") else clip.get("label")
    s = dict(clip_id=clip["id"], file=clip["file"], path=clip["path"], url=PUBLIC + clip["url"],
             edit_url=None if clip.get("supercut") else f"{PUBLIC}/?edit={clip['id']}", format=fmt, seconds=secs,
             width=w, height=h, bytes=clip["bytes"], source=src, in_ms=req.get("in_ms"), out_ms=req.get("out_ms"), notes=notes)
    lines = await _lines_on_screen(req) if not clip.get("supercut") else []
    s["lines"] = lines
    size = f"{clip['bytes'] / 1e6:.1f} MB" if clip["bytes"] > 1e6 else f"{clip['bytes'] // 1000} KB"
    shown = "; ".join(f"{ln['line']} \"{ln['text']}\" {ln['from_ms'] / 1000:.1f}-{ln['to_ms'] / 1000:.1f} s"
                      + (" (hidden)" if ln["hidden"] else "") for ln in lines)
    text = (f"Made {clip['id']}: {fmt.upper()} {w}x{h}{f', {secs} s' if secs else ''}, {size} from {src}."
            + (f" Notes: {'; '.join(notes)}." if notes else "")
            + (f"\nSubtitle lines (number, text, on screen): {shown}" if shown else "")
            + f"\nFile: {clip['path']}\nLink: {s['url']}" + (f"\nEdit by hand: {s['edit_url']}" if s["edit_url"] else "")
            + "\nThe image shows frames of the result: check the caption and framing before sending it.")
    return _result(text, s, sheet)


async def _render(ctx, req, label="render"):
    if req["format"] == "still":
        return await run_job(ctx, "/api/still", dict(req, at_ms=req.get("at_ms") or (req["in_ms"] + req["out_ms"]) // 2), "still")
    return await run_job(ctx, "/api/render", req, label)


async def _captions_for(ctx, req, captions, look, caption_text):
    """Fill in caption fields of a render request: source, look, and word timing when needed."""
    st = req.setdefault("subs", {})
    st.update(on=captions != "none", style=look if captions != "none" else "classic")
    req["caption_source"] = "speech" if captions == "speech" else "subs"
    req.pop("caption_cues", None)
    need_words = captions != "none" and (captions == "speech" or look in WORD_LOOKS or caption_text)
    if not need_words:
        return
    job = await run_job(ctx, "/api/transcribe", dict(item=req["item"], from_ms=req["in_ms"], to_ms=req["out_ms"],
                                                     source="speech" if captions == "speech" else "subs",
                                                     offset_ms=req.get("offset_ms", 0)), "timing the words")
    cues = job.get("cues") or []
    if caption_text:
        cues = _caption_text_cues(caption_text, cues or [dict(start_ms=req["in_ms"], end_ms=req["out_ms"], words=[])])
        req["caption_source"] = "speech"            # the render then uses exactly these cues
    if not cues:
        raise ToolError("no words were heard in this cut, so word-by-word captions can't be timed: "
                        "use look='classic', captions='none', or a caption_text")
    req["caption_cues"] = cues


FLASH_LINE_MS = 1000       # a neighbouring line cut off at an edge and on screen for less than this is hidden


async def _lines_on_screen(req):
    """The subtitle lines the clip shows, with when each is on screen (ms from the clip's start)
    and whether it is hidden. Empty for no captions, speech or a custom caption_text."""
    st = req.get("subs") or {}
    if not st.get("on") or req.get("caption_source") == "speech":
        return []
    a, b = int(req["in_ms"]), int(req["out_ms"])
    if req.get("caption_cues"):
        cues = [c for c in req["caption_cues"] if c.get("idx") is not None]
    else:
        cues = await api("GET", f"/api/cues?item={req['item']}&from_ms={a}&to_ms={b}&offset_ms={int(req.get('offset_ms') or 0)}")
    edits = req.get("sub_text") or {}
    out = []
    for c in cues:
        s, e = max(a, c["start_ms"]), min(b, c["end_ms"])
        if e <= s:
            continue
        k = str(c["idx"])
        out.append(dict(line=c["idx"], text=c["text"] if edits.get(k, c["text"]) is None else edits.get(k, c["text"]),
                        from_ms=s - a, to_ms=e - a, cut_off=c["start_ms"] < a or c["end_ms"] > b,
                        hidden=k in edits and edits[k] is None))
    return out


def _edit_lines(req, lines, hide=(), show=(), text=None):
    """Apply hide / show / reword requests (by line number) to the clip's `sub_text` edits."""
    if (hide or show or text) and not lines:
        raise ToolError("hide_lines / show_lines / line_text work on subtitle captions; this clip has none "
                        "(captions are off, from speech, or a caption_text)")
    known = {ln["line"] for ln in lines}
    asked = {int(n) for n in [*hide, *show, *(text or {})]}
    if asked - known:
        listed = "; ".join(f"{ln['line']} \"{ln['text']}\"" for ln in lines)
        raise ToolError(f"line {', '.join(map(str, sorted(asked - known)))} isn't in this clip. Lines on screen: {listed}")
    edits = dict(req.get("sub_text") or {})
    for n in show:
        edits.pop(str(int(n)), None)
    for n, t in (text or {}).items():
        edits[str(int(n))] = str(t)
    for n in hide:
        edits[str(int(n))] = None
    req["sub_text"] = edits


def _hide_flashes(req, lines, keep=()):
    """Hide lines that are cut off by the clip's edge and on screen too briefly to read (the end of
    the previous line, the start of the next): the editor's trash button, done for the agent."""
    edits = req.setdefault("sub_text", {})
    moment = (req.get("moment") or {}).get("idx")
    gone = []
    for ln in lines:
        if ln["cut_off"] and not ln["hidden"] and ln["to_ms"] - ln["from_ms"] < FLASH_LINE_MS \
                and ln["line"] != moment and ln["line"] not in keep and str(ln["line"]) not in edits:
            edits[str(ln["line"])] = None
            ln["hidden"] = True
            gone.append(f"line {ln['line']} (on screen {(ln['to_ms'] - ln['from_ms']) / 1000:.1f} s)")
    return f"hid {', '.join(gone)}: cut off at the edge, too brief to read; show_lines brings it back" if gone else ""


# ---------------------------------------------------------------- tools
@mcp.tool(title="Search lines", annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False))
async def saytheline_search(query: str, match: str = "phrase", kind: str = "all", titles: list[str] | None = None,
                          exclude_titles: list[str] | None = None, short_lines_only: bool = False,
                          limit: int = 8, offset: int = 0) -> CallToolResult:
    """Find where a line is said in the user's movies and TV shows (subtitle search).

    query: words to find, e.g. "i'll be back". Stemmed: "twin" also finds "twins".
    match: "phrase" (the words in order, default) or "any" (any of the words).
    kind: "all", "tv" or "movies".
    titles / exclude_titles: show or movie names to keep / drop, e.g. ["Some Show"].
    short_lines_only: only lines that are little more than the query ("Twins!") - best for reactions.
    limit: hits to return (1-25, default 8); offset pages through more.
    Returns each hit's moment id ("item:line", use it with the other tools), title, time, the line
    and its neighbours, plus the total and which titles have the most hits."""
    if not query.strip():
        raise ToolError("query is empty: give the words to search for")
    mode = "any" if match == "any" else "phrase"
    p = [("q", query), ("mode", mode), ("limit", max(1, min(int(limit), 25))), ("offset", max(0, int(offset)))]
    if kind in ("tv", "movies"):
        p.append(("kind", "episode" if kind == "tv" else "movie"))
    only = await _title_keys(query, mode, titles)
    if titles:
        p += [("only", k) for k in only]
    p += [("ex", k) for k in await _title_keys(query, mode, exclude_titles)]
    if short_lines_only:
        p.append(("short", "1"))
    data = await api("GET", "/api/search?" + urllib.parse.urlencode(p))
    facets = await api("GET", "/api/facets?" + urllib.parse.urlencode({"q": query, "mode": mode}))
    counted = bool(facets) and facets[0].get("n") is not None
    ex = {v for k, v in p if k == "ex"}
    total = None if short_lines_only or not counted else \
        sum(f["n"] for f in facets if (not titles or f["key"] in only) and f["key"] not in ex)
    hits = [dict(moment_id=f"{h['item_id']}:{h['idx']}", title=_label(h), episode_title=h["title"] if h["kind"] == "episode" else None,
                 time=_fmt(h["start_ms"]), line=h["text"], before=h["before"], after=h["after"])
            for h in data["hits"]]
    if not hits:
        raise ToolError(f'no hits for "{query}"' + (" with these filters" if titles or exclude_titles or short_lines_only else "")
                        + ". Try fewer words, match='any', or drop short_lines_only.")
    head = f'{len(hits)} of {"many" if total is None else total} hits for "{query}"'
    out = [head + (" (short lines only)" if short_lines_only else "") + ":"]
    for i, h in enumerate(hits, 1 + int(offset)):
        ep = f' "{h["episode_title"]}"' if h["episode_title"] else ""
        before = f'  (before: "{h["before"]}")' if h["before"] else ""
        out.append(f'{i}. [{h["moment_id"]}] {h["title"]}{ep} @{h["time"]}: "{h["line"]}"{before}')
    if counted and not titles:
        out.append("Most hits: " + ", ".join(f"{f['label']} {f['n']}" for f in facets[:5]))
    if data.get("more"):
        out.append(f"More: offset={int(offset) + len(hits)}")
    return _result("\n".join(out), dict(hits=hits, total=total, more=data.get("more", False),
                                        top_titles=[dict(title=f["label"], hits=f["n"]) for f in facets[:10]] if counted else []))


@mcp.tool(title="Look at moments", annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False))
async def saytheline_look(moment_ids: list[str] | None = None, clip_id: str = "", frames: int = 0) -> CallToolResult:
    """See frames, as one numbered image, to judge faces, framing and mood before choosing.

    moment_ids: up to 9 moment ids from saytheline_search; one frame each, numbered in order.
    With exactly one moment id, frames=N (2-9) shows N frames across that line instead.
    clip_id: frames of a clip you already made (to check it)."""
    if clip_id:
        img = await api("GET", f"/api/clip/{clip_id.strip()}/sheet?n={max(1, min(frames or 3, 9))}", raw=True)
        return _result(f"Frames of {clip_id}, in order.", dict(clip_id=clip_id), img)
    ids = (moment_ids or [])[:9]
    if not ids:
        raise ToolError("give moment_ids (from saytheline_search) or a clip_id")
    pts, legend = [], []
    if len(ids) == 1 and frames > 1:
        m = await _moment(ids[0])
        a, b = m["line"]["start_ms"] + m["offset"], m["line"]["end_ms"] + m["offset"]
        n = min(frames, 9)
        pts = [(m["item_id"], int(a - 500 + (b - a + 1000) * (k + 0.5) / n)) for k in range(n)]
        legend = [f"{k + 1}: {_fmt(t)}" for k, (_, t) in enumerate(pts)]
        head = f'{n} frames across "{m["line"]["text"]}" ({_label(m["item"])}):'
    else:
        for k, mid in enumerate(ids, 1):
            m = await _moment(mid)
            pts.append((m["item_id"], (m["line"]["start_ms"] + m["line"]["end_ms"]) // 2 + m["offset"]))
            legend.append(f'{k}: [{mid}] {_label(m["item"])} "{m["line"]["text"]}"')
        head = "One frame per moment, as numbered:"
    img = await api("GET", "/api/sheet?" + urllib.parse.urlencode([("f", f"{i}:{t}") for i, t in pts]), raw=True)
    return _result(head + "\n" + "\n".join(legend), dict(frames=legend), img)


WHOLE_SHOT_REACH = 12_000       # ms looked at either side of the line for its shot's ends


async def _fit_shots(item, in_ms, out_ms, keep, whole):
    """(in_ms, out_ms, note): the padding trimmed to the camera cuts in it (never into `keep`, the
    line or its heard words), or with `whole` the entire shot(s) holding `keep`. A failed scan
    leaves the cut as it was."""
    a, b = (keep[0] - WHOLE_SHOT_REACH, keep[1] + WHOLE_SHOT_REACH) if whole else (in_ms, out_ms)
    try:
        j = await api("GET", f"/api/shots?item={item}&from_ms={max(0, int(a))}&to_ms={int(b) + 1}")
    except ToolError:
        return in_ms, out_ms, "couldn't scan for camera cuts, so the padding is as usual"
    cuts, margin = [c["ms"] for c in j.get("cuts") or []], j.get("margin_ms") or 0
    if whole:
        s, e = shotcuts.whole_shot(keep[0], keep[1], cuts, margin)
        n_in, n_out = (in_ms if s is None else s), (out_ms if e is None else e)
        if s is None and e is None:
            return in_ms, out_ms, "no camera cut within 12 s of the line, so the cut is as usual"
        return n_in, n_out, f"the whole shot ({(n_out - n_in) / 1000:.1f} s)" + (
            "" if s is not None and e is not None else "; one end has no cut nearby, so it stays as it was")
    n_in, n_out = shotcuts.fit(in_ms, out_ms, keep[0], keep[1], cuts, margin)
    said = [f"starts on the line's own shot ({(n_in - in_ms) / 1000:.1f} s trimmed)" if n_in != in_ms else "",
            f"ends before the next shot ({(out_ms - n_out) / 1000:.1f} s trimmed)" if n_out != out_ms else ""]
    return n_in, n_out, ", ".join(x for x in said if x)


@mcp.tool(title="Make a clip, GIF or still", annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                                                     idempotent_hint=False, open_world_hint=False))
async def saytheline_make_clip(ctx: Context, moment_id: str, format: str = "gif", captions: str = "subtitles",
                             look: str = "classic", caption_text: str = "", shape: str = "original",
                             size: int = 0, lines_before: int = 0, lines_after: int = 0, snap: bool = True,
                             shots: bool = True, whole_shot: bool = False,
                             start_ms: int = 0, end_ms: int = 0, caption_size: str = "m",
                             caption_position: str = "", hide_lines: list[int] | None = None,
                             line_text: dict[str, str] | None = None) -> CallToolResult:
    """Render a moment as a reaction GIF (default), an MP4/WebM clip, or a captioned still.

    moment_id: from saytheline_search ("51:99").
    format: "gif" (silent, small; default), "mp4" or "webm" (with sound), "still" (one JPEG frame).
    captions: "subtitles" (default), "none", or "speech" (transcribe what is said instead).
    look: "classic" (plain subtitles), "tiktok", "popin", "bigword" (word-by-word, timed to speech), "poster".
    caption_text: your own caption instead of the line (e.g. a punchline); timed to the speech.
    shape: "original", "16:9", "9:16" (vertical), "1:1" or "4:3" (cropped to fill).
    size: short side in px (0 = 480 for GIFs, 720 otherwise).
    lines_before / lines_after: also include up to 4 neighbouring lines.
    snap: move the cut onto the spoken words (default true; subtitles are often seconds off).
    shots: start on the line's own camera shot and end before the next one (default true). Only the
      padding is trimmed, never the words.
    whole_shot: use the entire shot(s) the line is in, however long (up to ~12 s each side).
    start_ms / end_ms: extra time to add (positive) or trim (negative) at each end, after all that.
    caption_size: "s", "m", "l" or "xl". caption_position: "bottom", "middle" or "top".
    hide_lines: subtitle line numbers not to show (the number after the colon in a moment id, also
      listed in the result). A neighbouring line cut off by the clip's edge and on screen under
      0.5 s is hidden anyway.
    line_text: {line number: text} to reword a line as burned in (e.g. it differs from what is said).
    Takes a few seconds (longer the first time a show's audio is checked). Returns the clip id for
    saytheline_refine_clip, the file path to send, a link, and an image of the result to check."""
    fmt = format.lower()
    if fmt not in FORMATS:
        raise ToolError(f"format must be one of {', '.join(FORMATS)}")
    if look not in LOOKS:
        raise ToolError(f"look must be one of {', '.join(LOOKS)}")
    if shape not in SHAPES:
        raise ToolError(f"shape must be one of {', '.join(SHAPES)}")
    if captions not in ("subtitles", "none", "speech"):
        raise ToolError('captions must be "subtitles", "none" or "speech"')
    m = await _moment(moment_id)
    lines = sorted(m["lines"], key=lambda c: c["idx"])
    first = next(c for c in lines if c["idx"] >= m["idx"] - max(0, min(lines_before, 4)))
    last = [c for c in lines if c["idx"] <= m["idx"] + max(0, min(lines_after, 4))][-1]
    off, notes = m["offset"], []
    if abs(off) >= 250:
        notes.append(f"this episode's subtitles run {abs(off) / 1000:.1f} s {'late' if off < 0 else 'early'} (corrected)")
    in_ms, out_ms = first["start_ms"] + off, last["end_ms"] + off
    keep = (in_ms, out_ms)                          # what must stay in: the line, then the heard words
    if snap:
        await ctx.report_progress(0, None, "listening for the line")
        job = await run_job(ctx, "/api/snap", dict(item=m["item_id"], in_ms=in_ms, out_ms=out_ms, offset_ms=off), "snap")
        if job.get("found"):
            if abs(job.get("offset_ms", 0)) >= 250:
                off += job["offset_ms"]
                notes.append(f"subtitles were {abs(off) / 1000:.1f} s {'late' if off < 0 else 'early'}; snapped to the speech")
            in_ms, out_ms = job["in_ms"], job["out_ms"]
            keep = (job["first_s"], job["last_e"])
        else:
            notes.append("couldn't hear the line, so subtitle timing was used")
            in_ms, out_ms = in_ms - 150, out_ms + 350
    else:
        in_ms, out_ms = in_ms - 150, out_ms + 350
    if fmt == "gif":
        out_ms += 300                               # a beat after the line: reactions land better
    if not whole_shot:                              # no half sentence from the lines either side
        others = [dict(start_ms=c["start_ms"] + off, end_ms=c["end_ms"] + off)
                  for c in lines if not first["idx"] <= c["idx"] <= last["idx"]]
        in_ms, out_ms = shotcuts.fit_lines(in_ms, out_ms, keep[0], keep[1], others)
    if shots or whole_shot:
        in_ms, out_ms, note = await _fit_shots(m["item_id"], in_ms, out_ms, keep, whole_shot)
        if note:
            notes.append(note)
    in_ms, out_ms = max(0, in_ms - start_ms), out_ms + end_ms
    if out_ms - in_ms < 300:
        raise ToolError("that trim leaves less than 0.3 s: use smaller start_ms / end_ms")
    if out_ms - in_ms > 180_000:
        raise ToolError("clips are limited to 3 minutes: use fewer lines_before / lines_after")
    req = dict(item=m["item_id"], in_ms=int(in_ms), out_ms=int(out_ms), offset_ms=int(off), format=fmt if fmt != "still" else "mp4",
               aspect=shape, fit="crop", res=int(size) or (480 if fmt in ("gif", "still") else 720), audio="original",
               subs=dict(font="Inter Black" if look in WORD_LOOKS else "Inter", size=caption_size, colour="white",
                         hl="yellow", caps=False, box=False, position=caption_position or ("middle" if look == "bigword" else "bottom")),
               moment=dict(idx=m["idx"], start_ms=m["line"]["start_ms"], end_ms=m["line"]["end_ms"], text=m["line"]["text"]),
               keep=dict(start_ms=int(keep[0]), end_ms=int(keep[1])), made_by="mcp")
    req["mcp"] = dict(captions=captions, look=look, caption_text=caption_text)
    await _captions_for(ctx, req, captions, look, caption_text)
    lines = await _lines_on_screen(req)
    _edit_lines(req, lines, hide_lines or (), (), line_text)
    if note := _hide_flashes(req, lines):
        notes.append(note)
    req["format"] = fmt
    if fmt == "still":                              # grab the frame mid-caption, so the text is on it
        cs = req.get("caption_cues")
        line = m["line"]
        req["at_ms"] = (cs[0]["start_ms"] + cs[0]["end_ms"]) // 2 if cs else (line["start_ms"] + line["end_ms"]) // 2 + off
    job = await _render(ctx, req)
    return await _clip_payload(job, req, notes)


@mcp.tool(title="Refine a clip", annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                                           idempotent_hint=False, open_world_hint=False))
async def saytheline_refine_clip(ctx: Context, clip_id: str, start_ms: int = 0, end_ms: int = 0, format: str = "",
                               captions: str = "", look: str = "", caption_text: str = "", caption_shift_ms: int = 0,
                               caption_size: str = "", caption_position: str = "", shape: str = "", size: int = 0,
                               whole_shot: bool = False, still_at_ms: int = -1, hide_lines: list[int] | None = None,
                               show_lines: list[int] | None = None, line_text: dict[str, str] | None = None) -> CallToolResult:
    """Make a new version of a clip from feedback; the original is kept. Works on clips made in the
    web app too. Only pass what should change.

    start_ms / end_ms: move the start earlier (+) or later (-) and the end later (+) or earlier (-),
      e.g. "start half a second earlier" = start_ms=500; "cut the last word" ~ end_ms=-400.
    format, captions ("subtitles" | "none" | "speech"), look, caption_text, shape, size: as in saytheline_make_clip.
    caption_shift_ms: captions appear too early -> positive (e.g. 200); too late -> negative.
    caption_size ("s"-"xl"), caption_position ("bottom" | "middle" | "top").
    whole_shot: widen (or narrow) the cut to the entire shot(s) the line is in; start_ms / end_ms
      then apply on top.
    still_at_ms: for a still, which moment of the clip to grab (ms from its start).
    hide_lines / show_lines: subtitle line numbers (as listed in the result) to hide or bring back,
      e.g. the end of the previous line showing at the start. line_text: {line number: new text}."""
    old = await api("GET", f"/api/clip/{clip_id.strip()}")
    req = dict(old.get("request") or {})
    if not req or old.get("supercut"):
        raise ToolError("that clip can't be refined here (no recipe, or it is a supercut): make a new one")
    changes = []
    if whole_shot:
        k = req.get("keep") or {}
        mo, off = req.get("moment") or {}, int(req.get("offset_ms") or 0)
        keep = (k["start_ms"], k["end_ms"]) if k else (int(mo.get("start_ms", req["in_ms"])) + off, int(mo.get("end_ms", req["out_ms"])) + off)
        req["in_ms"], req["out_ms"], note = await _fit_shots(int(req["item"]), int(req["in_ms"]), int(req["out_ms"]), keep, True)
        changes.append(note or "whole shot")
    if start_ms or end_ms:
        req["in_ms"] = max(0, int(req["in_ms"]) - int(start_ms))
        req["out_ms"] = int(req["out_ms"]) + int(end_ms)
        if req["out_ms"] - req["in_ms"] < 300:
            raise ToolError("that leaves less than 0.3 s: use smaller start_ms / end_ms")
        changes.append(f"cut {_fmt(req['in_ms'])}-{_fmt(req['out_ms'])} ({(req['out_ms'] - req['in_ms']) / 1000:.1f} s)")
    st = dict(req.get("subs") or {})
    req["subs"] = st
    # what the clip was made with: recorded by this server, or read from a web-app render
    cur = req.get("mcp") or dict(captions="none" if not st.get("on") else
                                 "speech" if req.get("caption_source") == "speech" else "subtitles",
                                 look=st.get("style") if st.get("style") in LOOKS else "classic", caption_text="")
    if look and look not in LOOKS:
        raise ToolError(f"look must be one of {', '.join(LOOKS)}")
    if captions and captions not in ("subtitles", "none", "speech"):
        raise ToolError('captions must be "subtitles", "none" or "speech"')
    new = dict(captions=captions or cur["captions"], look=look or cur["look"],
               caption_text=caption_text if caption_text else cur.get("caption_text", ""))
    if caption_size:
        st["size"] = caption_size
    if caption_position:
        st["position"] = caption_position
    if look:
        st["font"] = "Inter Black" if look in WORD_LOOKS else "Inter"
        if look == "bigword" and not caption_position:
            st["position"] = "middle"
    fmt = (format or old["file"].rsplit(".", 1)[-1]).lower().replace("jpg", "still")
    if fmt not in FORMATS:
        raise ToolError(f"format must be one of {', '.join(FORMATS)}")
    if shape:
        if shape not in SHAPES:
            raise ToolError(f"shape must be one of {', '.join(SHAPES)}")
        req["aspect"] = shape
    if size:
        req["res"] = int(size)
    for k, v in (("format", format), ("captions", captions), ("look", look), ("caption text", caption_text),
                 ("shape", shape), ("size", size), ("caption size", caption_size), ("caption position", caption_position)):
        if v:
            changes.append(f"{k} {v}")
    # word timing only has to be redone when the cut or the caption choice changed
    if start_ms or end_ms or whole_shot or captions or look or caption_text or not req.get("caption_cues") and new["captions"] != "none" \
            and (new["look"] in WORD_LOOKS or new["captions"] == "speech" or new["caption_text"]):
        await _captions_for(ctx, req, new["captions"], new["look"], new["caption_text"])
    req["mcp"] = new
    if caption_shift_ms:
        if req.get("caption_cues"):
            req["caption_cues"] = [dict(c, start_ms=c["start_ms"] + caption_shift_ms, end_ms=c["end_ms"] + caption_shift_ms,
                                        words=[dict(w, s=w["s"] + caption_shift_ms, e=w["e"] + caption_shift_ms)
                                               for w in c.get("words") or []]) for c in req["caption_cues"]]
        else:                                        # subtitle lines: shift them through the offset
            req["offset_ms"] = int(req.get("offset_ms") or 0) + caption_shift_ms
        changes.append(f"captions {'later' if caption_shift_ms > 0 else 'earlier'} by {abs(caption_shift_ms)} ms")
    lines = await _lines_on_screen(req)
    _edit_lines(req, lines, hide_lines or (), show_lines or (), line_text)
    kept = (set(cur.get("shown_lines") or ()) | set(show_lines or ())) - set(hide_lines or ())
    new["shown_lines"] = sorted(kept)
    for k, v in (("hid lines", hide_lines), ("showed lines", show_lines), ("reworded lines", line_text)):
        if v:
            changes.append(f"{k} {', '.join(map(str, v))}")
    if start_ms or end_ms or whole_shot or caption_shift_ms or captions or look:
        if note := _hide_flashes(req, lines, kept):
            changes.append(note)
    req["format"] = fmt
    req["parent"] = clip_id
    if fmt == "still" and still_at_ms >= 0:
        req["at_ms"] = int(req["in_ms"]) + still_at_ms
    elif fmt == "still" and (start_ms or end_ms or not req.get("at_ms")):
        req["at_ms"] = (int(req["in_ms"]) + int(req["out_ms"])) // 2
    job = await _render(ctx, req)
    return await _clip_payload(job, req, [f"v2 of {clip_id}: " + (", ".join(changes) or "re-rendered")])


@mcp.tool(title="Recent clips", annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False))
async def saytheline_recent_clips(limit: int = 10) -> CallToolResult:
    """The most recently rendered clips, GIFs, stills and supercuts (made here or in the web app),
    newest first, with ids for saytheline_refine_clip / saytheline_look and paths to send."""
    clips = (await api("GET", "/api/clips"))[:max(1, min(int(limit), 50))]
    now = time.time()
    rows = [dict(clip_id=c["id"], file=c["file"], url=PUBLIC + c["url"], bytes=c["bytes"],
                 age_min=round((now - c.get("mtime", now)) / 60)) for c in clips]
    text = "\n".join(f"[{r['clip_id']}] {r['file']} ({r['age_min']} min ago)" for r in rows) or "No clips rendered yet."
    return _result(text, dict(clips=rows))


@mcp.tool(title="Save to library", annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                                             idempotent_hint=True, open_world_hint=False))
async def saytheline_save(clip_id: str, tags: list[str] | None = None) -> CallToolResult:
    """Heart a rendered clip into the user's Library, with optional tags (e.g. ["reactions", "bday video"])."""
    c = await api("GET", f"/api/clip/{clip_id.strip()}")
    req, item = c.get("request") or {}, c.get("item") or {}
    mo = req.get("moment") or {}
    e = await api("POST", "/api/library", dict(
        kind="clip", file=c["file"], item_id=item.get("id"), media_kind=item.get("kind"), label=_label(item) if item else c["file"],
        title=item.get("title") if item.get("kind") == "episode" else None, text=mo.get("text"), idx=mo.get("idx"),
        start_ms=mo.get("start_ms", req.get("in_ms")), end_ms=mo.get("end_ms", req.get("out_ms")),
        in_ms=req.get("in_ms"), out_ms=req.get("out_ms"), offset_ms=req.get("offset_ms"), tags=tags or []))
    return _result(f"Saved {clip_id} to the Library" + (f" with tags {', '.join(e.get('tags') or [])}" if e.get("tags") else "") + ".",
                   dict(library_id=e.get("id"), tags=e.get("tags") or []))


@mcp.tool(title="Make a supercut", annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                                             idempotent_hint=False, open_world_hint=False))
async def saytheline_supercut(ctx: Context, query: str, match: str = "phrase", titles: list[str] | None = None,
                            exclude_titles: list[str] | None = None, short_lines_only: bool = False,
                            max_clips: int = 12, cut: str = "phrase", order: str = "best", shape: str = "16:9",
                            captions: str = "none", labels: bool = True) -> CallToolResult:
    """Join every place a line is said into one MP4 ("every 'I'm not here to make friends'").

    Hits are checked against the speech (lines never heard are skipped), cut to just the phrase by
    default, levelled for loudness and labelled with the show. max_clips 2-40 (default 12).
    order: "best" (closest matches), "air" (by air date / title order) or "shuffle".
    captions: "none" (default), "classic", "tiktok", "popin" or "bigword". cut: "phrase" or "line".
    Slow: about 5-10 s per clip to check, then a few seconds to render; progress is reported."""
    mode = "any" if match == "any" else "phrase"
    p = [("q", query), ("mode", mode), ("limit", 300)]
    p += [("only", k) for k in await _title_keys(query, mode, titles)]
    p += [("ex", k) for k in await _title_keys(query, mode, exclude_titles)]
    if short_lines_only:
        p.append(("short", "1"))
    hits = (await api("GET", "/api/search?" + urllib.parse.urlencode(p)))["hits"]
    seen, picked = set(), []
    for h in hits:                                   # drop repeats of the same line in one item
        k = (h["item_id"], re.sub(r"\W+", " ", h["text"].lower()).strip())
        if k not in seen:
            seen.add(k)
            picked.append(h)
    if order == "shuffle":
        import random
        random.shuffle(picked)
    elif order == "air":
        picked.sort(key=lambda h: (h.get("series") or h.get("title") or "", h.get("season") or 0, h.get("episode") or 0, h["start_ms"]))
    picked = picked[:max(2, min(int(max_clips), 40))]
    if len(picked) < 2:
        raise ToolError(f'only {len(picked)} usable hit(s) for "{query}": a supercut needs at least 2')
    look = captions if captions in LOOKS[:4] else "off"
    body = dict(query=query, hits=[dict(item=h["item_id"], start_ms=h["start_ms"], end_ms=h["end_ms"]) for h in picked],
                check=True, cut="phrase" if cut == "phrase" else "line", padding="normal", unheard="skip",
                transition="cut", loudnorm=True, label=labels, aspect=shape if shape in SHAPES[1:] else "16:9", res=720, fit="crop",
                subs=dict(on=look != "off", style=look if look != "off" else "classic", font="Inter Black" if look in WORD_LOOKS else "Inter",
                          size="m", colour="white", box=False, position="middle" if look == "bigword" else "bottom", hl="yellow", caps=False))
    job = await run_job(ctx, "/api/supercut", body, "supercut")
    skipped = [r for r in job.get("report") or [] if str(r.get("status", "")).startswith(("skipped", "left out"))]
    notes = [f"{job.get('segments')} clips"] + ([f"{len(skipped)} skipped (not heard or over the limit)"] if skipped else [])
    return await _clip_payload(job, dict(in_ms=None, out_ms=None), notes)


# ---------------------------------------------------------------- prompts
@mcp.prompt(title="Reply with a reaction GIF")
def reaction_gif(feeling: str) -> str:
    """Pick and make a reaction GIF from the library that fits a feeling or a situation."""
    return (f"Make a reaction GIF from my library for this: {feeling}\n\n"
            "Think of 4-6 short, well-known lines that would say it (quotes, one-word reactions, catchphrases). "
            "saytheline_search each with short_lines_only=true until you have a few good candidates, "
            "saytheline_look at the best ones together, pick the one whose face and line fit best, "
            "saytheline_make_clip it as a GIF, check the image, then send me the file with one line saying where it's from.")


@mcp.prompt(title="Make a clip from a description")
def clip_from_description(description: str) -> str:
    """Find a scene from a description of what's said and make a clip of it."""
    return (f"Find this in my library and make a clip of it: {description}\n\n"
            "Search for the likely words (try a few phrasings), confirm the right scene with saytheline_look if unsure, "
            "make it with saytheline_make_clip (mp4 if it needs sound, gif otherwise), check the image, and send me the file. "
            "Then ask if I want any changes; use saytheline_refine_clip for them.")


def main():
    mcp.run("stdio")


if __name__ == "__main__":
    main()
