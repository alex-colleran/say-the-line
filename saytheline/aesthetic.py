"""Aesthetics: a treatment for the whole picture, graded before the captions go on (so the
captions keep their own colours). Plain ffmpeg filters, so they add next to nothing to a render.

  punchy  the shared-clip look: more colour and contrast, "clarity" (mid-size detail lifted with a
          wide unsharp mask), a crisp finish (contrast-adaptive sharpening), a touch warmer, a soft
          vignette
  grunge  dark and dirty: blacks crushed and whites held down, colour drained towards green-teal,
          film grain that moves, a heavy vignette and a slight red/blue fringe
  y2k     pink and lilac with a soft glow, in a bedazzled frame drawn to fit the shape (y2k.py)

Sizes (the clarity radius, the fringe) follow the output size, so 480p and 1080p look alike.
The editor shows an approximation with CSS filters (= JS AES_CSS); the render is exact."""

NAMES = {"none": "None", "punchy": "Punchy", "grunge": "Grunge", "y2k": "Y2K"}


def _odd(x, lo, hi):
    x = int(round(max(lo, min(hi, x))))
    return x if x % 2 else x + 1


def filters(name, w, h):
    """The filters for the aesthetic at this output size ([] for none)."""
    short = min(w, h)
    if name == "punchy":
        k = _odd(short / 60, 5, 23)                   # ~13 at 720p: lifts detail without halos
        return ["eq=contrast=1.08:saturation=1.38:gamma=1.04",
                "colorbalance=rm=0.025:bm=-0.025:rh=0.02:bh=-0.02",
                f"unsharp=lx={k}:ly={k}:la=0.55:cx=5:cy=5:ca=0",
                "cas=strength=0.35",
                "vignette=angle=PI/8"]
    if name == "y2k":
        from . import y2k
        return y2k.filters(w, h)
    if name == "grunge":
        px = max(1, round(short / 360))               # the fringe, ~2 px at 720p
        return ["curves=all='0/0.04 0.15/0.1 0.5/0.47 0.85/0.8 1/0.9'",
                "eq=saturation=0.6",
                "colorbalance=rs=-0.06:gs=0.03:bs=0.02:rm=-0.04:gm=0.04:bm=-0.01:rh=0.02:bh=-0.05",
                f"rgbashift=rh=-{px}:bh={px}",
                f"noise=c0s={10 + short // 180}:c0f=t+u",
                "vignette=angle=PI/6"]
    return []


def of(opts):
    """The aesthetic a request asks for, or "none". Raises ValueError for an unknown one."""
    name = opts.get("aesthetic") or "none"
    if name not in NAMES:
        raise ValueError(f"aesthetics are {', '.join(NAMES.values())}")
    return name
