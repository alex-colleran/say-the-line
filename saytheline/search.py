"""Phrase / word search over the cue index, with the neighbouring cues as context."""
import contextlib
import re
import sqlite3

from . import db


def fts_query(q, mode):
    """phrase: the words in order (default). any: any of the words. raw: FTS5 syntax as typed."""
    if mode == "raw":
        return q
    words = re.findall(r"[\w']+", q.lower())
    if not words:
        return None
    quoted = ['"' + w.replace('"', '') + '"' for w in words]
    return '"' + " ".join(words) + '"' if mode == "phrase" else " OR ".join(quoted)


# A "group" is what the show filter lists: one per series ("s:<name>"), one per movie ("m:<item id>").
GROUP = "(CASE WHEN i.kind = 'episode' THEN 's:' || i.series ELSE 'm:' || i.id END)"


def _filters(series=None, kind=None, only=None, exclude=None, max_chars=None):
    filt, args = "", []
    if max_chars:                         # "just the phrase": lines barely longer than the search
        filt, args = " AND length(q.text) <= ?", [int(max_chars)]
    if series:
        filt, args = " AND i.series = ?", [series]
    if kind:
        filt, args = filt + " AND i.kind = ?", args + [kind]
    if only is not None:                  # [] = nothing selected = no hits
        filt += f" AND {GROUP} IN ({','.join('?' * len(only)) or 'NULL'})"
        args += list(only)
    if exclude:
        filt += f" AND {GROUP} NOT IN ({','.join('?' * len(exclude))})"
        args += list(exclude)
    return filt, args


def short_limit(q):
    """Character budget for short_lines: the query plus a few words ("Twins!", "Oh. Twins.")."""
    return len(q.strip()) + 18


def search(index_path, *a, **k):
    with contextlib.closing(sqlite3.connect(index_path)) as c:
        return _search(c, *a, **k)


def facets(index_path, *a, **k):
    with contextlib.closing(sqlite3.connect(index_path)) as c:
        return _facets(c, *a, **k)


def _search(c, q, mode="phrase", series=None, limit=50, offset=0, kind=None, only=None, exclude=None,
            short=False):
    """only / exclude: group keys (see GROUP) to keep or to drop. short: only lines that are
    little more than the search itself (clean reaction clips)."""
    c.row_factory = sqlite3.Row
    match = fts_query(q, mode)
    if not match:
        return []
    filt, args = _filters(series, kind, only, exclude, short_limit(q) if short else None)

    hits = {}
    # Hits inside a single cue first, then phrases that straddle two cues. A pair hit is
    # redundant when either of its cues already matched on its own.
    # A two-line match only adds something for a phrase of 2+ words: a single word, or any one
    # of several, is always inside one line. Skipping it halves the cost of very common words.
    multi = not short and (mode == "raw" or (mode == "phrase" and len(re.findall(r"[\w']+", q)) > 1))
    for table, span in (("cues_fts", 1), ("pairs_fts", 2)) if multi else (("cues_fts", 1),):
        for r in c.execute(f"""
                SELECT q.id, q.item_id, q.idx, bm25({table}) AS score
                FROM {table} f JOIN cues q ON q.id = f.rowid JOIN items i ON i.id = q.item_id
                WHERE {table} MATCH ?{filt} ORDER BY score LIMIT ?""", [match, *args, (offset + limit) * 3]):
            key = (r["item_id"], r["idx"])
            if span == 2 and (key in hits or (r["item_id"], r["idx"] + 1) in hits):
                continue
            hits.setdefault(key, (r["score"], span))

    out = []
    for (item_id, idx), (score, span) in sorted(hits.items(), key=lambda kv: kv[1][0])[offset:offset + limit]:
        item = c.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        ctx = c.execute("SELECT idx, start_ms, end_ms, text FROM cues WHERE item_id = ? "
                        "AND idx BETWEEN ? AND ? ORDER BY idx",
                        (item_id, idx - 1, idx + span)).fetchall()
        hit = [x for x in ctx if idx <= x["idx"] < idx + span]
        out.append(dict(
            item=dict(item), score=score, item_id=item_id, idx=idx, span=span,
            start_ms=hit[0]["start_ms"], end_ms=hit[-1]["end_ms"],
            text=" ".join(x["text"] for x in hit),
            before=next((x["text"] for x in ctx if x["idx"] == idx - 1), None),
            after=next((x["text"] for x in ctx if x["idx"] == idx + span), None)))
    return out


FACET_MAX = 20000      # beyond this many hits, counting costs seconds and the numbers stop helping


def _facets(c, q, mode="phrase", kind=None):
    """Hits per show / movie for the show filter, most first: [{key, label, kind, n}]. Counts the
    same hits search() returns: single-line matches, plus two-line matches neither of whose lines
    matched on its own. A very common query ("you": 1.2 M lines) gets every show and movie
    without counts instead (n = None), so the filter still works."""
    match = fts_query(q, mode)
    if not match:
        return []
    c.row_factory = sqlite3.Row
    filt, args = _filters(kind=kind)
    many = c.execute("SELECT COUNT(*) FROM (SELECT rowid FROM cues_fts WHERE cues_fts MATCH ? LIMIT ?)",
                     (match, FACET_MAX + 1)).fetchone()[0] > FACET_MAX
    if many:
        rows = c.execute(f"""SELECT {GROUP} AS key, i.kind, i.series, i.title, i.year, NULL AS n FROM items i
                             WHERE 1 = 1{filt} GROUP BY key ORDER BY i.kind, lower(coalesce(i.series, i.title))""", args).fetchall()
    else:
        rows = c.execute(f"""
        WITH one AS (SELECT rowid AS id FROM cues_fts WHERE cues_fts MATCH ?),
             two AS (SELECT rowid AS id FROM pairs_fts WHERE pairs_fts MATCH ?
                     AND rowid NOT IN (SELECT id FROM one) AND rowid + 1 NOT IN (SELECT id FROM one)),
             hit AS (SELECT id FROM one UNION ALL SELECT id FROM two)
        SELECT {GROUP} AS key, i.kind, i.series, i.title, i.year, COUNT(*) AS n
        FROM hit JOIN cues q ON q.id = hit.id JOIN items i ON i.id = q.item_id
        WHERE 1 = 1{filt} GROUP BY key ORDER BY n DESC, key""", [match, match, *args]).fetchall()
    out = []
    for r in rows:
        if r["kind"] == "episode":
            name = r["series"] or "(unnamed show)"
        else:
            name = f"{r['title']} ({r['year']})" if r["year"] else (r["title"] or "(untitled)")
        out.append(dict(key=r["key"], kind=r["kind"], label=name, n=r["n"]))
    return out


def fmt_ms(ms):
    s = ms // 1000
    return f"{s // 3600:d}:{s // 60 % 60:02d}:{s % 60:02d}"


def label(item):
    if item["kind"] == "episode":
        return f"{item['series']} S{item['season'] or 0:02d}E{item['episode'] or 0:02d} \"{item['title']}\""
    t, y = item["title"] or "", item["year"]
    return t if not y or t.endswith(f"({y})") else f"{t} ({y})"     # some titles already carry the year


def context(index_path, item_id, idx, before=4, after=4):
    with contextlib.closing(sqlite3.connect(index_path)) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(
            "SELECT idx, start_ms, end_ms, text FROM cues WHERE item_id = ? AND idx BETWEEN ? AND ? "
            "ORDER BY idx", (item_id, idx - before, idx + after))]
