"""Retake the README screenshots from a demo index that holds one public-domain film, so nothing
else in your library shows. Needs Playwright (`pip install playwright`) and Chrome or Chromium.

  python docs/screenshots/take.py demo "His Girl Friday"     # 1. build ./demo-data from the index
  SAYTHELINE_DATA=demo-data SAYTHELINE_PORT=8766 python -m saytheline.web &      # 2. a demo server
  python docs/screenshots/take.py shoot story                # 3. screenshots into ./shots

Then compose docs/screenshots/*.png from ./shots (see the README there).
"""
import asyncio
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

URL = os.environ.get("DEMO_URL", "http://127.0.0.1:8766/")
CHROME = os.environ.get("CHROME", "/usr/bin/google-chrome")


def demo(title, out="demo-data"):
    from saytheline import config, db
    os.makedirs(out, exist_ok=True)
    c = sqlite3.connect(os.path.join(out, "cues.db"))
    c.executescript(db.SCHEMA)
    c.execute("ATTACH ? AS o", (os.path.join(config.data_dir(), "cues.db"),))
    ids = [r[0] for r in c.execute("SELECT id FROM o.items WHERE title = ?", (title,))]
    if not ids:
        sys.exit(f"no item titled {title!r} in the index")
    q = ",".join("?" * len(ids))
    c.execute(f"INSERT INTO items SELECT * FROM o.items WHERE id IN ({q})", ids)
    c.execute(f"INSERT INTO cues SELECT * FROM o.cues WHERE item_id IN ({q})", ids)
    c.execute("INSERT INTO cues_fts(cues_fts) VALUES('rebuild')")
    c.execute(f"INSERT INTO pairs_fts(rowid, text) SELECT p.rowid, p.text FROM o.pairs_fts p "
              f"JOIN o.cues q ON q.id = p.rowid WHERE q.item_id IN ({q})", ids)
    c.commit()
    with open(os.path.join(out, "offsets.json"), "w") as f:
        json.dump({}, f)
    print(f"{out}: {len(ids)} item(s), {c.execute('SELECT count(*) FROM cues').fetchone()[0]} lines")


async def until(pg, js, secs):
    for _ in range(secs * 2):
        if await pg.evaluate(js):
            return True
        await pg.wait_for_timeout(500)
    return False


async def shoot_at(b, query, width, out):
    mob = width < 700
    ctx = await b.new_context(viewport=dict(width=width, height=844 if mob else 860), device_scale_factor=2 if mob else 1,
                              is_mobile=mob, has_touch=mob)
    pg = await ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    shot = lambda n: pg.screenshot(path=f"{out}/{width}-{n}.png")      # noqa: E731
    await pg.goto(URL + "?q=" + query.replace(" ", "+"))
    await pg.wait_for_timeout(4500)
    await shot("1-results")
    await pg.click(".card >> nth=0")
    await pg.wait_for_timeout(2500)
    if await pg.locator("#startFresh").count():                          # a draft from an earlier run
        await pg.click("#startFresh")
        await pg.wait_for_timeout(1500)
    await until(pg, "!document.querySelector('#shotHint').textContent.includes('Finding')", 60)
    await pg.wait_for_timeout(1500)
    await shot("2-editor")
    if mob:                                                             # TikTok look, word timing, a GIF
        await pg.click("[data-tab=captions]")
        await pg.click("[data-opt=look] .chip:nth-child(2)")
        await pg.click("#sttGo")
        await pg.wait_for_timeout(1000)
        await until(pg, "!document.querySelector('#sttGo').disabled && "
                        "document.querySelector('#sttHint').textContent.startsWith('Done')", 120)
        await pg.evaluate("seekTo(ed.inMs + 1600)")                     # mid-line, so a word is lit
        await pg.wait_for_timeout(1200)
        await pg.evaluate("document.querySelector('.stage').scrollIntoView({block:'start'}); drawOverlay()")
        await pg.wait_for_timeout(400)
        await shot("3-captions")                                        # preview with captions over the Look row
        await pg.evaluate("document.querySelector('#sheet').scrollTop = 0")
        await pg.click("[data-tab=output]")
        await pg.click("[data-opt=format] .chip:nth-child(3)")
        await pg.click("#render")
        await until(pg, "!!document.querySelector('#result .result')", 180)
        await pg.wait_for_timeout(2500)
        await pg.evaluate("document.querySelector('#result').scrollIntoView({block:'center'})")
        await pg.wait_for_timeout(800)
        await shot("4-result")
    await ctx.close()
    return errs


async def shoot(query, out="shots"):
    from playwright.async_api import async_playwright
    os.makedirs(out, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=CHROME)
        for w in (390, 1280):
            errs = await shoot_at(b, query, w, out)
            print(w, "page errors:", errs or "none")
        await b.close()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "demo":
        demo(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == "shoot":
        asyncio.run(shoot(sys.argv[2]))
    else:
        sys.exit(__doc__)
