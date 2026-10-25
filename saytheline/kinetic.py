"""'Kinetic' captions: Poster's expressive layout, set in motion.

Each card (one short sentence or clause, sized word by word like Poster) is set as a justified
stack: every line scaled to the same width. On every other card that can take it, one short big
word is turned 90 degrees and sized to run exactly the height of the stack beside it (a lockup).
Words arrive as they are spoken, each by what it does:
  rise   ordinary words slide up from behind their line's baseline (masked)
  slam   the loudest-looking words (tier 4) land oversized and settle, with a slight overshoot
  slide  the vertical word glides along its column, out from behind its end
Only a card's first word (set on the left, reading up) or last word (on the right, reading down)
turns vertical, so the card still reads left to right in the order it is spoken.
While a card is held it grows slowly (a push-in); when the next one comes, its words fall away
one after another.

word_state() is the whole animation: a pure function of time. The render samples it every frame
into ASS events (ass()); the editor's preview runs the same function in JS (= kineticState).
Change both together."""
from PIL import ImageFont

from . import media, poster

FPS = 30
ENTER_MS = {"rise": 260, "slam": 320, "slide": 420}
EXIT_MS, EXIT_STAGGER_MS = 300, 22
PUSH = 0.05                   # a held card grows this much
JUSTIFY_MAX = 1.45            # a line is scaled up to the stack's width at most this much
LOCKUP_LETTERS = (3, 7)       # a vertical word is a short word (shorter = chunkier)
FALL = 0.6                    # how far a leaving word falls, as a fraction of the frame height


def _cap(family):
    """Cap height per pixel of em."""
    f = ImageFont.truetype(media.font_path(family), 1000)
    box = f.getbbox("H")
    return (box[3] - box[1]) / 1000


def _width(line):
    return sum(poster.measure(x["w"], x["font"], x["size"]) for x in line) + \
        sum(poster.measure(" ", x["font"], x["size"]) for x in line[1:])


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


def layout(words, w, h, st):
    """Cards for the clip, in output pixels: [{"start_ms", "end_ms", "cx", "cy", "fall",
    "words": [{"w", "s", "e", "k", "tier", "font", "size", "lh", "cx", "cy", "rot", "enter",
    "clip"}]}]. A word's cx / cy is the centre of its text box; rot is 90 for the lockup word."""
    fw, fh = media.fill(st.get("size"))
    max_w, max_h = w * fw, h * min(0.9, fh * 1.1)
    pos = st.get("position") or "middle"
    cards, lockups = [], 0
    for p in poster.layout(words, w, h, st):
        lines = [[dict(x) for x in ln["words"]] for ln in p["lines"]]
        flat = [x for ln in lines for x in ln]
        for k, x in enumerate(flat):
            x["k"] = k
        # the lockup: on every other card that can take it, its first or last word turns vertical
        vert = side = None
        ends = [(x, sd) for x, sd in ((flat[0], "left"), (flat[-1], "right")) if x["tier"] >= 2
                and x["w"][-1:] not in ",;:" and LOCKUP_LETTERS[0] <= len(_bare(x["w"])) <= LOCKUP_LETTERS[1]]
        if len(flat) >= 4 and ends:
            lockups += 1
            if lockups % 2:
                vert, side = max(ends, key=lambda e: (e[0]["tier"], -len(e[0]["w"])))
                rest = [x for x in flat if x is not vert]
                lines = _balance(rest, 3 if len(rest) >= 7 else 2)
        # justify: each line scaled to the widest one's width
        target = max(_width(ln) for ln in lines)
        for ln in lines:
            k = target / max(1, _width(ln))
            if k <= (JUSTIFY_MAX + 0.3 if vert else JUSTIFY_MAX):
                for x in ln:
                    x["size"] *= k
        # stack the lines cap to baseline, a little apart (more after a line with descenders)
        base, y = [], 0.0
        for i, ln in enumerate(lines):
            cap = max(_cap(x["font"]) * x["size"] for x in ln)
            if i:
                prev = max(x["size"] for x in lines[i - 1])
                y += prev * (0.24 if any(c in "gjpqy," for x in lines[i - 1] for c in x["w"]) else 0.14)
            y += cap
            base.append(y)
        block_h, block_w = base[-1], target
        col = gap = 0.0
        if vert:
            adv = poster.measure(vert["w"], vert["font"], 1000) / 1000
            vert["size"] = block_h / adv                        # its length = the stack's height
            cap_v, desc_v = _cap(vert["font"]), media.font_box(vert["font"])[1] * 0.6
            if (cap_v + desc_v) * vert["size"] > block_w * 0.42:  # too fat for the stack: shorter, centred
                vert["size"] = block_w * 0.42 / (cap_v + desc_v)
            col = (cap_v + desc_v) * vert["size"]
            gap = 0.16 * sum(x["size"] for x in flat if x is not vert) / max(1, len(flat) - 1)
        total_w = col + gap + block_w
        f = min(1.0, max_w / total_w, max_h / block_h)          # the lockup widens the card: fit it
        top = media.block_top(pos, block_h * f, h)
        x0 = (w - total_w * f) / 2
        out = []
        xb = x0 + ((col + gap) * f if side == "left" else 0)
        for ln, b in zip(lines, base):
            lw = _width(ln) * f
            x = xb + (block_w * f - lw) / 2
            by = top + b * f
            desc = max(media.font_box(y["font"])[1] * y["size"] for y in ln) * f
            asc = max(media.font_box(y["font"])[0] * y["size"] for y in ln) * f
            clip = [round(xb - 0.1 * block_w * f), round(by - asc * 1.1), round(xb + block_w * f * 1.1), round(by + desc)]
            for i, wd in enumerate(ln):
                size = wd["size"] * f
                a, d = media.font_box(wd["font"])
                adv = poster.measure(wd["w"], wd["font"], size)
                out.append(dict(w=wd["w"], s=wd["s"], e=wd["e"], k=wd["k"], tier=wd["tier"], font=wd["font"],
                                size=round(size, 1), lh=round(a + d, 3), cx=round(x + adv / 2, 1),
                                cy=round(by - (a - d) / 2 * size, 1), rot=0,
                                enter="slam" if wd["tier"] == 4 else "rise", clip=clip))
                x += adv + poster.measure(" ", wd["font"], size)
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
                            size=round(size, 1), lh=round(a + d, 3), cx=round(cx, 1), cy=round(mid, 1), rot=rot,
                            enter="slide", clip=[round(left - size * 0.15), round(mid - length / 2 - size * 0.1),
                                                 round(left + col * f + size * 0.15), round(mid + length / 2 + size * 0.1)]))
        out.sort(key=lambda x: x["k"])
        cards.append(dict(start_ms=p["start_ms"], end_ms=p["end_ms"], cx=round(w / 2, 1),
                          cy=round(top + block_h * f / 2, 1), fall=round(h * FALL), words=out))
    return cards


