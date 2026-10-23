"""Jellyfin library -> subtitle cues -> SQLite FTS5. Incremental: an item is re-read only when
its subtitle source or that source's mtime changed."""
import datetime
import os

from . import db, jellyfin, subs

def _drop_cues(c, item_id):
    for cid, text in c.execute("SELECT id, text FROM cues WHERE item_id = ?", (item_id,)).fetchall():
        c.execute("INSERT INTO cues_fts(cues_fts, rowid, text) VALUES('delete', ?, ?)", (cid, text))
        c.execute("DELETE FROM pairs_fts WHERE rowid = ?", (cid,))
    c.execute("DELETE FROM cues WHERE item_id = ?", (item_id,))


def index(index_path, series=None, movies=False, external_only=False, log=print):
    c = db.open_index(index_path)
    pmap = db.path_map()
    stats = dict(items=0, indexed=0, unchanged=0, no_subs=0, errors=0, cues=0)
    # Subtitle files first (cheap), then embedded tracks (ffmpeg reads the whole file), each newest
    # first: a pass cut short by its time cap has then done the most for what was just added.
    work = []
    for it in jellyfin.list_items(None, series, movies):
        path = db.to_host(it["path"], pmap)
        work.append((it, path, subs.find_external(path)))
    work.sort(key=lambda x: not x[2])
    for it, path, ext in work:
        stats["items"] += 1
        if ext:
            source, mtime = ext, os.path.getmtime(ext)
        elif it["embedded"] and not external_only and os.path.exists(path):
            source, mtime = "embedded", os.path.getmtime(path)
        else:
            stats["no_subs"] += 1
            continue

        row = c.execute("SELECT id, sub_source, sub_mtime FROM items WHERE jf_id = ?",
                        (it["jf_id"],)).fetchone()
        if row and row[1].split(":")[0] == source and row[2] == mtime:
            stats["unchanged"] += 1
            continue

        # Candidates in order: the external file, then each embedded text track.
        cands = [(ext, os.path.splitext(ext)[1][1:].lower(), None)] if ext else []
        cands += [] if external_only else [(f"embedded:{x}", "ass" if cdc in ("ass", "ssa") else "srt", (x, cdc))
                  for x, cdc in it["embedded"]] if os.path.exists(path) else []
        cues, err, fallback = None, None, None
        for src, kind, emb in cands:
            try:
                text = subs.extract_embedded(path, *emb) if emb else subs.read_text(src)
                got = subs.tidy(subs.parse(text, kind))
            except Exception as e:  # one bad file must not stop a library pass
                err = e
                continue
            if subs.is_dialogue(got, it["runtime_s"]):
                # a track that overlaps itself heavily holds more than this episode: keep looking,
                # and use it only when nothing else reads as dialogue
                if subs.overlap(got) <= subs.MAX_OVERLAP:
                    cues, source = got, src
                    break
                fallback = fallback or (got, src)
        if cues is None and fallback:
            cues, source = fallback
        if cues is None:
            if err:
                stats["errors"] += 1
                log(f"  ! {path}: {err}")
            else:
                stats["no_subs"] += 1
                log(f"  - {path}: no track reads as English dialogue")
            continue

        now = datetime.datetime.now().isoformat(timespec="seconds")
        meta = (it["kind"], it["series"], it["season"], it["episode"], it["title"], it["year"],
                path, it["runtime_s"], source, mtime, now)
        if row:
            item_id = row[0]
            _drop_cues(c, item_id)
            c.execute("""UPDATE items SET kind=?, series=?, season=?, episode=?, title=?, year=?,
                         path=?, runtime_s=?, sub_source=?, sub_mtime=?, indexed_at=? WHERE id=?""",
                      meta + (item_id,))
        else:
            item_id = c.execute("""INSERT INTO items (kind, series, season, episode, title, year,
                         path, runtime_s, sub_source, sub_mtime, indexed_at, jf_id)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", meta + (it["jf_id"],)).lastrowid
        ids = []
        for n, (a, b, t) in enumerate(cues):
            cid = c.execute("INSERT INTO cues (item_id, idx, start_ms, end_ms, text) "
                            "VALUES (?,?,?,?,?)", (item_id, n, a, b, t)).lastrowid
            c.execute("INSERT INTO cues_fts(rowid, text) VALUES (?, ?)", (cid, t))
            ids.append((cid, t))
        for (cid, t), (_, t2) in zip(ids, ids[1:]):
            c.execute("INSERT INTO pairs_fts(rowid, text) VALUES (?, ?)", (cid, f"{t} {t2}"))
        c.commit()
        stats["indexed"] += 1
        stats["cues"] += len(cues)
        label = (f"{it['series']} S{it['season'] or 0:02d}E{it['episode'] or 0:02d}"
                 if it["kind"] == "episode" else it["title"])
        log(f"  {label}: {len(cues)} cues ({source if source.startswith('embedded') else 'external'})")
    c.commit()
    return stats
