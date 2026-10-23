"""Word-timestamped transcript of a short WAV, as JSON on stdout.

Runs in the Python that has faster-whisper (tools.stt_python), as a child process:
  python -I stt_worker.py clip.wav [--careful]
Model: small.en, CPU int8 (downloaded once, ~500 MB, on first use).

--careful decodes with beam search (5) instead of greedily. Given a window holding several lines
with silence between them, greedy decoding can drop a line and stretch the others over the gap
(in one test episode "You're" was timed 3 s before it is said, which put the caption over
the previous speaker's line); beam search hears both where they are. It is slower, so snap uses it only to re-time a
match that looks stretched.

Whisper tends to start a word where the previous one ended, so after a pause the next word
"starts" in the silence, and a caption pops up before anyone speaks. Each start is therefore
refined against the audio itself: moved LATER to where the voice energy actually rises (never
earlier), unless the word is mid-flow with no dip to find. A faint word followed by silence is one
Whisper put on a breath or a noise ahead of the speech, and moves to just before the next word.

Each word also gets "loud": how much louder (+) or quieter (-) than the clip's typical word it is,
in dB, from the loudest part of the word. The laid-out caption looks size shouts up and whispers
down with it.
"""
import json
import sys
import wave

import numpy as np

FRAME_MS = 10


def read_wav(path):
    """16 kHz mono 16-bit PCM (what snap.transcribe writes) as float32 in [-1, 1]. Decoding it here
    and handing Whisper the samples keeps PyAV out of the loop (its API changes between releases)."""
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    return pcm, rate


def energy_db(pcm, rate):
    hop = rate * FRAME_MS // 1000
    n = len(pcm) // hop
    if n == 0:
        return np.zeros(0)
    frames = pcm[: n * hop].reshape(n, hop)
    return 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-6)


def _threshold(db):
    """The voice-onset level for this window, or None when there is no usable contrast."""
    if len(db) < 20:
        return None
    floor, loud = np.percentile(db, 15), np.percentile(db, 90)
    if loud - floor < 8:                     # no usable contrast (music bed, noise): leave it
        return None
    return floor + 0.35 * (loud - floor)


def refine_starts(words, db):
    thr = _threshold(db)
    if thr is None:
        return words
    for w in words:
        a = int(w["s"] / FRAME_MS)
        # the whole word: after a pause Whisper can stretch a word a second or more into the silence
        b = min(int(w["e"] / FRAME_MS), len(db) - 2)
        if a >= b or db[a] > thr:            # already loud at the stated start: it is mid-flow
            continue
        for f in range(a, b):
            if db[f] > thr and db[f + 1] > thr:              # two frames: ignore clicks
                w["s"] = min(f * FRAME_MS, w["e"] - 40)
                break
    return words


STRAY_DB, STRAY_GAP_MS, STRAY_MS = -15.0, 300, 100


def place_strays(words, db):
    """A faint word followed by silence is one Whisper put on a noise ahead of the speech ("I" on
    a breath 1.3 s before "I didn't do it"): move it to just before the next word."""
    thr = _threshold(db)
    if thr is None or len(words) < 3:
        return words
    peaks = [_peak(w, db) for w in words]
    known = [p for p in peaks if p is not None]
    if len(known) < 3:
        return words
    mid = float(np.median(known))
    for i, (w, nxt) in enumerate(zip(words, words[1:])):
        if peaks[i] is None or peaks[i] - mid > STRAY_DB or nxt["s"] - w["e"] < STRAY_GAP_MS:
            continue
        gap = db[int(w["e"] / FRAME_MS):int(nxt["s"] / FRAME_MS)]
        if len(gap) and gap.max() <= thr:
            d = min(w["e"] - w["s"], STRAY_MS)
            w["s"], w["e"] = nxt["s"] - d, nxt["s"]
    return words


def _peak(w, db):
    a, b = int(w["s"] / FRAME_MS), max(int(w["s"] / FRAME_MS) + 1, int(w["e"] / FRAME_MS))
    seg = db[a:min(b, len(db))]
    return float(np.percentile(seg, 80)) if len(seg) else None


def loudness(words, db):
    """words[i]["loud"]: the word's level (its loudest 10 ms frames) relative to the median word."""
    peaks = [_peak(w, db) for w in words]
    known = [p for p in peaks if p is not None]
    if len(known) < 3:
        return words
    mid = float(np.median(known))
    for w, p in zip(words, peaks):
        if p is not None:
            w["loud"] = round(p - mid, 1)
    return words


def transcribe(model, pcm, beam):
    segs, _ = model.transcribe(pcm, beam_size=beam, language="en", word_timestamps=True,
                               condition_on_previous_text=False)
    return [{"w": w.word.strip(), "s": round(w.start * 1000), "e": round(w.end * 1000)}
            for s in segs for w in (s.words or [])]


if __name__ == "__main__":
    from faster_whisper import WhisperModel
    model = WhisperModel("small.en", device="cpu", compute_type="int8", cpu_threads=4)
    # No VAD: the window is short and VAD can clip the first word, which is the one we need.
    pcm, rate = read_wav(sys.argv[1])
    words = transcribe(model, pcm, 5 if "--careful" in sys.argv[2:] else 1)
    db = energy_db(pcm, rate)
    json.dump(loudness(place_strays(refine_starts(words, db), db), db), sys.stdout)
