"""Jellyfin library -> subtitle cues -> SQLite FTS5. Incremental: an item is re-read only when
its subtitle source or that source's mtime changed."""
import datetime
import os
import subprocess
import tempfile
import time

from . import config, db, jellyfin, pgs, subs, vobsub

OCR_DIR = os.path.join(config.data_dir(), "ocr")      # Blu-ray and DVD picture tracks read once, kept as SRT


def ocr_track(path, jf_id, stream, codec="hdmv_pgs_subtitle"):
    """An English Blu-ray or DVD picture track as SRT text, read by OCR once and kept in data/ocr."""
    out = os.path.join(OCR_DIR, f"{jf_id}-{stream}.srt")
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(path):
        with open(out, encoding="utf-8") as f:
            return f.read()
    os.makedirs(OCR_DIR, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="saytheline-pgs-") as tmp:
        dvd = codec in subs.VOBSUB_CODECS
        # (a DVD track goes into a small .mkv, which keeps its palette with it)
        one = os.path.join(tmp, "t.mkv" if dvd else "t.sup")
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", path, "-map", f"0:{stream}",
                            "-c", "copy", one], capture_output=True, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode(errors="replace").strip()[:300])
        text = vobsub.to_srt(one) if dvd else pgs.to_srt(one)
    with open(out + ".tmp", "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(out + ".tmp", out)
    return text

def _drop_cues(c, item_id):
    for cid, text in c.execute("SELECT id, text FROM cues WHERE item_id = ?", (item_id,)).fetchall():
        c.execute("INSERT INTO cues_fts(cues_fts, rowid, text) VALUES('delete', ?, ?)", (cid, text))
        c.execute("DELETE FROM pairs_fts WHERE rowid = ?", (cid,))
    c.execute("DELETE FROM cues WHERE item_id = ?", (item_id,))


def _kind(sub_source):
    """'embedded' or 'pgs' for a file's own track ('embedded:3'), else the external file's path,
    whole: a path can hold a colon of its own (D:/..., "Star Trek: Picard"), so no splitting it.
    Normalised, so an index made or imported with / still matches a path joined with \\ on Windows."""
    return sub_source.split(":")[0] if sub_source.startswith(("embedded:", "pgs:")) else os.path.normpath(sub_source)


def cached_ocr(jf_id, src):
    return os.path.exists(os.path.join(OCR_DIR, f"{jf_id}-{src[4:]}.srt"))


def index(index_path, series=None, movies=False, external_only=False, log=print, ocr_minutes=60, only=None):
    """ocr_minutes: at most this long reading picture tracks per pass (the rest wait for the next);
    only: just this Jellyfin id."""
    c = db.open_index(index_path)
    ocr_until = time.time() + ocr_minutes * 60
    pmap = db.path_map()
    jf_subs = jellyfin.subtitle_cache()
    stats = dict(items=0, indexed=0, unchanged=0, no_subs=0, errors=0, cues=0, from_jellyfin=0, ocr=0, ocr_later=0)
    use_ocr = pgs.available() and not external_only
    # Subtitle files first (cheap), then embedded tracks (ffmpeg reads the whole file), each newest
    # first: a pass cut short by its time cap has then done the most for what was just added.
    work = []
    for it in jellyfin.list_items(None, series, movies):
        if only and it["jf_id"] != jellyfin.norm_id(only):
            continue
        path = db.to_host(it["path"], pmap)
        work.append((it, path, subs.find_external(path)))
    work.sort(key=lambda x: not x[2])
    for it, path, ext in work:
        stats["items"] += 1
        if ext:
            source, mtime = ext, os.path.getmtime(ext)
        elif it["embedded"] and not external_only and os.path.exists(path):
            source, mtime = "embedded", os.path.getmtime(path)
        elif it.get("images") and use_ocr and os.path.exists(path):
            source, mtime = "pgs", os.path.getmtime(path)
        else:
            stats["no_subs"] += 1
            continue

        row = c.execute("SELECT id, sub_source, sub_mtime FROM items WHERE jf_id = ?",
                        (it["jf_id"],)).fetchone()
        was = _kind(row[1]) if row else None
        # (a file's own tracks, text or pictures, count as one source: the same file, unchanged)
        # (read by OCR: its kept text must still be there, so deleting it reads the track again)
        if row and (was == _kind(source) or {was, source} <= {"embedded", "pgs"}) and row[2] == mtime \
                and not (was == "pgs" and not cached_ocr(it["jf_id"], row[1])):
            stats["unchanged"] += 1
            continue

        # Candidates in order: the external file, then each embedded text track.
        cands = [(ext, os.path.splitext(ext)[1][1:].lower(), None)] if ext else []
        cands += [] if external_only else [(f"embedded:{x}", "ass" if cdc in ("ass", "ssa") else "srt", (x, cdc))
                  for x, cdc in it["embedded"]] if os.path.exists(path) else []
        # then English picture tracks (Blu-ray or DVD; both kept as "pgs:N"), by OCR: only when no
        # text track reads as dialogue
        cands += [(f"pgs:{x}", "srt", cdc) for x, cdc in it.get("images") or []] if use_ocr and os.path.exists(path) else []
        cues, err, fallback, later = None, None, None, False
        for src, kind, emb in cands:
            try:
                if src.startswith("pgs:"):
                    if not cached_ocr(it["jf_id"], src) and time.time() > ocr_until:
                        later = True                # this pass's OCR time is used up: next time
                        break
                    text = ocr_track(path, it["jf_id"], int(src[4:]), emb)
                    stats["ocr"] += 1
                    got = subs.tidy(subs.parse(text, kind))
                    if subs.is_dialogue(got, it["runtime_s"]) and subs.overlap(got) <= subs.MAX_OVERLAP:
                        cues, source = got, src
                        break
                    continue
                cached = emb and jellyfin.cached_subtitle(jf_subs, it["jf_id"], emb[0], emb[1], path)
                if cached:                  # Jellyfin already read the file for it: no second read
                    text = subs.read_text(cached)
                    stats["from_jellyfin"] += 1
                else:
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
        if cues is None and later:
            stats["ocr_later"] += 1
            continue
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
        log(f"  {label}: {len(cues)} cues ({source if source.startswith(('embedded', 'pgs')) else 'external'})")
    c.commit()
    c.close()                   # an open handle keeps Windows from moving or deleting the index
    return stats
