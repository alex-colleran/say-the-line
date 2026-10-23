"""Subtitle discovery and parsing: external .srt/.ass/.vtt beside the video, else an embedded
text stream pulled out with ffmpeg. Bitmap subs (PGS/VobSub/DVB) are skipped — not searchable
without OCR."""
import os
import re
import subprocess

TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "sami", "microdvd", "text"}
SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt")
ENGLISH = {"en", "eng", "english"}

# ASS styles that carry signs, song lyrics and karaoke rather than dialogue (fansub convention).
NON_DIALOGUE_STYLE = re.compile(r"(?i)sign|song|karaoke|lyric|^op|^ed|title|credit|note")


def _lang_rank(tokens):
    """Rank an external sub by the dotted tokens between the video stem and the extension
    ('Show.S01E01.eng.sdh.srt' -> ['eng', 'sdh']). Lower is better; None = not English."""
    toks = [t.lower() for t in tokens]
    langs = [t for t in toks if len(t) in (2, 3) and t.isalpha() and t not in ("hi", "cc")]
    if langs and not any(t in ENGLISH for t in langs):
        return None
    rank = 0 if langs else 1           # explicitly English beats unlabelled
    if "forced" in toks:
        rank += 10                      # forced tracks only cover foreign-language bits
    if any(t in ("sdh", "hi", "cc") for t in toks):
        rank += 1                       # SDH is fine, plain dialogue is cleaner
    return rank


def find_external(video_path):
    """Best English external subtitle next to the video, or None."""
    d, base = os.path.split(video_path)
    stem = os.path.splitext(base)[0]
    best = None
    try:
        names = os.listdir(d)
    except OSError:
        return None
    for n in names:
        root, ext = os.path.splitext(n)
        if ext.lower() not in SUB_EXTS or not root.startswith(stem):
            continue
        rest = root[len(stem):].strip(". ")
        rank = _lang_rank([t for t in rest.split(".") if t])
        if rank is None:
            continue
        if best is None or rank < best[0]:
            best = (rank, os.path.join(d, n))
    return best[1] if best else None


def extract_embedded(video_path, stream_index, codec):
    """Pull one embedded text stream. ASS stays ASS: converting it to SRT throws away the
    style names that tell dialogue apart from typeset signs. Everything else becomes SRT."""
    fmt = "ass" if codec in ("ass", "ssa") else "srt"
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", video_path, "-map", f"0:{stream_index}",
         "-f", fmt, "-"],
        capture_output=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()[:300])
    return r.stdout.decode("utf-8", errors="replace")


MAX_OVERLAP = 0.25     # share of lines starting before the previous one ends


def overlap(cues):
    """Share of cues that start before the previous one ends. Near 0 for real subtitles; one
    release (The Boondocks, YOGI) muxed each episode's lines together with every earlier episode's,
    which shows up as half the lines overlapping. Signs over dialogue in anime overlap a little."""
    cs = sorted(cues)
    if len(cs) < 2:
        return 0.0
    return sum(1 for (a0, b0, _), (a1, _, _) in zip(cs, cs[1:]) if a1 < b0 - 100) / (len(cs) - 1)


def read_text(path):
    with open(path, "rb") as f:
        raw = f.read()
    # UTF-16 only with a byte-order mark or the NUL bytes it is full of: decoding as UTF-16
    # "succeeds" on most even-length Latin-1 files and yields gibberish (72 movies read as no subs)
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    head = raw[:200]
    if head[1::2].count(0) > len(head) // 4:
        return raw.decode("utf-16-le", errors="replace")
    if head[0::2].count(0) > len(head) // 4:
        return raw.decode("utf-16-be", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


TAG = re.compile(r"<[^>]+>|\{[^}]*\}")


def clean(text):
    text = text.replace("¶", "♪")   # SDH/CEA-608 conversions use a pilcrow for sung lines
    text = text.replace("\\N", " ").replace("\\n", " ").replace("\\h", " ")
    text = TAG.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


SRT_TIME = re.compile(
    r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})")


