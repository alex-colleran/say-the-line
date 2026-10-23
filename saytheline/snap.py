"""Snap a cut to the spoken words: transcribe a little audio around it with word timestamps, find
the subtitle text in that transcript, and move in/out to where the words actually are.

Subtitle timing can be off by a second or more (wrong release, no sync), and a clip cut on
subtitle times then starts mid-word or ends early. Whisper's word times are accurate to roughly
0.1 s. Cost: ~10 s of CPU for a ~20 s window (2.3x realtime under the heat guard)."""
import difflib
import json
import os
import re
import subprocess
import tempfile

from . import config
from . import media

STT_PY = config.stt_python()          # the Python with faster-whisper / spaCy
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stt_worker.py")
SLACK_MS = 5000          # listen this far beyond the subtitle-timed cut on each side
LEAD_MS, TAIL_MS = 250, 400


def norm(w):
    return re.sub(r"[^a-z0-9']", "", w.lower().replace("’", "'"))


def tokens(text):
    return [t for t in (norm(w) for w in text.split()) if t]


def transcribe(src, a_ms, b_ms, careful=False):
    """Words with times on the media timeline. careful: beam search (see stt_worker), slower."""
    with tempfile.TemporaryDirectory(prefix="saytheline-snap-") as tmp:
        wav = os.path.join(tmp, "a.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{a_ms / 1000:.3f}",
                        "-t", f"{(b_ms - a_ms) / 1000:.3f}", "-i", src, "-map", f"0:a:{media.audio_track(src)}",
                        "-vn", "-ac", "1", "-ar", "16000", wav],
                       check=True, capture_output=True, timeout=60)
        r = subprocess.run([*config.nice(), STT_PY, "-I", WORKER, wav, *(["--careful"] if careful else [])],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[-300:])
    return [dict(w, s=w["s"] + a_ms, e=w["e"] + a_ms) for w in json.loads(r.stdout or "[]")]


WIDE_MS = 45000          # second try when the line is not heard near its subtitle time


def snap(src, in_ms, out_ms, cues, wide=True, context=None):
    """cues: the subtitle lines inside the current cut (their text is what we look for).
    context: other subtitle lines nearby (same timeline), used to pick the right one when the line
    is said more than once in the listening window.
    Tries ±5 s first; if the words are not there, listens ±45 s, because a subtitle made for a
    different release can be off by tens of seconds (26 s late in one test episode)."""
    target = tokens(" ".join(c["text"] for c in cues))
    if not target:
        raise ValueError("no subtitle text inside the cut to listen for")
    res = _snap_window(src, in_ms, out_ms, cues, target, SLACK_MS, context)
    # Also listen wide when the narrow match has no support from the neighbouring lines: the line
    # may be said twice, and with subtitles far off, ±5 s can hold only the *other* occurrence.
    doubtful = res.get("found") and context and res.get("context") == 0
    if wide and (not res.get("found") or doubtful):
        wide_res = _snap_window(src, in_ms, out_ms, cues, target, WIDE_MS, context)
        if wide_res.get("found") and (not res.get("found") or wide_res.get("context", 0) > 0):
            wide_res["wide"] = True
            res = wide_res
    return _retime(src, res, cues, target) if res.get("found") else res


STRETCHED_MS = 1200      # a matched word lasting longer than this was stretched over a pause
RETIME_MS = 3000         # how far around a stretched match to listen again


def _retime(src, res, cues, target):
    """Whisper sometimes stretches a word over a silence and drops the line before it (in one test
    episode "You're" was timed 3 s before it is said). Such a match is listened to again, close
    up and with beam search, and re-timed from that when the line is heard there as well."""
    said = [w for w in res["words"] if res["first_s"] <= w["s"] and w["e"] <= res["last_e"]]
    if not any(w["e"] - w["s"] > STRETCHED_MS for w in said):
        return res
    shift = res["first_s"] - min(c["start_ms"] for c in cues)
    moved = [dict(c, start_ms=c["start_ms"] + shift, end_ms=c["end_ms"] + shift) for c in cues]
    close = _snap_window(src, res["first_s"], res["last_e"], moved, target, RETIME_MS, careful=True)
    if not close.get("found") or close["score"] < res["score"]:
        return res
    close.update(offset_ms=close["offset_ms"] + shift, candidates=res["candidates"], context=res["context"],
                 wide=res.get("wide"), retimed=True)
    return close


