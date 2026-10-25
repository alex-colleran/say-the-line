"""Beats of a song, for the Studio's music: tempo, every beat's time and which beats start a bar.

Runs in the speech Python (it needs numpy only), as a child process:
  python beat_worker.py song.wav  ->  {"bpm", "beats": [s], "downbeats": [s], "duration"}
The wav is mono 22050 Hz (web.py decodes it with ffmpeg first).

The method is the classic one (Ellis 2007, "Beat tracking by dynamic programming"):
  1. onset strength: how much new sound starts in each 23 ms frame (positive spectral flux on a
     log-compressed spectrogram), with its slow trend removed;
  2. tempo: the period whose autocorrelation of that envelope is strongest, weighted towards ~115
     BPM so a song isn't read at half or double its tempo without good reason;
  3. beats: a dynamic programme picks the beat times that land on strong onsets while keeping the
     spacing close to that period;
  4. downbeats: of the four ways to group beats into bars of four, the one whose first beats carry
     the most low-frequency onset (kick drums usually mark the bar)."""
import json
import sys
import wave

import numpy as np

SR, N_FFT, HOP = 22050, 2048, 512
FPS = SR / HOP                       # onset frames per second (~43)
BPM_MIN, BPM_MAX, BPM_PRIOR, PRIOR_WIDTH = 60, 200, 115, 1.0
TIGHTNESS = 100                      # how strongly beats keep to the period (Ellis's alpha)
LATENCY_S = 0.015                    # flux peaks this early (measured on click tracks, 70-174 BPM)


def read(path):
    with wave.open(path) as w:
        assert w.getframerate() == SR and w.getnchannels() == 1 and w.getsampwidth() == 2, "mono 16-bit 22050 Hz wav expected"
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768


def spectrogram(x):
    x = np.concatenate([np.zeros(N_FFT // 2, dtype=x.dtype), x])     # frame f is centred on f * HOP
    n = 1 + max(0, len(x) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
    frames = x[np.minimum(idx, len(x) - 1)] * np.hanning(N_FFT)[None, :]
    return np.abs(np.fft.rfft(frames, axis=1))                 # (frames, bins)


def onset_strength(mag, lo_hz=0, hi_hz=SR / 2):
    """Positive spectral flux of the log spectrogram, summed over bins in [lo_hz, hi_hz)."""
    freqs = np.fft.rfftfreq(N_FFT, 1 / SR)
    band = (freqs >= lo_hz) & (freqs < hi_hz)
    logm = np.log1p(100 * mag[:, band])
    flux = np.maximum(0, np.diff(logm, axis=0)).sum(axis=1)
    flux = np.concatenate([[0], flux])
    trend = np.convolve(flux, np.ones(int(FPS)) / int(FPS), mode="same")    # ~1 s moving average
    env = np.maximum(0, flux - trend)
    return env / (env.std() + 1e-9)


def tempo(env):
    """(bpm, period in frames) from the onset envelope's autocorrelation."""
    e = env - env.mean()
    ac = np.correlate(e, e, mode="full")[len(e) - 1:]
    lags = np.arange(len(ac))
    lo, hi = int(FPS * 60 / BPM_MAX), int(FPS * 60 / BPM_MIN)
    bpm = 60 * FPS / np.maximum(lags, 1)
    prior = np.exp(-0.5 * (np.log2(bpm / BPM_PRIOR) / PRIOR_WIDTH) ** 2)
    score = np.where((lags >= lo) & (lags <= hi), ac * prior, -np.inf)
    lag = int(np.argmax(score))
    # refine to a fractional lag with a parabola through the peak and its neighbours
    if 0 < lag < len(ac) - 1:
        a, b, c = ac[lag - 1], ac[lag], ac[lag + 1]
        d = (a - c) / (2 * (a - 2 * b + c)) if (a - 2 * b + c) != 0 else 0
        lag = lag + float(np.clip(d, -0.5, 0.5))
    return 60 * FPS / lag, lag


def track(env, period):
    """Beat frames: Ellis's dynamic programme."""
    n = len(env)
    score = env.copy()
    back = -np.ones(n, dtype=int)
    lo, hi = int(round(period / 2)), int(round(period * 2))
    offsets = np.arange(-hi, -lo + 1)
    penalty = -TIGHTNESS * np.log(-offsets / period) ** 2
    for t in range(hi, n):
        cand = score[t + offsets] + penalty
        k = int(np.argmax(cand))
        score[t] = env[t] + cand[k]
        back[t] = t + offsets[k]
    # end on the best-scoring frame in the last period, then follow the chain back
    t = int(np.argmax(score[max(0, n - hi):])) + max(0, n - hi)
    beats = []
    while t >= 0:
        beats.append(t)
        t = back[t]
    beats = np.array(beats[::-1])
    # the chain can't reach the first ~2 periods: extend it backwards on the same spacing
    first = beats[0] if len(beats) else 0
    while first - period > 0:
        first -= period
        beats = np.concatenate([[int(round(first))], beats])
    return beats


def downbeats(beats, low_env):
    """The bar starts: of the 4 phases, the one whose beats have the most low-band onset."""
    if len(beats) < 8:
        return beats[:1]
    sums = [low_env[np.minimum(beats[p::4], len(low_env) - 1)].mean() for p in range(4)]
    return beats[int(np.argmax(sums))::4]


def analyse(x):
    mag = spectrogram(x)
    env = onset_strength(mag)
    bpm, period = tempo(env)
    beats = track(env, period)
    low = onset_strength(mag, 0, 150)
    db = downbeats(beats, low)
    t = lambda f: [round(float(v) / FPS + LATENCY_S, 3) for v in f]    # noqa: E731
    return dict(bpm=round(bpm, 1), beats=t(beats), downbeats=t(db), duration=round(len(x) / SR, 2))


if __name__ == "__main__":
    print(json.dumps(analyse(read(sys.argv[1]))))