def _ms(h, m, s, frac):
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac.ljust(3, "0")[:3])


def parse_srt(text):
    """SRT and WebVTT share the timing line; anything that isn't a timing line, a cue number
    or a blank line is cue text."""
    cues, cur = [], None
    for line in text.splitlines():
        m = SRT_TIME.search(line)
        if m:
            if cur and cur[2]:
                cues.append(cur)
            cur = [_ms(*m.groups()[:4]), _ms(*m.groups()[4:]), []]
        elif cur is not None and line.strip():
            if line.strip().isdigit() and not cur[2]:
                continue
            cur[2].append(line)
        elif cur is not None and not line.strip() and cur[2]:
            cues.append(cur)
            cur = None
    if cur and cur[2]:
        cues.append(cur)
    return [(a, b, clean(" ".join(t))) for a, b, t in cues]


def parse_ass(text):
    cues, fmt = [], None
    for line in text.splitlines():
        if line.startswith("Format:") and fmt is None and "Start" in line:
            fmt = [f.strip().lower() for f in line[7:].split(",")]
        elif line.startswith("Dialogue:") and fmt:
            parts = line[9:].split(",", len(fmt) - 1)
            row = dict(zip(fmt, (p.strip() for p in parts)))
            if NON_DIALOGUE_STYLE.search(row.get("style", "")):
                continue
            # Typeset signs are positioned or animated; dialogue almost never is. {\p1} lines
            # are vector drawings ("m 608 206 l 724 247 ...").
            if re.search(r"\\(pos|move|p[1-9]|clip|org)\b", row.get("text", "")):
                continue
            h1, m1, s1 = row["start"].split(":")
            h2, m2, s2 = row["end"].split(":")
            s1, f1 = s1.split(".")
            s2, f2 = s2.split(".")
            cues.append((_ms(h1, m1, s1, f1 + "0"), _ms(h2, m2, s2, f2 + "0"), clean(row["text"])))
    return cues


def parse(text, kind):
    return parse_ass(text) if kind in ("ass", "ssa") else parse_srt(text)


# Credits and ads that subtitle sites stamp into the first/last cues.
SUB_SPAM = re.compile(r"(?i)addic7ed|opensubtitles|subscene|podnapisi|yify|\bwww\.|\.(com|org|net)\b"
                      r"|sync(ed)? (and|&) correct|subtitles? (by|ripped|downloaded)|advertise your"
                      r"|transcript by|captioning (made possible|sponsored)|resync")


COMMON_EN = {"the", "you", "i", "to", "a", "and", "it", "is", "that", "what", "of", "in", "me",
             "this", "we", "don't", "i'm", "your", "no", "yes", "be", "have", "do", "not", "my"}


def is_dialogue(cues, runtime_s):
    """A usable English dialogue track: enough lines for the runtime (rules out signs-only and
    forced tracks) and mostly common English words (rules out mislabelled foreign tracks)."""
    minutes = (runtime_s or 1320) / 60
    if len(cues) < max(15, 3 * minutes):
        return False
    words = re.findall(r"[a-z']+", " ".join(t for _, _, t in cues[:400]).lower())
    return bool(words) and sum(w in COMMON_EN for w in words) / len(words) > 0.15


def tidy(cues):
    """Drop empty and pure sound-effect cues, sort, and merge exact duplicates (some rips
    repeat every line for styling layers)."""
    out, seen = [], set()
    for a, b, t in sorted(cues):
        # "[DOOR SLAMS]", "(sighs)", "♪" — nothing left once the bracketed parts are gone
        if not re.sub(r"\[[^\]]*\]|\([^)]*\)", "", t).strip(" ♪#*-") or SUB_SPAM.search(t):
            continue
        key = (a, t)
        if key in seen:
            continue
        seen.add(key)
        out.append((a, b, t))
    return out