def _ease_out_cubic(x):
    return 1 - (1 - x) ** 3


def _ease_out_back(x, s=1.9):
    return 1 + (s + 1) * (x - 1) ** 3 + s * (x - 1) ** 2


def _clamp(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def word_state(card, x, t):
    """Word x of a card at time t (ms): {"cx", "cy", "scale", "rot", "alpha", "clip"} or None while
    it isn't on screen (= JS kineticState)."""
    exit_at = card["end_ms"] + EXIT_STAGGER_MS * x["k"]
    if t < x["s"] or t >= exit_at + EXIT_MS:
        return None
    k = 1 + PUSH * _clamp((t - card["start_ms"]) / max(1, card["end_ms"] - card["start_ms"]))
    grow = lambda px, c: c + (px - c) * k                       # noqa: E731  (about the card's centre)
    cx, cy = grow(x["cx"], card["cx"]), grow(x["cy"], card["cy"])
    mask = [grow(x["clip"][0], card["cx"]), grow(x["clip"][1], card["cy"]),
            grow(x["clip"][2], card["cx"]), grow(x["clip"][3], card["cy"])]
    scale, rot, alpha, clip, dx, dy = k, x["rot"], 1.0, None, 0.0, 0.0
    p = (t - x["s"]) / ENTER_MS[x["enter"]]
    if p < 1:
        if x["enter"] == "rise":            # from just below its line's mask
            dy = (1 - _ease_out_cubic(p)) * (mask[3] - cy + x["size"] * k * x["lh"] / 2)
            clip = mask
        elif x["enter"] == "slam":          # lands big, overshoots a little, settles
            scale *= 1.5 - 0.5 * _ease_out_back(p)
            alpha = _clamp(p * 4)
        else:                               # slide: along its column, out from behind its end
            dy = (1 - _ease_out_cubic(p)) * (mask[3] - mask[1]) * (1 if x["rot"] > 0 else -1)
            clip = mask
    q = (t - exit_at) / EXIT_MS
    if q > 0:                               # falls away, tipping, fading as it goes
        dy += q * q * card["fall"]
        rot += (1 if x["k"] % 2 else -1) * (8 + 5 * (x["k"] % 3)) * q
        alpha *= 1 - _clamp((q - 0.2) / 0.8)
        clip = None
    return dict(cx=cx + dx, cy=cy + dy, scale=scale, rot=rot, alpha=alpha, clip=clip)


def _t(ms):
    cs = max(0, round(ms / 10))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass(cards, in_ms, out_ms, w, h, st, path):
    """ASS for layout(): one event per word per frame while it moves (every word moves a little
    while its card is held). Returns False when there is nothing to show."""
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
    step = 1000 / FPS
    n = 0
    for card in cards:
        for x in card["words"]:
            col = hl if x["tier"] == 4 else soft if x["tier"] <= 1 else base
            fixed = (f"\\fn{x['font']}\\fs{media.ass_size(x['font'], x['size'])}\\b{0 if 'Black' in x['font'] else 1}"
                     f"\\c&H{col}&\\bord{max(2, round(x['size'] * 0.075))}\\shad{max(1, round(x['size'] * 0.04))}")
            a = max(x["s"], in_ms)
            b = min(card["end_ms"] + EXIT_STAGGER_MS * x["k"] + EXIT_MS, out_ms)
            i = 0
            while a + i * step < b:
                t0, t1 = a + i * step, min(b, a + (i + 1) * step)
                i += 1
                s = word_state(card, x, t0)
                if not s or s["alpha"] <= 0.01:
                    continue
                tag = (f"\\an5\\pos({s['cx']:.1f},{s['cy']:.1f}){fixed}\\fscx{s['scale'] * 100:.1f}"
                       f"\\fscy{s['scale'] * 100:.1f}\\frz{s['rot']:.2f}\\alpha&H{round((1 - s['alpha']) * 255):02X}&")
                if s["clip"]:
                    tag += "\\clip({},{},{},{})".format(*(round(v) for v in s["clip"]))
                lines.append(f"Dialogue: 1,{_t(t0 - in_ms)},{_t(t1 - in_ms)},K,,0,0,0,,{{{tag}}}{x['w']}")
                n += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return n > 0

