"""'Kinetic' captions: Poster's expressive layout, set in motion.

Each card (one short sentence or clause, sized word by word like Poster, including how loudly each
word was said) is set as a justified stack: every line scaled to the same width, and a line too
short for that stretched sideways to fill it. On every other card that can take it, its first word
(on the left, reading up) or last word (on the right, reading down) turns 90 degrees and runs the
height of the stack: a lockup, which still reads in spoken order.

Words arrive as they are spoken, each by what it is and how it was said (layout picks; see _enter):
  slam   the biggest words land oversized and settle; a shouted one shakes as it lands
  drop   big words fall in from above and bounce
  wipe   long words are revealed left to right
  type   a word after a pause types itself out letter by letter
  fade   a whisper fades up, drawn in outline only
  rise   the rest slide up from behind their line's baseline (masked)
  slide  the vertical word glides along its column
The card's key word may sit on a highlight bar that wipes in behind it. While a card is held it
grows slowly (a push-in); when the next arrives it leaves by how its sentence ends: "!" scatters,
"?" pushes up and away, anything else falls.

word_state() and bar_state() are the whole animation: pure functions of time. The render samples
them every frame into ASS events (ass()); the editor's preview runs the same functions in JS
(= kineticState / kineticBarState). Change both together."""
import math

from PIL import ImageFont

from . import media, poster

FPS = 30
ENTER_MS = {"rise": 260, "slam": 320, "slide": 420, "drop": 440, "wipe": 320, "fade": 420}
TYPE_MS_PER_LETTER, TYPE_MS = 45, (160, 480)
EXIT_MS, EXIT_STAGGER_MS = 300, 22
BAR_DELAY_MS, BAR_MS = 60, 220
PUSH = 0.05                   # a held card grows this much
JUSTIFY_MAX = 1.45            # a line is scaled up to the stack's width at most this much ...
STRETCH_MAX = 1.5             # ... and beyond that stretched sideways, at most this much
LOCKUP_LETTERS = (3, 7)       # a vertical word is a short word (shorter = chunkier)
WIPE_LETTERS = 8              # a word this long is wiped in
TYPE_AFTER_MS = 350           # a word after this long a pause types itself out
FALL, PUSH_UP, SPREAD = 0.6, 0.3, 0.45   # exit distances: fractions of the frame height / width
DARK = "1A1A1A"               # text on the highlight bar


def _cap(family):
    """Cap height per pixel of em."""
    f = ImageFont.truetype(media.font_path(family), 1000)
    box = f.getbbox("H")
    return (box[3] - box[1]) / 1000


def _adv(x):
    return poster.measure(x["w"], x["font"], x["size"]) * x.get("sx", 1)


def _width(line):
    return sum(_adv(x) for x in line) + sum(poster.measure(" ", x["font"], x["size"]) * x.get("sx", 1) for x in line[1:])


def _balance(words, n):
    """The words re-flowed into n lines of about equal width: a lockup needs a solid stack beside
    it, not Poster's one-big-word-per-line breaks."""
    if n <= 1 or len(words) < n:
        return [words]
    if n == 2:
        cut = min(range(1, len(words)), key=lambda i: abs(_width(words[:i]) - _width(words[i:])))
        return [words[:cut], words[cut:]]
    best = None
    for i in range(1, len(words) - 1):
        for j in range(i + 1, len(words)):
            ws = [_width(words[:i]), _width(words[i:j]), _width(words[j:])]
            if best is None or max(ws) - min(ws) < best[0]:
                best = (max(ws) - min(ws), [words[:i], words[i:j], words[j:]])
    return best[1]


def _bare(w):
    return w.strip(".,!?;:'\"-()")


def _enter(x, prev, k):
    """How a (horizontal) word arrives: by its tier and how it was said."""
    if x.get("whisper"):
        return "fade"
    if x["tier"] == 4 or x.get("shout"):
        return "slam"
    if len(_bare(x["w"])) >= WIPE_LETTERS:
        return "wipe"
    if prev is not None and x["s"] - prev["e"] >= TYPE_AFTER_MS and len(_bare(x["w"])) >= 4:
        return "type"
    if x["tier"] == 3:
        return "drop" if k % 2 else "rise"
    return "rise"


