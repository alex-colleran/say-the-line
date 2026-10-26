"""'Poster' captions: automatic expressive typography. Each word's size comes from what it does
in the sentence (pos_worker.py), then the words are set into a few balanced, centred lines,
measured with the real font files so the layout fits, and revealed word by word as spoken.

Layout runs here (system Python has Pillow); only the tagging needs spaCy's venv."""
import functools
import json
import os
import subprocess

from PIL import ImageFont

from . import config, media

STT_PY = config.stt_python()          # the Python with faster-whisper / spaCy
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pos_worker.py")

TIER_SCALE = {1: 0.72, 2: 0.92, 3: 1.25, 4: 1.75}     # x the base size for the chosen S/M/L/XL
LIGHT_FONT = "Inter"                                  # function words: lighter face for contrast
MAX_WORDS, MAX_CHARS, GAP_MS = 9, 48, 650             # one poster = one short sentence or clause
MAX_FILL = 1.6                                        # how far a short poster may be scaled up
HOLD_MS = 400
LEADING = 1.02                                        # tight, poster-like line spacing
# how a word is said, from its "loud" (dB against the clip's typical word): shouts become the
# biggest tier; a word louder than the rest of its own poster at least a big one (against the
# poster, so a line said loudly throughout isn't all big); whispers are flagged, a size smaller
SHOUT_DB, LOUDER_DB, WHISPER_DB = 6.0, 3.0, -11.0
WHISPER_SCALE = 0.85
MAX_SHOUTS = 2              # per poster: a whole line said loudly keeps only its loudest words big
# a full stop after these doesn't end the sentence ("Mr. Smith" stays on one poster)
ABBREV = {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "prof", "vs", "mt", "lt", "sgt", "capt", "col", "gen", "rev"}


