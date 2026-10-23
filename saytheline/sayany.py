"""Say Anything: a typed sentence, said by the library ("sentence mixing").

The sentence is split into the longest phrases the subtitles contain, longest first, so "I didn't
do it" comes from one line rather than four. Each phrase's candidate lines are listened to (the
same check a supercut uses) until one is heard saying exactly those words, and the cleanest take
wins: words that stand apart from their neighbours cut without the next syllable bleeding in.

This module is the planning logic only; it is handed `find` (subtitle search) and `listen` (the
speech check) so it can be tested without a library. web.py runs it as a job."""
import array
import math
import re
import subprocess

from . import media, snap

MAX_PHRASE = 6           # words per piece, at most
CANDIDATES = 8           # subtitle lines fetched per phrase
TRIES = 3                # of those, how many are listened to before settling...
MORE_TRIES = 5           # ...or this many while no take has a pause on both sides
OK_GAP_MS = 100
LISTENS_PER_WORD, MIN_LISTENS, MAX_LISTENS = 5, 40, 150   # the listening budget for a sentence:
# each listen is a few seconds of Whisper. Past it, each word still left gets one listen of its own,
# so a long sentence finishes with every word tried instead of the tail marked missing.
GOOD_GAP_MS = 180        # silence on both sides of a take: clean enough to stop looking
# padding around the heard words (ms before, after). Whisper's word ends run early more often
# than its starts, so the tail gets more.
PAD = {"tight": (30, 70), "normal": (70, 140), "loose": (150, 260)}


def words(text):
    """The typed sentence as [(as typed, normalised)], punctuation kept on the typed form.
    Hyphens and slashes split words ("say-the-line" is three words said), as search does."""
    out = []
    for raw in text.split():
        for part in re.split(r"[-‐-―/]+", raw):
            n = snap.norm(part)
            if n:
                out.append((part, n))
    return out


def runs(heard, want):
    """Index ranges where the heard words are exactly `want` (normalised), in order."""
    h = [snap.norm(w["w"]) for w in heard]
    n = len(want)
    return [(i, i + n) for i in range(len(h) - n + 1) if h[i:i + n] == want]


def take_from(facts, want):
    """The phrase as heard inside one checked line, or None. Scored for how cleanly it cuts out."""
    if not facts or not facts.get("found"):
        return None
    ws = [w for w in facts.get("words") or [] if w["e"] > facts["line_s"] - 200 and w["s"] < facts["line_e"] + 200]
    best = None
    for i, j in runs(ws, want):
        s, e = ws[i]["s"], ws[j - 1]["e"]
        before = s - ws[i - 1]["e"] if i > 0 else 1000
        after = ws[j]["s"] - e if j < len(ws) else 1000
        per_word = (e - s) / len(want)
        loud = [w["loud"] for w in ws[i:j] if w.get("loud") is not None]
        score = min(before, 400) + min(after, 400)
        if not 90 <= per_word <= 900:          # bunched or stretched: Whisper lost the timing
            score -= 600
        if loud and min(loud) < -12:           # a mumble or a word under music
            score -= 300
        t = dict(s=s, e=e, before=before, after=after, score=score,
                 words=[dict(w) for w in ws[i:j]])
        if best is None or t["score"] > best["score"]:
            best = t
    return best


def order(hits, want):
    """Lines that are just the phrase first, then ones it starts or ends (a natural pause on one
    side), then the rest; shorter lines first within each."""
    def rank(h):
        t = [x for x in (snap.norm(w) for w in (h.get("text") or "").split()) if x]
        n = len(want)
        return (0 if t == want else 1 if t[-n:] == want or t[:n] == want else 2, len(t))
    return sorted(hits, key=rank)


def clean(t):
    return t["before"] >= OK_GAP_MS and t["after"] >= OK_GAP_MS