def layout(words, w, h, st):
    """Cards for the clip, in output pixels: [{"start_ms", "end_ms", "cx", "cy", "exit", "fall",
    "push", "spread", "words": [...], "bars": [...]}]. A word: {"w", "s", "e", "k", "tier", "font",
    "size", "lh", "adv", "sx", "cx", "cy", "rot", "enter", "dur", "clip", "letters", "whisper",
    "onbar"}; cx / cy is the centre of its text box, rot 90 / -90 for a lockup word. A bar:
    {"k", "s", "cx", "cy", "bw", "bh"}, timed and moved with word k."""
    max_w, max_h = w * 0.86, h * 0.6
    pos = st.get("position") or "middle"
    cards, lockups, barred = [], 0, 0
    for p in poster.layout(words, w, h, st):
        lines = [[dict(x) for x in ln["words"]] for ln in p["lines"]]
        flat = [x for ln in lines for x in ln]
        for k, x in enumerate(flat):
            x["k"] = k
        # the lockup: on every other card that can take it, its first or last word turns vertical
        vert = side = None
        # (a word of thin letters, "I'll", reads as stripes turned on its side)
        ends = [(x, sd) for x, sd in ((flat[0], "left"), (flat[-1], "right")) if x["tier"] >= 2
                and x["w"][-1:] not in ",;:" and LOCKUP_LETTERS[0] <= len(_bare(x["w"])) <= LOCKUP_LETTERS[1]
                and sum(c not in "iIlj'tf!.,?" for c in x["w"]) >= 2]
        if len(flat) >= 4 and ends:
            lockups += 1
            if lockups % 2:
                vert, side = max(ends, key=lambda e: (e[0]["tier"], -len(e[0]["w"])))
                rest = [x for x in flat if x is not vert]
                lines = _balance(rest, 3 if len(rest) >= 7 else 2)
        # justify: each line scaled to the widest one's width; past the limit, stretched sideways
        target = max(_width(ln) for ln in lines)
        lim = JUSTIFY_MAX + 0.3 if vert else JUSTIFY_MAX
        for ln in lines:
            k = target / max(1, _width(ln))
            grow, stretch = min(k, lim), min(STRETCH_MAX, k / lim) if k > lim else 1.0
            for x in ln:
                x["size"] *= grow
                x["sx"] = stretch
        # stack the lines cap to baseline, a little apart (more after a line with descenders)
        base, y = [], 0.0
        for i, ln in enumerate(lines):
            cap = max(_cap(x["font"]) * x["size"] for x in ln)
            if i:
                prev = max(x["size"] for x in lines[i - 1])
                y += prev * (0.24 if any(c in "gjpqy," for x in lines[i - 1] for c in x["w"]) else 0.14)
            y += cap
            base.append(y)
        block_h, block_w = base[-1], max(_width(ln) for ln in lines)
        col = gap = 0.0
        if vert:
            vert["sx"] = 1.0
            adv = poster.measure(vert["w"], vert["font"], 1000) / 1000
            vert["size"] = block_h / adv                        # its length = the stack's height
            cap_v, desc_v = _cap(vert["font"]), media.font_box(vert["font"])[1] * 0.6
            if (cap_v + desc_v) * vert["size"] > block_w * 0.42:  # too fat for the stack: shorter, centred
                vert["size"] = block_w * 0.42 / (cap_v + desc_v)
            col = (cap_v + desc_v) * vert["size"]
            gap = 0.16 * sum(x["size"] for x in flat if x is not vert) / max(1, len(flat) - 1)
        total_w = col + gap + block_w
        f = min(1.0, max_w / total_w, max_h / block_h)          # the lockup widens the card: fit it
        top = {"top": h * 0.12, "bottom": h * 0.78 - block_h * f}.get(pos, (h - block_h * f) / 2)
        x0 = (w - total_w * f) / 2
        out, bars = [], []
        # the highlight bar: under a shouted key word always, under any other key word on every other card
        key = next((x for x in flat if x is not vert and not x.get("whisper") and (x.get("shout") or x["tier"] == 4)), None)
        if key and not key.get("shout"):
            barred += 1
            if barred % 2 == 0:
                key = None
        xb = x0 + ((col + gap) * f if side == "left" else 0)
        order = sorted(flat, key=lambda x: x["s"])
        prev_of = {id(b): a for a, b in zip(order, order[1:])}
        for ln, b in zip(lines, base):
            lw = _width(ln) * f
            x = xb + (block_w * f - lw) / 2
            by = top + b * f
            desc = max(media.font_box(y["font"])[1] * y["size"] for y in ln) * f
            asc = max(media.font_box(y["font"])[0] * y["size"] for y in ln) * f
            clip = [round(xb - 0.1 * block_w * f), round(by - asc * 1.1), round(xb + block_w * f * 1.1), round(by + desc)]
            for wd in ln:
                size = wd["size"] * f
                a, d = media.font_box(wd["font"])
                adv = poster.measure(wd["w"], wd["font"], size)
                sx = wd.get("sx", 1.0)
                enter = _enter(wd, prev_of.get(id(wd)), wd["k"])
                letters = [round(poster.measure(wd["w"][:i], wd["font"], size), 1) for i in range(1, len(wd["w"]) + 1)]
                dur = (min(TYPE_MS[1], max(TYPE_MS[0], TYPE_MS_PER_LETTER * len(wd["w"])))
                       if enter == "type" else ENTER_MS[enter])
                cx, cy = x + adv * sx / 2, by - (a - d) / 2 * size
                out.append(dict(w=wd["w"], s=wd["s"], e=wd["e"], k=wd["k"], tier=wd["tier"], font=wd["font"],
                                size=round(size, 1), lh=round(a + d, 3), adv=round(adv, 1), sx=round(sx, 3),
                                cx=round(cx, 1), cy=round(cy, 1), rot=0, enter=enter, dur=dur, clip=clip,
                                letters=letters, whisper=bool(wd.get("whisper")), onbar=wd is key,
                                shake=bool(wd.get("shout"))))
                if wd is key:
                    cap_h = _cap(wd["font"]) * size
                    bars.append(dict(k=wd["k"], s=wd["s"], cx=round(cx, 1), cy=round(by - cap_h / 2, 1),
                                     bw=round(adv * sx + 0.3 * size, 1), bh=round(cap_h * 1.5, 1)))
                x += (adv + poster.measure(" ", wd["font"], size)) * sx
        if vert:
            size = vert["size"] * f
            a, d = media.font_box(vert["font"])
            cap_v = _cap(vert["font"]) * size
            # the rotated baseline faces the stack; caps face outwards
            if side == "left":                                  # reads up: rotated 90 degrees anticlockwise
                left = x0
                bx = left + cap_v
                cx, rot = bx - (a - d) / 2 * size, 90
            else:                                               # reads down: 90 clockwise
                left = x0 + (block_w + gap) * f
                bx = left + col * f - cap_v
                cx, rot = bx + (a - d) / 2 * size, -90
            length = poster.measure(vert["w"], vert["font"], size)
            mid = top + block_h * f / 2
            out.append(dict(w=vert["w"], s=vert["s"], e=vert["e"], k=vert["k"], tier=vert["tier"], font=vert["font"],
                            size=round(size, 1), lh=round(a + d, 3), adv=round(length, 1), sx=1.0, cx=round(cx, 1),
                            cy=round(mid, 1), rot=rot, enter="slide", dur=ENTER_MS["slide"], letters=[],
                            whisper=False, onbar=False, shake=False,
                            clip=[round(left - size * 0.15), round(mid - length / 2 - size * 0.1),
                                  round(left + col * f + size * 0.15), round(mid + length / 2 + size * 0.1)]))
        out.sort(key=lambda x: x["k"])
        last = flat[-1]["w"].rstrip("\"')")[-1:]
        cards.append(dict(start_ms=p["start_ms"], end_ms=p["end_ms"], cx=round(w / 2, 1),
                          cy=round(top + block_h * f / 2, 1), exit={"!": "scatter", "?": "push"}.get(last, "fall"),
                          fall=round(h * FALL), push=round(h * PUSH_UP), spread=round(w * SPREAD), words=out, bars=bars))
    return cards