def _occurrences(heard, target):
    """Every place in the transcript where the target words appear (>= half of them, in order)."""
    out, n = [], len(target)
    i = 0
    while i < len(heard):
        seg = heard[i:i + n + 2]
        m = difflib.SequenceMatcher(None, seg, target, autojunk=False)
        blocks = [b for b in m.get_matching_blocks() if b.size]
        if blocks and blocks[0].a == 0 and sum(b.size for b in blocks) / n >= 0.5:
            out.append((i, blocks))
            i += max(1, blocks[-1].a + blocks[-1].size)
        else:
            i += 1
    return out


def _context_score(words, context, delta):
    """How many words of the neighbouring subtitle lines are heard where they should be if the
    subtitles are shifted by `delta` ms."""
    score = 0
    for c in context or []:
        want = set(tokens(c["text"]))
        near = {norm(w["w"]) for w in words
                if c["start_ms"] + delta - 1500 <= w["s"] <= c["end_ms"] + delta + 1500}
        score += len(want & near)
    return score


def _snap_window(src, in_ms, out_ms, cues, target, slack, context=None, careful=False):
    a = max(0, in_ms - slack)
    words = transcribe(src, a, out_ms + slack, careful)
    heard = [norm(w["w"]) for w in words]
    if not words:
        return dict(found=False, reason="no speech heard")
    sub_start = min(c["start_ms"] for c in cues)
    occ = _occurrences(heard, target)
    if not occ:
        m = difflib.SequenceMatcher(None, heard, target, autojunk=False)
        score = sum(b.size for b in m.get_matching_blocks() if b.size) / len(target)
        return dict(found=False, score=round(score, 2),
                    heard=" ".join(w["w"] for w in words), reason="the subtitle words were not heard here")
    # Several places say it: the right one is where the neighbouring lines also line up. Ties go
    # to the occurrence nearest the subtitle's own time.
    def rank(o):
        start = words[o[0]]["s"]
        return (_context_score(words, context, start - sub_start), -abs(start - sub_start))
    i0, blocks = max(occ, key=rank)
    ctx = rank((i0, blocks))[0]
    matched = sum(b.size for b in blocks)
    first_h = i0 + blocks[0].a
    last_h = i0 + blocks[-1].a + blocks[-1].size - 1
    # If the subtitle text starts or ends with words Whisper missed, stretch by how many.
    first_h = max(0, first_h - blocks[0].b)
    last_h = min(len(words) - 1, last_h + (len(target) - (blocks[-1].b + blocks[-1].size)))
    new_in = max(0, words[first_h]["s"] - LEAD_MS)
    new_out = words[last_h]["e"] + TAIL_MS
    return dict(found=True, score=round(matched / len(target), 2), in_ms=new_in, out_ms=new_out,
                offset_ms=words[first_h]["s"] - sub_start,   # + = subtitles early, - = late
                heard=" ".join(w["w"] for w in words[first_h:last_h + 1]),
                first_s=words[first_h]["s"], last_e=words[last_h]["e"], candidates=len(occ), context=ctx,
                words=words)                                 # the whole window, for captions / phrase cuts


def phrase_span(words, query, lo_s, hi_e):
    """Times of the searched phrase itself inside the matched line, for "just the phrase" cuts."""
    want = tokens(query)
    span = [w for w in words if w["s"] >= lo_s - 50 and w["e"] <= hi_e + 50]
    if not want or not span:
        return None
    heard = [norm(w["w"]) for w in span]
    m = difflib.SequenceMatcher(None, heard, want, autojunk=False)
    blocks = [b for b in m.get_matching_blocks() if b.size]
    if not blocks or sum(b.size for b in blocks) / len(want) < 0.6:
        return None
    return span[blocks[0].a]["s"], span[blocks[-1].a + blocks[-1].size - 1]["e"]


