"""Say the Line CLI.

  python -m saytheline index  --series "Your Show"         # or --movies, or no filter = all episodes
  python -m saytheline search "we need to talk" [--any|--raw] [--series NAME] [--limit N]
  python -m saytheline stats
  python -m saytheline serve                              # the web UI (same as python -m saytheline.web)
  python -m saytheline doctor                             # check the setup
  python -m saytheline login [--url URL]                  # Jellyfin token by Quick Connect, saved as api_key
  python -m saytheline mcp                               # MCP server for AI agents (stdio; needs the web server running)
"""
import argparse
import os
import re
import sqlite3
import time

from . import config
from . import index as idx
from . import search as srch

INDEX = os.environ.get("SAYTHELINE_INDEX", os.path.join(config.data_dir(), "cues.db"))


def main():
    ap = argparse.ArgumentParser(prog="saytheline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("index")
    pi.add_argument("--series")
    pi.add_argument("--movies", action="store_true")
    pi.add_argument("--external-only", action="store_true",
                    help="skip embedded tracks (they need ffmpeg to read the whole file)")
    pi.add_argument("--ocr-minutes", type=float, default=60,
                    help="at most this long reading Blu-ray and DVD picture subtitles by OCR per pass (default 60)")
    pi.add_argument("--item", help="just this Jellyfin item id")
    pi.add_argument("-q", "--quiet", action="store_true")
    ps = sub.add_parser("search")
    ps.add_argument("query")
    ps.add_argument("--any", action="store_true", help="match any word instead of the phrase")
    ps.add_argument("--raw", action="store_true", help="FTS5 query syntax as typed")
    ps.add_argument("--series")
    ps.add_argument("--limit", type=int, default=20)
    sub.add_parser("stats")
    sub.add_parser("serve")
    sub.add_parser("doctor")
    pl = sub.add_parser("login", help="get a Jellyfin token by Quick Connect and save it as [jellyfin] api_key")
    pl.add_argument("--url", help="Jellyfin's address, if saytheline.toml has none yet")
    sub.add_parser("mcp")
    a = ap.parse_args()

    if a.cmd == "login":
        from . import login
        return login.main(a.url)

    if a.cmd == "mcp":
        try:
            from . import mcp_server
        except ImportError as e:
            raise SystemExit(f"the MCP server needs the mcp package: pip install -r requirements-mcp.txt ({e})")
        return mcp_server.main()

    if a.cmd == "serve":
        from . import web
        return web.main()
    if a.cmd == "doctor":
        from . import doctor
        raise SystemExit(doctor.main())

    if a.cmd == "index":
        config.lower_priority()          # Windows: what `nice` does for the heavy children on Linux
        t = time.time()
        st = idx.index(INDEX, a.series, a.movies, a.external_only,
                       log=(lambda *_: None) if a.quiet else print, ocr_minutes=a.ocr_minutes, only=a.item)
        print(f"{st}  in {time.time() - t:.1f}s")
    elif a.cmd == "search":
        mode = "raw" if a.raw else "any" if a.any else "phrase"
        t = time.time()
        hits = srch.search(INDEX, a.query, mode, a.series, a.limit)
        words = [w for w in re.findall(r"[\w']+", a.query.lower()) if len(w) > 1]
        hl = re.compile(r"(?i)\b(" + "|".join(map(re.escape, words)) + r")") if words else None
        for h in hits:
            text = hl.sub(lambda m: f"\033[1;33m{m.group(0)}\033[0m", h["text"]) if hl else h["text"]
            print(f"\033[1m{srch.label(h['item'])}\033[0m  @ {srch.fmt_ms(h['start_ms'])}")
            if h["before"]:
                print(f"    \033[2m{h['before']}\033[0m")
            print(f"    {text}")
            if h["after"]:
                print(f"    \033[2m{h['after']}\033[0m")
        print(f"-- {len(hits)} hit(s) in {(time.time() - t) * 1000:.0f} ms")
    elif a.cmd == "stats":
        c = sqlite3.connect(INDEX)
        n_items, n_cues = c.execute("SELECT (SELECT COUNT(*) FROM items), (SELECT COUNT(*) FROM cues)").fetchone()
        print(f"{n_items} items, {n_cues} cues, {os.path.getsize(INDEX) / 1e6:.1f} MB")
        for s, n in c.execute("SELECT ifnull(series, '(movies)'), COUNT(*) FROM items GROUP BY 1 ORDER BY 2 DESC LIMIT 20"):
            print(f"  {n:5d}  {s}")


if __name__ == "__main__":
    main()
