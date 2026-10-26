"""The Y2K aesthetic: a pink and lilac grade with a soft glow, inside a bedazzled frame.

The frame is drawn by ffmpeg's geq from formulas, once per output size (cached in data/cache/y2k),
so no artwork ships and every shape gets its own: a border of low-poly facets in pink, lilac and
pearl, a row of rhinestones along it (each shaded with a highlight, facets and a rim), a bright
inner edge, and a four-point star at each inner corner. It goes over the picture with movie= and
overlay, under the captions."""
import os
import subprocess

from . import config, media

CACHE = os.path.join(config.data_dir(), "cache", "y2k")
VERSION = 2                      # bump when the drawing changes, so cached frames are redrawn


def _frame_expr(w, h, ch):
    """geq expression for one channel (r, g, b or a) of the frame."""
    b = max(10, round(min(w, h) * 0.05))      # the border's width
    s = b * 0.9                               # facet cell
    p = b * 1.3                               # rhinestone spacing
    rg = b * 0.3                              # rhinestone radius
    R = b * 1.25                              # corner star
    # registers: 0 ex, 1 ey, 2 e (distance in from the nearest edge), 3 pos (along that edge),
    # 4 gem dx, 5 gem dy, 6 gem distance, 7 facet hash, 8 star, 9 gem index
    pre = (f"st(0,min(X,W-1-X));st(1,min(Y,H-1-Y));"
           f"st(2,min(ld(0),ld(1)));st(3,if(lt(ld(0),ld(1)),Y,X));"
           f"st(4,mod(ld(3),{p:.3f})-{p / 2:.3f});st(5,ld(2)-{b / 2:.3f});st(6,hypot(ld(4),ld(5)));"
           f"st(9,floor(ld(3)/{p:.3f}));"
           f"st(7,sin((floor(X/{s:.3f})*7.13+floor(Y/{s:.3f})*3.71+gt(mod(X,{s:.3f}),mod(Y,{s:.3f}))*1.37)*12.9898)*43758.5453);"
           f"st(7,ld(7)-floor(ld(7)));"
           # the nearest inner corner's star: an astroid, |dx|^.5 + |dy|^.5 < R^.5
           f"st(8,sqrt(abs(min(abs(X-{b}),abs(X-(W-1-{b}))))/{R:.3f})+sqrt(abs(min(abs(Y-{b}),abs(Y-(H-1-{b}))))/{R:.3f}));")
    band = f"lt(ld(2),{b})"
    gem = f"lt(ld(6),{rg:.3f})"
    star = "lt(ld(8),1)"
    edge = f"between(ld(2),{b},{b + max(1.5, b * 0.08):.3f})"
    # the gem's light: a highlight up and left, six facets, a darker rim
    hl = f"max(0,1-hypot(ld(4)+{rg * 0.35:.3f},ld(5)+{rg * 0.35:.3f})/{rg * 0.55:.3f})"
    facet = f"0.12*cos(6*atan2(ld(5),ld(4)))"
    rim = f"if(gt(ld(6),{rg * 0.78:.3f}),0.7,1)"
    gl = f"min(1,({rim})*(0.78+{facet})+{hl})"
    # colours: facets pink / lilac / pearl by the cell's hash, each shaded by a second hash
    shade = "(0.72+0.38*(ld(7)*7-floor(ld(7)*7)))"
    pal = {"r": (255, 205, 255), "g": (105, 160, 222), "b": (190, 255, 240)}
    gems = {"r": (255, 235), "g": (70, 235), "b": (165, 255)}          # pink, crystal
    if ch == "a":
        body = f"if({star},255,if({gem}+{band},if({band},235,255),if({edge},220,0)))"
    else:
        c0, c1, c2 = pal[ch]
        g0, g1 = gems[ch]
        facets = f"min(255,if(lt(ld(7),0.42),{c0},if(lt(ld(7),0.78),{c1},{c2}))*{shade})"
        gemc = f"min(255,if(mod(ld(9),3),{g0},{g1})*{gl}+60*{hl})"
        starc = {"r": 255, "g": 245, "b": 252}[ch]
        edgec = {"r": 255, "g": 225, "b": 245}[ch]
        body = f"if({star},{starc},if({gem},{gemc},if({band},{facets},{edgec})))"
    return pre + body


def frame(w, h):
    """The frame for a w x h output, as an RGBA PNG (drawn once, then cached)."""
    out = os.path.join(CACHE, f"v{VERSION}-{w}x{h}.png")
    if os.path.isfile(out):
        return out
    os.makedirs(CACHE, exist_ok=True)
    geq = ":".join(f"{c}='{_frame_expr(w, h, c)}'" for c in ("r", "g", "b", "a"))
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", f"color=c=black@0:s={w}x{h}:d=0.04,format=rgba",
                        "-vf", f"geq={geq}", "-frames:v", "1", out + ".tmp.png"], capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("could not draw the Y2K frame: " + r.stderr.decode(errors="replace").strip()[-300:])
    os.replace(out + ".tmp.png", out)
    return out


def filters(w, h):
    """The grade, the glow and the frame, as chain elements (labels are y2k-only)."""
    short = min(w, h)
    return ["curves=all='0/0.07 0.5/0.55 1/1'",
            "eq=saturation=1.18:gamma=1.04",
            "colorbalance=rs=0.08:gs=-0.04:bs=0.12:rm=0.11:gm=-0.07:bm=0.08:rh=0.07:gh=-0.03:bh=0.07",
            # a soft glow on the light parts (screened in RGB)
            f"format=gbrp,split[yka][ykb];[ykb]gblur=sigma={max(2, short / 90):.1f}[ykg];"
            "[yka][ykg]blend=all_mode=screen:all_opacity=0.35",
            f"null[yk0];movie={media._fpath(frame(w, h))}[ykf];[yk0][ykf]overlay=0:0:format=auto"]