def _ease_out_cubic(x):
    return 1 - (1 - x) ** 3


def _ease_out_back(x, s=1.9):
    return 1 + (s + 1) * (x - 1) ** 3 + s * (x - 1) ** 2


def _ease_out_bounce(x):
    n, d = 7.5625, 2.75
    if x < 1 / d:
        return n * x * x
    if x < 2 / d:
        x -= 1.5 / d
        return n * x * x + 0.75
    if x < 2.5 / d:
        x -= 2.25 / d
        return n * x * x + 0.9375
    x -= 2.625 / d
    return n * x * x + 0.984375


def _clamp(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _held(card, cx, cy, k_word, t):
    """The push-in and the exit, shared by words and bars: (cx, cy, scale, drot, alpha, leaving)."""
    k = 1 + PUSH * _clamp((t - card["start_ms"]) / max(1, card["end_ms"] - card["start_ms"]))
    gx, gy = card["cx"] + (cx - card["cx"]) * k, card["cy"] + (cy - card["cy"]) * k
    q = (t - card["end_ms"] - EXIT_STAGGER_MS * k_word) / EXIT_MS
    scale, rot, alpha = k, 0.0, 1.0
    if q > 0:
        sign = 1 if k_word % 2 else -1
        if card["exit"] == "scatter":       # flies outwards from the card's centre, spinning
            dx, dy = gx - card["cx"], gy - card["cy"]
            n = math.hypot(dx, dy) or 1.0
            if n < 1:
                dx, dy = sign, -0.3
            d = q ** 1.6 * card["spread"]
            gx, gy = gx + dx / n * d, gy + dy / n * d
            rot, scale = sign * 30 * q, scale * (1 + 0.3 * q)
        elif card["exit"] == "push":        # the whole card lifts away
            gy -= q * q * card["push"]
        else:                               # falls, tipping
            gy += q * q * card["fall"]
            rot = sign * (8 + 5 * (k_word % 3)) * q
        alpha = 1 - _clamp((q - 0.2) / 0.8)
    return gx, gy, scale, rot, alpha, q > 0


def word_state(card, x, t):
    """Word x of a card at time t (ms): {"cx", "cy", "scale", "sx", "rot", "alpha", "clip"} or None
    while it isn't on screen (= JS kineticState)."""
    if t < x["s"] or t >= card["end_ms"] + EXIT_STAGGER_MS * x["k"] + EXIT_MS:
        return None
    cx, cy, scale, drot, alpha, leaving = _held(card, x["cx"], x["cy"], x["k"], t)
    k = 1 + PUSH * _clamp((t - card["start_ms"]) / max(1, card["end_ms"] - card["start_ms"]))
    grow = lambda px, c: c + (px - c) * k                       # noqa: E731  (about the card's centre)
    mask = [grow(x["clip"][0], card["cx"]), grow(x["clip"][1], card["cy"]),
            grow(x["clip"][2], card["cx"]), grow(x["clip"][3], card["cy"])]
    rot, clip, dx, dy = x["rot"] + drot, None, 0.0, 0.0
    half_h = x["size"] * k * x["lh"] / 2
    left = cx - x["adv"] * x["sx"] * k / 2
    p = (t - x["s"]) / x["dur"]
    if p < 1 and not leaving:
        e = x["enter"]
        if e == "rise":                     # from just below its line's mask
            dy = (1 - _ease_out_cubic(p)) * (mask[3] - cy + half_h)
            clip = mask
        elif e == "slam":                   # lands big, overshoots a little, settles; a shout shakes
            scale *= 1.5 - 0.5 * _ease_out_back(p)
            alpha *= _clamp(p * 4)
            if x["tier"] == 4 and p > 0.35 and x.get("shake"):
                dx = x["size"] * 0.05 * math.sin((t - x["s"]) / 18) * (1 - p)
        elif e == "drop":                   # falls in from above and bounces
            dy = -(1 - _ease_out_bounce(p)) * x["size"] * 1.6
            alpha *= _clamp(p * 5)
        elif e == "wipe":                   # revealed left to right
            clip = [left - 4, cy - half_h - 4, left + x["adv"] * x["sx"] * k * _ease_out_cubic(p) + 1, cy + half_h + 4]
        elif e == "type":                   # letter by letter
            n = min(len(x["letters"]), int(p * len(x["letters"])) + 1)
            clip = [left - 4, cy - half_h - 4, left + x["letters"][n - 1] * x["sx"] * k + 1, cy + half_h + 4]
        elif e == "fade":                   # a whisper: fades up, drifting a little
            alpha *= _ease_out_cubic(p)
            dy = (1 - _ease_out_cubic(p)) * x["size"] * 0.25
        else:                               # slide: along its column, out from behind its end
            dy = (1 - _ease_out_cubic(p)) * (mask[3] - mask[1]) * (1 if x["rot"] > 0 else -1)
            clip = mask
    return dict(cx=cx + dx, cy=cy + dy, scale=scale, sx=x["sx"], rot=rot, alpha=alpha, clip=clip)


def bar_state(card, bar, t):
    """A highlight bar at time t: it wipes in just after its word arrives, then moves with it
    (= JS kineticBarState). Same shape as word_state's result."""
    start = bar["s"] + BAR_DELAY_MS
    if t < start or t >= card["end_ms"] + EXIT_STAGGER_MS * bar["k"] + EXIT_MS:
        return None
    cx, cy, scale, rot, alpha, leaving = _held(card, bar["cx"], bar["cy"], bar["k"], t)
    p = (t - start) / BAR_MS
    clip = None
    if p < 1 and not leaving:
        left = cx - bar["bw"] * scale / 2
        clip = [left - 2, cy - bar["bh"] * scale / 2 - 2, left + bar["bw"] * scale * _ease_out_cubic(p) + 1,
                cy + bar["bh"] * scale / 2 + 2]
    return dict(cx=cx, cy=cy, scale=scale, sx=1.0, rot=rot, alpha=alpha, clip=clip)


def _t(ms):
    cs = max(0, round(ms / 10))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _frames(a, b):
    step = 1000 / FPS
    i = 0
    while a + i * step < b:
        yield a + i * step, min(b, a + (i + 1) * step)
        i += 1


def _moved(s):
    tag = (f"\\an5\\pos({s['cx']:.1f},{s['cy']:.1f})\\fscx{s['scale'] * s['sx'] * 100:.1f}\\fscy{s['scale'] * 100:.1f}"
           f"\\frz{s['rot']:.2f}\\alpha&H{round((1 - s['alpha']) * 255):02X}&")
    if s["clip"]:
        tag += "\\clip({},{},{},{})".format(*(round(v) for v in s["clip"]))
    return tag


def ass(cards, in_ms, out_ms, w, h, st, path):
    """ASS for layout(): one event per word (and bar) per frame while it moves, which is always
    while its card is held. Returns False when there is nothing to show."""
    hl = media._bgr(media.HIGHLIGHTS.get(st.get("hl"), "FFE14D"))
    base = media._bgr(media.COLOURS.get(st.get("colour"), "FFFFFF") if st.get("colour") != "black" else "FFFFFF")
    soft = media._bgr("D9D9D9") if base == "FFFFFF" else base
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 2",
             "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
             "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
             "MarginL, MarginR, MarginV, Encoding",
             "Style: K,Inter,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,0,0,0,0,100,100,0,0,1,3,2,5,0,0,0,1",
             "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    n = 0
    for card in cards:
        for bar in card["bars"]:
            shape = f"m 0 0 l {bar['bw']:.1f} 0 {bar['bw']:.1f} {bar['bh']:.1f} 0 {bar['bh']:.1f}"
            end = card["end_ms"] + EXIT_STAGGER_MS * bar["k"] + EXIT_MS
            for t0, t1 in _frames(max(bar["s"] + BAR_DELAY_MS, in_ms), min(end, out_ms)):
                s = bar_state(card, bar, t0)
                if s and s["alpha"] > 0.01:
                    lines.append(f"Dialogue: 0,{_t(t0 - in_ms)},{_t(t1 - in_ms)},K,,0,0,0,,"
                                 f"{{{_moved(s)}\\p1\\bord0\\shad0\\c&H{hl}&}}{shape}{{\\p0}}")
                    n += 1
        for x in card["words"]:
            bord = max(2, round(x["size"] * 0.075))
            if x["onbar"]:                  # dark type on the bar
                look = f"\\c&H{DARK}&\\bord0\\shad0"
            elif x["whisper"]:              # outline only
                look = f"\\1a&HFF&\\3c&H{base}&\\bord{max(1, round(x['size'] * 0.045))}\\shad0"
            else:
                col = hl if x["tier"] == 4 else soft if x["tier"] <= 1 else base
                look = f"\\c&H{col}&\\bord{bord}\\shad{max(1, round(x['size'] * 0.04))}"
            fixed = f"\\fn{x['font']}\\fs{media.ass_size(x['font'], x['size'])}\\b{0 if 'Black' in x['font'] else 1}{look}"
            end = card["end_ms"] + EXIT_STAGGER_MS * x["k"] + EXIT_MS
            for t0, t1 in _frames(max(x["s"], in_ms), min(end, out_ms)):
                s = word_state(card, x, t0)
                if s and s["alpha"] > 0.01:
                    tag = _moved(s)
                    if x["whisper"] and not x["onbar"]:   # \alpha sets every layer: keep the fill clear
                        tag += "\\1a&HFF&"
                    lines.append(f"Dialogue: 1,{_t(t0 - in_ms)},{_t(t1 - in_ms)},K,,0,0,0,,{{{fixed}{tag}}}{x['w']}")
                    n += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return n > 0