def plan(sentence, find, listen, max_phrase=MAX_PHRASE, progress=None):
    """Pieces covering the sentence, in order:
      [{"text", "want", "take": {hit, facts, s, e, score, ...} or None, "alts": [hits not tried]}]
    find(phrase) -> subtitle hits for the exact phrase, best first.
    listen(hit, phrase) -> check facts (snap) for one hit.

    A phrase of several words is kept only when a take has a pause on both sides; otherwise a
    shorter phrase is tried, since a word alone often has a line of its own ("Tonight."). Lines
    already listened to are reused, so trying the shorter phrase costs little."""
    ws = words(sentence)
    pieces, i, listens, heard = [], 0, [0], {}
    budget = min(MAX_LISTENS, max(MIN_LISTENS, LISTENS_PER_WORD * len(ws)))

    def try_hits(hits, want, text, limit):
        best, tried = None, 0
        hits = order(hits, want)
        for k, h in enumerate(hits):
            key = (h.get("item_id"), h.get("start_ms"))
            if key not in heard:
                if tried >= limit:
                    return best, [x for x in hits[k:] if (x.get("item_id"), x.get("start_ms")) not in heard]
                tried += 1
                listens[0] += 1
                if progress:
                    progress(text, listens[0])
                heard[key] = listen(h, text)
            t = take_from(heard[key], want)
            if t and (best is None or t["score"] > best["score"]):
                best = dict(t, hit=h, facts=heard[key])
                if t["before"] >= GOOD_GAP_MS and t["after"] >= GOOD_GAP_MS:
                    return best, [x for x in hits[k + 1:] if (x.get("item_id"), x.get("start_ms")) not in heard]
        return best, []

    while i < len(ws):
        fallback, why, left = None, "none", []
        spent = listens[0] >= budget          # over budget: one word at a time, one listen each
        for n in range(1 if spent else min(max_phrase, len(ws) - i), 0, -1):
            if n > 1 and listens[0] >= budget:
                continue                      # ran out part-way: straight to the single word
            spent = spent or listens[0] >= budget
            text = " ".join(raw for raw, _ in ws[i:i + n])
            want = [w for _, w in ws[i:i + n]]
            hits = find(" ".join(want))
            if not hits:
                continue
            take, rest = try_hits(hits, want, text, 1 if spent else TRIES if n > 1 else MORE_TRIES)
            if not take:
                if n == 1:              # in the subtitles, not heard yet: the rest can still be tried
                    why, left = ("rushed" if spent else "unheard"), rest
                continue
            piece = dict(text=text, want=want, take=take, alts=rest, n=n)
            if clean(take) or n == 1:
                # a single word with no clean take: a longer unclean phrase means fewer joins
                if not clean(take) and fallback:
                    piece = fallback
                break
            if fallback is None or take["score"] > fallback["take"]["score"]:
                fallback = piece
        else:
            piece = fallback
        if piece:
            pieces.append({k: v for k, v in piece.items() if k != "n"})
            i += piece["n"]
        else:
            raw, w = ws[i]
            pieces.append(dict(text=raw, want=[w], take=None, alts=left, why=why))   # none: no subtitle has it
            i += 1
    return pieces


def frames_db(src, a_ms, b_ms):
    """10 ms loudness frames (dB) of the file's dialogue track between two media times."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{a_ms / 1000:.3f}", "-t", f"{(b_ms - a_ms) / 1000:.3f}",
                        "-i", src, "-map", f"0:a:{media.audio_track(src)}", "-vn", "-ac", "1", "-ar", "16000",
                        "-f", "s16le", "-"], capture_output=True, timeout=60)
    pcm = array.array("h", r.stdout[: len(r.stdout) // 2 * 2])
    out = []
    for i in range(0, len(pcm) - 159, 160):
        seg = pcm[i:i + 160]
        out.append(20 * math.log10(math.sqrt(sum(x * x for x in seg) / 160) + 1))
    return out


def _quiet(db, base_ms, lo_ms, hi_ms, target_ms, prefer_late):
    """The time in [lo, hi] to cut at: among the quietest frames there (within 3 dB of the
    minimum), the one nearest the target. A cut in a valley doesn't clip a syllable."""
    a, b = max(0, (lo_ms - base_ms) // 10), min(len(db), (hi_ms - base_ms) // 10 + 1)
    if a >= b:
        return target_ms
    low = min(db[a:b])
    quiet = [f for f in range(a, b) if db[f] <= low + 3]
    f = min(quiet, key=lambda f: (abs(base_ms + f * 10 + 5 - target_ms), -f if prefer_late else f))
    return base_ms + f * 10 + 5


def cuts(take, src):
    """In and out points for every padding, from the audio around the take: each edge moves to a
    quiet frame near the padded time, never into the take's own words, and never further out than
    the neighbouring words. Stored on the take so the preview and the render cut the same."""
    s, e = take["s"], take["e"]
    reach_b = min(400, take["before"]) if take["before"] < 1000 else 400
    reach_a = min(500, take["after"]) if take["after"] < 1000 else 500
    base = max(0, s - reach_b)
    db = frames_db(src, base, e + reach_a)
    out = {}
    for name, (pb, pa) in PAD.items():
        a = _quiet(db, base, s - max(pb, 120), s + 10, s - pb, prefer_late=True) if db else s - pb
        b = _quiet(db, base, e - 10, e + max(pa, 160), e + pa, prefer_late=False) if db else e + pa
        out[name] = [max(base, a), min(e + reach_a, b)]
    return out


def cut(take, padding="normal"):
    """In and out points for one take: from its measured cuts when it has them, else the padding
    clamped so it never reaches into the neighbouring words (half the silence each side, at most)."""
    if take.get("cuts") and padding in take["cuts"]:
        return tuple(take["cuts"][padding])
    pb, pa = PAD.get(padding, PAD["normal"])
    pb = min(pb, max(0, take["before"] // 2)) if take["before"] < 1000 else pb
    pa = min(pa, max(0, take["after"] // 2)) if take["after"] < 1000 else pa
    return max(0, take["s"] - pb), take["e"] + pa


def caption_words(piece, take):
    """The take's heard timings carrying the words as typed (the user's spelling and punctuation)."""
    typed = [raw for raw in piece["text"].split() if snap.norm(raw)]
    return [dict(w, w=typed[k] if k < len(typed) else w["w"]) for k, w in enumerate(take["words"])]


def phrase_query(want):
    """The exact-phrase search for normalised words (quotes: tier 1 only)."""
    return '"' + " ".join(re.sub(r"[^a-z0-9']", "", w) for w in want) + '"'


def so_far(said, limit=60):
    """The sentence said so far, for subtitle captions: its last `limit` characters, whole words."""
    text = " ".join(said)
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    return "…" + cut[cut.find(" ") + 1:] if " " in cut else cut