def group_words(words, max_chars=42, max_ms=3500, gap_ms=600):
    """Turn Whisper words into caption lines: break on a pause, at sentence ends once a line has
    some length, or before a line gets too long to read."""
    cues, cur = [], []
    for w in words:
        if cur:
            text = " ".join(x["w"] for x in cur)
            if (w["s"] - cur[-1]["e"] > gap_ms or len(text) + 1 + len(w["w"]) > max_chars
                    or w["e"] - cur[0]["s"] > max_ms or (text[-1:] in ".?!" and len(text) > 15)):
                cues.append(cur)
                cur = []
        cur.append(w)
    if cur:
        cues.append(cur)
    out = []
    for i, c in enumerate(cues):
        end = c[-1]["e"] + 200                                   # brief hold so lines are readable
        if i + 1 < len(cues):
            end = min(end, cues[i + 1][0]["s"])
        out.append(dict(idx=i, start_ms=c[0]["s"], end_ms=end, text=" ".join(x["w"] for x in c),
                        words=list(c)))                          # each word belongs to exactly one line
    return out


def transcribe_cues(src, in_ms, out_ms, margin_ms=1000):
    """Caption lines from the speech itself, for clips whose subtitles are wrong, missing or
    paraphrased. A little margin so a word straddling the in/out point is not cut in half.
    Each line carries its words (with times) for word-synced caption styles."""
    words = transcribe(src, max(0, in_ms - margin_ms), out_ms + margin_ms)
    return group_words(words)


def align_words(sub_cues, words):
    """Word times for the SUBTITLE's own text: match its words against the transcript and take
    the spoken times; words Whisper missed are spread evenly between their matched neighbours
    (or across their line when nothing in it matched, e.g. sung lyrics)."""
    disp = []                                   # (display word, cue) in reading order
    for c in sub_cues:
        for w in c["text"].split():
            if norm(w):
                disp.append((w, c))
    heard = [norm(w["w"]) for w in words]
    m = difflib.SequenceMatcher(None, [norm(w) for w, _ in disp], heard, autojunk=False)
    times = [None] * len(disp)
    for b in m.get_matching_blocks():
        for k in range(b.size):
            times[b.a + k] = (words[b.b + k]["s"], words[b.b + k]["e"])
    i = 0
    while i < len(disp):                         # fill runs of unmatched words
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < len(disp) and times[j] is None:
            j += 1
        cue = disp[i][1]
        lo = times[i - 1][1] if i and disp[i - 1][1] is cue else cue["start_ms"]
        hi = times[j][0] if j < len(disp) and disp[j][1] is cue else cue["end_ms"]
        # anchor to heard speech, not the subtitle's own timing (which can lead the voice)
        heard_in = [x["s"] for x in words if lo - 100 <= x["s"] < hi]
        if heard_in and not (i and disp[i - 1][1] is cue):
            lo = max(lo, min(heard_in))
        # and never out of reading order: subtitles seconds ahead of the voice put a line's own start
        # among the previous line's spoken words, so a missed first word showed in the wrong caption
        if i:
            lo = max(lo, times[i - 1][1])
        if hi <= lo:
            hi = lo + 120 * (j - i)
        if j < len(disp) and lo < times[j][0] < hi:
            hi = times[j][0]
        step = (hi - lo) / (j - i)
        for k in range(i, j):
            times[k] = (round(lo + step * (k - i)), round(lo + step * (k - i + 1)))
        i = j
    out = []
    for c in sub_cues:
        ws = [dict(w=w, s=t[0], e=t[1]) for (w, cc), t in zip(disp, times) if cc is c]
        out.append(dict(idx=c.get("idx", 0), start_ms=c["start_ms"], end_ms=c["end_ms"], text=c["text"], words=ws))
    return out


def subtitle_words(src, in_ms, out_ms, sub_cues, margin_ms=1500):
    words = transcribe(src, max(0, in_ms - margin_ms), out_ms + margin_ms)
    return align_words(sub_cues, words)