def tiers(words):
    if not words:
        return []
    r = subprocess.run([*config.nice(), STT_PY, "-I", WORKER], input=json.dumps({"words": words}),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if r.returncode != 0:
        raise RuntimeError("tagging failed: " + r.stderr.strip()[-300:])
    return json.loads(r.stdout)


def font_file(family):
    return media.font_path(family)


@functools.lru_cache(maxsize=512)
def _font(family, size):
    return ImageFont.truetype(font_file(family), size)


def measure(text, family, size):
    return _font(family, size).getlength(text)


def group(words):
    """Split the word stream into posters, one sentence or clause each: always at a sentence end
    or a pause; at a comma once the poster has a few words; or at the size limits."""
    posters, cur = [], []
    for w in words:
        if cur:
            text = " ".join(x["w"] for x in cur)
            last = cur[-1]["w"][-1:]
            ends = last in ".?!" and not (last == "." and cur[-1]["w"][:-1].lower() in ABBREV)
            if (ends or w["s"] - cur[-1]["e"] > GAP_MS or (last in ",;:" and len(cur) >= 4)
                    or len(cur) >= MAX_WORDS or len(text) + 1 + len(w["w"]) > MAX_CHARS):
                posters.append(cur)
                cur = []
        cur.append(w)
    if cur:
        posters.append(cur)
    return posters


def _lines(ws, max_w):
    """Each line is anchored by at most one big word (tier >= 3) with the small words around it,
    the way a typographic poster stacks "I am the / ONE / who knocks". Width still wins."""
    lines, cur, cur_w, has_big = [], [], 0.0, False
    for x in ws:
        space = measure(" ", x["font"], x["size"])
        add = x["width"] + (space if cur else 0)
        big = x["tier"] >= 3
        if cur and (cur_w + add > max_w or (big and has_big)):
            lines.append(cur)
            cur, cur_w, has_big = [], 0.0, False
            add = x["width"]
        cur.append(x)
        cur_w += add
        has_big = has_big or big
    if cur:
        lines.append(cur)
    # a lone small word on the last line joins the line above
    if len(lines) > 1 and len(lines[-1]) == 1 and lines[-1][0]["tier"] <= 2:
        lines[-2].append(lines[-1].pop())
        lines.pop()
    return lines


def _said(x):
    """The tier and flags for how loudly the word was said (no "loud": as the text alone says)."""
    loud = x.get("loud")
    x = dict(x, tier0=x["tier"])
    if loud is None:
        return x
    if loud >= SHOUT_DB:
        return dict(x, tier=4, shout=True)
    if loud <= WHISPER_DB:
        return dict(x, tier=min(x["tier"], 2), whisper=True)
    return x


def _set(g, unit, heavy, scale, max_w):
    ws = []
    for x in g:
        fam = LIGHT_FONT if x["tier"] <= 1 else heavy
        size = max(10, round(unit * TIER_SCALE[x["tier"]] * scale * (WHISPER_SCALE if x.get("whisper") else 1)))
        ws.append(dict(x, font=fam, size=size, width=measure(x["w"], fam, size)))
    lines = _lines(ws, max_w)
    widths = [sum(y["width"] for y in ln) + sum(measure(" ", y["font"], y["size"]) for y in ln[1:]) for ln in lines]
    heights = [max(y["size"] for y in ln) * LEADING for ln in lines]
    return lines, widths, heights


def layout(words, w, h, st):
    """words: [{"w","s","e"}] (absolute ms). Returns posters with lines and per-word size/font/tier,
    in output pixels: [{"start_ms","end_ms","lines":[{"y","height","words":[...]}]}]."""
    if not words:
        return []
    if st.get("caps"):
        words = [dict(x, w=x["w"].upper()) for x in words]
    t = tiers([x["w"] for x in words])
    words = [dict(x, tier=tt) for x, tt in zip(words, t)]
    words = [_said(x) for x in words]
    # a whisper is a quiet stretch, not one soft word: it needs a quiet neighbour
    for i, x in enumerate(words):
        if x.get("whisper") and not any((words[j].get("loud") is not None and words[j]["loud"] <= WHISPER_DB + 3)
                                        for j in (i - 1, i + 1) if 0 <= j < len(words)):
            words[i] = dict(x, whisper=False, tier=x["tier0"])
    heavy = st.get("font") or "Inter Black"
    unit = min(w, h) * media.TT_SIZES.get(st.get("size"), 0.095)
    fw, fh = media.fill(st.get("size"))
    max_w, max_h = w * fw, h * fh
    pos = st.get("position") or "middle"
    out = []
    groups = group(words)
    for gi, g in enumerate(groups):
        loudest = {id(x) for x in sorted((x for x in g if x.get("shout")), key=lambda x: -x["loud"])[:MAX_SHOUTS]}
        g = [x if not x.get("shout") or id(x) in loudest else dict(x, shout=False, tier=x["tier0"]) for x in g]
        levels = sorted(x["loud"] for x in g if x.get("loud") is not None)
        if len(levels) >= 3:
            mid = levels[len(levels) // 2]
            g = [dict(x, tier=max(x["tier"], 3)) if x.get("loud") is not None and x["loud"] - mid >= LOUDER_DB
                 and not x.get("shout") else x for x in g]
        # every poster needs a focal word: if nothing is tier 3+, promote its last content word
        if not any(x["tier"] >= 3 for x in g):
            cand = [i for i, x in enumerate(g) if x["tier"] == 2] or list(range(len(g)))
            g = [dict(x, tier=3) if i == cand[-1] else x for i, x in enumerate(g)]
        # set at natural size, then scale the whole poster so its widest line fills the frame
        # (short posters grow up to MAX_FILL; long ones shrink until they fit the height too)
        lines, widths, heights = _set(g, unit, heavy, 1.0, max_w)
        scale = min(MAX_FILL, max_w / max(widths), max_h / sum(heights))
        for _ in range(6):
            lines, widths, heights = _set(g, unit, heavy, scale, max_w)
            if max(widths) <= max_w and sum(heights) <= max_h:
                break
            scale *= 0.92
        total = sum(heights)
        top = media.block_top(pos, total, h)
        rows, y = [], top
        for ln, lh in zip(lines, heights):
            rows.append(dict(y=round(y), height=round(lh),
                             words=[dict(w=x["w"], s=x["s"], e=x["e"], tier=x["tier"], size=x["size"],
                                         font=x["font"], **{k: True for k in ("shout", "whisper") if x.get(k)})
                                    for x in ln]))
            y += lh
        end = g[-1]["e"] + HOLD_MS
        if gi + 1 < len(groups):
            end = min(end, groups[gi + 1][0]["s"])
        out.append(dict(start_ms=g[0]["s"], end_ms=end, lines=rows))
    return out
