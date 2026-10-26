"""`python -m saytheline import SRC [--map OLD=NEW,...]`: use another machine's index here.

When Jellyfin and the disks are on another box, indexing from here means reading every video with
embedded subtitles across the network: hours. The box with the disks does it far faster, so copy its
data/cues.db (with data/ocr/ and offsets.json beside it, if wanted) and import it:

- the index is replaced whole, never merged: cue ids, both FTS tables and items.jf_id stay
  consistent only as a set (the old one is kept as cues.db.before-import);
- items.path and external sub_source are moved from the other machine's paths to this one's with
  --map (its HOST paths, which are not Jellyfin's: path_map does not apply to them);
- what is keyed by the local item id is carried over (Library entries, by Jellyfin id) or dropped to
  rebuild itself (previews, thumbnails, shots, movie covers, offsets unless they came along).

Later `index` runs read only what is new, as long as file mtimes read the same here as there (they
do over SMB)."""
import glob
import json
import os
import shutil
import sqlite3

from . import config, db

ITEM_KEYS = ("item", "item_id")          # where Library state JSON names an index item


def parse_map(spec):
    """'/mnt/media=M:,/srv/x=S:' -> [(old, new)], longest first (as SAYTHELINE_PATH_MAP)."""
    pairs = [tuple(p.split("=", 1)) for p in (spec or "").split(",") if "=" in p]
    return sorted(pairs, key=lambda p: -len(p[0]))


def remap(c, pmap):
    """Move items.path and external sub_source onto this machine. Returns how many rows changed."""
    n = 0
    for i, path, src in c.execute("SELECT id, path, sub_source FROM items").fetchall():
        p2 = db.to_host(path, pmap)
        s2 = src if not src or src.startswith(("embedded:", "pgs:")) else db.to_host(src, pmap)
        if (p2, s2) != (path, src):
            c.execute("UPDATE items SET path = ?, sub_source = ? WHERE id = ?", (p2, s2, i))
            n += 1
    return n


def _ids(path):
    if not os.path.exists(path):
        return {}
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return dict(c.execute("SELECT id, jf_id FROM items"))
    finally:
        c.close()


def _swap(v, move):
    """Library state JSON with its item ids moved (by `move`), wherever ITEM_KEYS hold one."""
    if isinstance(v, dict):
        return {k: (move.get(x, x) if k in ITEM_KEYS and isinstance(x, int) else _swap(x, move)) for k, x in v.items()}
    if isinstance(v, list):
        return [_swap(x, move) for x in v]
    return v


def relink_library(lib_path, old_ids, new_ids):
    """Saved moments, clips and drafts name index item ids: carry them over by Jellyfin id.
    Returns (moved, lost): lost ones point at titles the new index doesn't have."""
    if not os.path.exists(lib_path):
        return 0, 0
    by_jf = {jf: i for i, jf in new_ids.items()}
    move = {i: by_jf[jf] for i, jf in old_ids.items() if jf in by_jf}
    c = sqlite3.connect(lib_path)
    moved = lost = 0
    with c:
        for sid, item, state in c.execute("SELECT id, item_id, state FROM saved").fetchall():
            if item is None:
                continue
            if item not in move:             # its old id may name another title now: unlink it
                c.execute("UPDATE saved SET item_id = NULL WHERE id = ?", (sid,))
                lost += 1
                continue
            new_state = json.dumps(_swap(json.loads(state), move)) if state else state
            # (the unique indexes on (item_id, start_ms) can't clash: every id moves at once below)
            c.execute("UPDATE saved SET item_id = ?, state = ? WHERE id = ?", (-move[item], new_state, sid))
            moved += 1
        c.execute("UPDATE saved SET item_id = -item_id WHERE item_id < 0")
    c.close()
    return moved, lost


def main(src, mapping=None, index_path=None, log=print):
    data = config.data_dir()
    index_path = index_path or os.environ.get("SAYTHELINE_INDEX", os.path.join(data, "cues.db"))
    folder = src if os.path.isdir(src) else os.path.dirname(os.path.abspath(src))
    src_db = os.path.join(src, "cues.db") if os.path.isdir(src) else src
    if not os.path.isfile(src_db):
        raise SystemExit(f"no index at {src_db}")
    if os.path.abspath(src_db) == os.path.abspath(index_path):
        raise SystemExit("that is this machine's own index")
    pmap = parse_map(mapping)

    tmp = index_path + ".import"
    for p in glob.glob(tmp + "*"):
        os.remove(p)
    s = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    d = sqlite3.connect(tmp)
    s.backup(d)                              # a consistent copy, even of a database in use
    s.close()
    if d.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit(f"{src_db} failed SQLite's quick_check: copy it again")
    with d:
        moved = remap(d, pmap)
    items, cues = d.execute("SELECT (SELECT count(*) FROM items), (SELECT count(*) FROM cues)").fetchone()
    sample = [p for (p,) in d.execute("SELECT path FROM items ORDER BY random() LIMIT 40")]
    d.close()
    found = sum(os.path.exists(p) for p in sample)
    if sample and not found:
        os.remove(tmp)
        raise SystemExit(f"none of {len(sample)} sample files exist here, e.g. {sample[0]}: "
                         "--map the other machine's path prefixes to this one's (OLD=NEW,...)")

    old_ids = _ids(index_path)
    if os.path.exists(index_path):
        try:
            os.replace(index_path, index_path + ".before-import")
        except PermissionError:
            os.remove(tmp)
            raise SystemExit("the index is in use: stop `serve` (and any `index`) first, then import again")
        for ext in ("-wal", "-shm"):
            if os.path.exists(index_path + ext):
                os.replace(index_path + ext, index_path + ".before-import" + ext)
    os.replace(tmp, index_path)
    db.open_index(index_path).close()        # this machine's journal mode (WAL)
    new_ids = _ids(index_path)

    # what was keyed by the old local item ids
    relinked, lost = relink_library(os.path.join(data, "library.db"), old_ids, new_ids)
    base = os.path.dirname(index_path)
    for sub in ("previews", "thumbs", "shots"):
        shutil.rmtree(os.path.join(base, sub), ignore_errors=True)
    for p in glob.glob(os.path.join(base, "covers", "m-*")):
        os.remove(p)
    offsets = os.path.join(base, "offsets.json")
    if os.path.isfile(os.path.join(folder, "offsets.json")):
        shutil.copyfile(os.path.join(folder, "offsets.json"), offsets)
    elif os.path.exists(offsets):
        os.remove(offsets)
    ocr_n = 0
    if os.path.isdir(os.path.join(folder, "ocr")) and os.path.abspath(os.path.join(folder, "ocr")) != os.path.abspath(os.path.join(data, "ocr")):
        os.makedirs(os.path.join(data, "ocr"), exist_ok=True)
        for f in glob.glob(os.path.join(folder, "ocr", "*.srt")):
            to = os.path.join(data, "ocr", os.path.basename(f))
            if not os.path.exists(to):
                shutil.copy2(f, to)              # (copy2 keeps the mtime the indexer compares)
                ocr_n += 1

    log(f"imported {items} titles, {cues} lines; {moved} paths moved by --map; "
        f"{found} of {len(sample)} sampled files found here")
    log(f"Library: {relinked} saved entries carried over" + (f", {lost} point at titles not in this index" if lost else ""))
    log(f"OCR'd subtitles copied: {ocr_n}; the old index is {index_path}.before-import")
    return dict(items=items, cues=cues, moved=moved, found=found, sampled=len(sample),
                relinked=relinked, lost=lost, ocr=ocr_n)
