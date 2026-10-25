"""Favourites and tags: hearted moments (a line or a cut, re-openable in the editor) and hearted
rendered clips, each with free-form tags for projects. Kept in its own small database
(data/library.db) so rebuilding the cue index never touches it.

Every clip worked on in the editor is also kept as a 'recent' draft (the last RECENT_KEEP), so work
is never lost without anyone tapping save. A moment or draft carries the whole editor state as JSON
(cut, caption look and style, the edited transcript, output settings).

A 'draft' is a hearted Say Anything, supercut or Studio that isn't rendered yet: its state is the whole
sheet (pieces and takes; hits, ticks and checks; or a Studio's clips in order), with item_id/start_ms
only for its thumbnail."""
import contextlib
import datetime
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS saved (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,              -- moment | clip | recent | draft
  item_id    INTEGER,
  idx        INTEGER,                    -- cue index of the line that was found (highlighting)
  media_kind TEXT,                       -- episode | movie
  label      TEXT,                       -- "Some Show S01E07" / "Some Film (1999)"
  title      TEXT,                       -- episode title
  text       TEXT,                       -- the line(s)
  start_ms   INTEGER, end_ms INTEGER,    -- the subtitle line's own times
  in_ms      INTEGER, out_ms INTEGER,    -- the cut, if one was set
  offset_ms  INTEGER DEFAULT 0,          -- subtitle correction found by snap
  file       TEXT,                       -- rendered clip file name (kind = clip)
  note       TEXT,
  state      TEXT,                       -- editor state (JSON): cut, captions, transcript, output
  created    TEXT, updated TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS saved_moment ON saved(item_id, start_ms) WHERE kind = 'moment';
CREATE UNIQUE INDEX IF NOT EXISTS saved_clip ON saved(file) WHERE kind = 'clip';
CREATE UNIQUE INDEX IF NOT EXISTS saved_recent ON saved(item_id, start_ms) WHERE kind = 'recent';
CREATE TABLE IF NOT EXISTS tags (
  saved_id INTEGER NOT NULL REFERENCES saved(id) ON DELETE CASCADE,
  tag      TEXT NOT NULL,
  PRIMARY KEY (saved_id, tag)
);
"""
FIELDS = ("kind", "item_id", "idx", "media_kind", "label", "title", "text", "start_ms", "end_ms",
          "in_ms", "out_ms", "offset_ms", "file", "note", "state")
KINDS = ("moment", "clip", "recent", "draft")
RECENT_KEEP = 40
_lock = threading.Lock()


class Library:
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        with self._db() as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE name = 'saved'").fetchone() and \
                    "state" not in [r[1] for r in c.execute("PRAGMA table_info(saved)")]:
                c.execute("ALTER TABLE saved ADD COLUMN state TEXT")    # editor state, JSON
            c.executescript(SCHEMA)

    @contextlib.contextmanager
    def _db(self):
        """One connection per call: commit on success, roll back on error, always close."""
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            with c:
                yield c
        finally:
            c.close()

    @staticmethod
    def _clean_tags(tags):
        out = []
        for t in tags or []:
            t = " ".join(str(t).strip().lower().split())[:40]
            if t and t not in out:
                out.append(t)
        return out[:20]

    def _row(self, c, sid):
        r = c.execute("SELECT * FROM saved WHERE id = ?", (sid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["tags"] = [t for (t,) in c.execute("SELECT tag FROM tags WHERE saved_id = ? ORDER BY tag", (sid,))]
        return d

    def save(self, data):
        """Create, or update the existing entry for the same moment (item + line start) or file."""
        kind = data.get("kind") if data.get("kind") in KINDS else "moment"
        vals = {k: data.get(k) for k in FIELDS}
        vals["kind"] = kind
        now = datetime.datetime.now().isoformat(timespec="seconds")
        with _lock, self._db() as c:
            if kind == "clip":
                old = c.execute("SELECT id FROM saved WHERE kind='clip' AND file = ?", (vals["file"],)).fetchone()
            elif kind == "draft":
                old = None                # two drafts may start on the same line
            else:
                old = c.execute("SELECT id FROM saved WHERE kind = ? AND item_id = ? AND start_ms = ?",
                                (kind, vals["item_id"], vals["start_ms"])).fetchone()
            if old:
                sid = old[0]
                sets = [k for k in FIELDS if data.get(k) is not None and k != "kind"]
                if sets:
                    c.execute(f"UPDATE saved SET {', '.join(k + ' = ?' for k in sets)}, updated = ? WHERE id = ?",
                              [vals[k] for k in sets] + [now, sid])
            else:
                sid = c.execute(f"INSERT INTO saved ({', '.join(FIELDS)}, created, updated) VALUES "
                                f"({', '.join('?' * len(FIELDS))}, ?, ?)", [vals[k] for k in FIELDS] + [now, now]).lastrowid
            if "tags" in data and kind != "recent":
                self._set_tags(c, sid, data["tags"])
            if kind == "recent":
                c.execute("DELETE FROM saved WHERE kind = 'recent' AND id NOT IN "
                          "(SELECT id FROM saved WHERE kind = 'recent' ORDER BY updated DESC, id DESC LIMIT ?)", (RECENT_KEEP,))
            return self._row(c, sid)

    def draft(self, item_id, start_ms):
        """The saved state to reopen a moment with: the hearted moment if there is one, else its draft."""
        with self._db() as c:
            r = c.execute("SELECT id FROM saved WHERE item_id = ? AND start_ms = ? AND kind IN ('moment', 'recent') "
                          "AND state IS NOT NULL ORDER BY kind = 'moment' DESC, updated DESC LIMIT 1",
                          (item_id, start_ms)).fetchone()
            return self._row(c, r[0]) if r else None

    def get(self, sid):
        with self._db() as c:
            return self._row(c, sid)

    def _set_tags(self, c, sid, tags):
        c.execute("DELETE FROM tags WHERE saved_id = ?", (sid,))
        c.executemany("INSERT INTO tags (saved_id, tag) VALUES (?, ?)", [(sid, t) for t in self._clean_tags(tags)])

    def update(self, sid, data):
        with _lock, self._db() as c:
            if not c.execute("SELECT 1 FROM saved WHERE id = ?", (sid,)).fetchone():
                return None
            sets = [k for k in ("note", "in_ms", "out_ms", "offset_ms", "text", "state", "label", "item_id", "start_ms", "end_ms")
                    if k in data]
            if sets:
                c.execute(f"UPDATE saved SET {', '.join(k + ' = ?' for k in sets)}, updated = ? WHERE id = ?",
                          [data[k] for k in sets] + [datetime.datetime.now().isoformat(timespec="seconds"), sid])
            if "tags" in data:
                self._set_tags(c, sid, data["tags"])
            return self._row(c, sid)

    def delete(self, sid):
        with _lock, self._db() as c:
            c.execute("DELETE FROM saved WHERE id = ?", (sid,))

    def list(self, tag=None, kind=None, with_state=False):
        """Saved entries (drafts only when asked for by kind); the bulky editor state is left out
        unless with_state, and fetched per entry when one is opened."""
        q, args = "SELECT id FROM saved WHERE 1=1", []
        if kind in KINDS:
            q += " AND kind = ?"
            args.append(kind)
        else:
            q += " AND kind != 'recent'"
        if tag:
            q += " AND id IN (SELECT saved_id FROM tags WHERE tag = ?)"
            args.append(tag)
        with self._db() as c:
            rows = [self._row(c, sid) for (sid,) in c.execute(q + " ORDER BY updated DESC", args).fetchall()]
        for r in rows:
            st = r.pop("state", None)
            r["has_state"] = bool(st)
            if with_state:
                r["state"] = st
        return rows

    def tags(self):
        with self._db() as c:
            return [dict(r) for r in c.execute("SELECT tag, COUNT(*) AS n FROM tags GROUP BY tag ORDER BY n DESC, tag")]
