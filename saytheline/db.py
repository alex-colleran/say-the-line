"""The cue index (our own SQLite file) and read-only access to Jellyfin's database."""
import json
import os
import shutil
import sqlite3
import subprocess

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id          INTEGER PRIMARY KEY,
  jf_id       TEXT UNIQUE NOT NULL,
  kind        TEXT NOT NULL,          -- episode | movie
  series      TEXT,
  season      INTEGER,
  episode     INTEGER,
  title       TEXT,
  year        INTEGER,
  path        TEXT NOT NULL,          -- host path (after SAYTHELINE_PATH_MAP)
  runtime_s   INTEGER,
  sub_source  TEXT,                   -- external file path, or 'embedded:<stream index>'
  sub_mtime   REAL,
  indexed_at  TEXT
);
CREATE TABLE IF NOT EXISTS cues (
  id        INTEGER PRIMARY KEY,
  item_id   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  idx       INTEGER NOT NULL,
  start_ms  INTEGER NOT NULL,
  end_ms    INTEGER NOT NULL,
  text      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cues_item ON cues(item_id, idx);
-- one row per cue
CREATE VIRTUAL TABLE IF NOT EXISTS cues_fts USING fts5(
  text, content='cues', content_rowid='id', tokenize='porter unicode61');
-- one row per (cue, next cue): subtitles split sentences across cues, so a phrase can
-- straddle two of them. rowid = the first cue's id.
CREATE VIRTUAL TABLE IF NOT EXISTS pairs_fts USING fts5(text, tokenize='porter unicode61');
"""


def open_index(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    c.executescript(SCHEMA)
    # item ids used to be stored dashed (as Jellyfin's DB has them); the API has none
    if c.execute("SELECT 1 FROM items WHERE jf_id LIKE '%-%' LIMIT 1").fetchone():
        c.execute("UPDATE items SET jf_id = replace(jf_id, '-', '')")
        c.commit()
    return c


def path_map():
    """SAYTHELINE_PATH_MAP='/media=/srv/media,/tv=/mnt/disk2/tv': when Jellyfin runs in a container
    (or on another machine), its file paths are its own view of the disks. Longest prefix wins."""
    raw = os.environ.get("SAYTHELINE_PATH_MAP", "")
    pairs = [p.split("=", 1) for p in raw.split(",") if "=" in p]
    return sorted(pairs, key=lambda p: -len(p[0]))


def to_host(path, pmap):
    for src, dst in pmap:
        if path == src or path.startswith(src.rstrip("/") + "/"):
            return dst + path[len(src):]
    return path


def jellyfin_query(db_path, sql):
    """Read-only query against Jellyfin's DB. A Docker Jellyfin's DB is often root-owned on the
    host, so fall back to `sudo -n sqlite3 -json` (passwordless sudo only) when it cannot be
    opened directly. The API source avoids all of this."""
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(sql)]
    except sqlite3.OperationalError:
        if not (shutil.which("sudo") and shutil.which("sqlite3")):
            raise RuntimeError(f"cannot read {db_path}: use the Jellyfin API instead (url + api_key)")
        r = subprocess.run(["sudo", "-n", "sqlite3", "-readonly", "-json", db_path, sql],
                           capture_output=True, text=True, check=True)
        return json.loads(r.stdout or "[]")
