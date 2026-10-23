"""Phrase / word search over the cue index, with the neighbouring cues as context."""
import contextlib
import re
import sqlite3

from . import db


def spellings(w):
    """A word and its dropped-g twin: subtitles write "doing", "doin'" and "doin" for the same word.
    Only "-ing" with a vowel before it ("do-ing", "noth-ing"), so "thing" never also finds "thin";
    going the other way, "begin" also tries "beging", which matches nothing, which is harmless."""
    w = w.strip("'")
    if w.endswith("ing") and re.search(r"[aeiouy]", w[:-3]):
        return [w, w[:-1]]
    if w.endswith("in") and len(w) > 3 and re.search(r"[aeiouy]", w[:-2]):
        return [w, w + "g"]
    return [w]


NEAR_WORDS = 3       # a "close" match: at most this many other words among the searched ones
FALLBACK = 10        # fewer exact + close hits than this: also lines with all the words anywhere


def _parse(q):
    """Search-engine syntax: "quoted words" = that exact phrase (no other spellings); -word or
    -"words" = leave out lines with them; word* = any word starting so. Returns (words, phrases,
    excluded) where a word is (text, prefix)."""
    words, phrases, excluded = [], [], []
    for m in re.finditer(r'(-?)"([^"]*)"?|(-?)(\S+)', q.lower()):
        if m.group(2) is not None and m.group(0).lstrip("-").startswith('"'):
            ws = [w for w in re.findall(r"[\w']+", m.group(2)) if w.strip("'")]
            if ws:
                (excluded if m.group(1) else phrases).append([w.strip("'") for w in ws])
            continue
        raw = m.group(4) or ""
        for part in re.findall(r"[\w']+\*?", raw):
            prefix = part.endswith("*")
            w = part.rstrip("*").strip("'")
            if not w:
                continue
            if m.group(3):
                excluded.append([w])
            else:
                words.append((w, prefix))
    return words, phrases, excluded


def _tok(w, prefix):
    return '"' + w + '"' + (" *" if prefix else "")


def _variants(words):
    """Every spelling of the words in order ("doin" / "doing"), at most 8 combinations."""
    seqs = [[]]
    for w, prefix in words:
        opts = [(w, True)] if prefix else [(s, False) for s in spellings(w)]
        seqs = [q + [o] for q in seqs for o in (opts if len(seqs) < 8 else opts[:1])]
    return seqs


def fts_tiers(q, mode):
    """The FTS5 queries for a search, most exact first. phrase (default): the words in order,
    then close together (NEAR_WORDS), then anywhere in the line; quoted phrases must appear in
    all of them and excluded words in none. any: any of the words. raw: FTS5 syntax as typed."""
    if mode == "raw":
        return [q] if q.strip() else []
    words, phrases, excluded = _parse(q)
    if not words and not phrases:
        return []
    must = ['"' + " ".join(p) + '"' for p in phrases]
    tail = " NOT (" + " OR ".join('"' + " ".join(x) + '"' for x in excluded) + ")" if excluded else ""
    if mode != "phrase":
        terms = [_tok(s, p) for w, p in words for s in ([w] if p else spellings(w))] + must
        return ["(" + " OR ".join(terms) + ")" + tail]
    def both(core):
        return "(" + " AND ".join(["(" + core + ")"] + must) + ")" + tail if core else "(" + " AND ".join(must) + ")" + tail
    if not words:
        return [both("")]
    seqs = _variants(words)
    tiers = []
    # a phrase can only end on a prefix word ("an offer refu*"); one earlier needs the close tier
    if all(not p for (_, p) in words[:-1]):
        tiers.append(both(" OR ".join('"' + " ".join(w for w, _ in sq) + '"' + (" *" if sq[-1][1] else "") for sq in seqs)))
    if len(words) > 1:
        # FTS5's NEAR counts the tokens between the first and last word, the searched ones included
        span = NEAR_WORDS + len(words) - 2
        tiers.append(both(" OR ".join(f"NEAR({' '.join(_tok(w, p) for w, p in sq)}, {span})" for sq in seqs)))
        tiers.append(both(" AND ".join("(" + " OR ".join(_tok(s, p) for s in ([w] if p else spellings(w))) + ")"
                                       for w, p in words)))
    return tiers


def fts_query(q, mode):
    """The most exact query for a search (see fts_tiers), or None."""
    t = fts_tiers(q, mode)
    return t[0] if t else None


def _in_order(text, words):
    """The searched words appear in this order in the line (each word may carry an ending)."""
    toks = re.findall(r"\w+", text.lower())
    i = 0
    for t in toks:
        if i < len(words) and any(t.startswith(s[:max(3, len(s) - 2)]) for s in spellings(words[i])):
            i += 1
    return i == len(words)


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
            short=False, exact=False):
    """only / exclude: group keys (see GROUP) to keep or to drop. short: only lines that are
    little more than the search itself (clean reaction clips). Hits are ranked by tier (1 = the
    exact phrase, 2 = the words close together, 3 = all of them in the line); exact: tier 1 only."""
    c.row_factory = sqlite3.Row
    tiers = fts_tiers(q, mode)
    if not tiers:
        return []
    if exact:
        tiers = tiers[:1]
    filt, args = _filters(series, kind, only, exclude, short_limit(q) if short else None)

    hits = {}
    # Hits inside a single cue first, then phrases that straddle two cues. A pair hit is
    # redundant when either of its cues already matched on its own.
    # A two-line match only adds something for a phrase of 2+ words: a single word, or any one
    # of several, is always inside one line. Skipping it halves the cost of very common words.
    multi = not short and (mode == "raw" or (mode == "phrase" and len(re.findall(r"[\w']+", q)) > 1))
    need = (offset + limit) * 3
    for tier, match in enumerate(tiers, 1):
        if len(hits) >= need or (tier == 3 and len(hits) >= FALLBACK):
            break
        # a phrase across two lines only for the exact tier: close words across lines are noise
        for table, span in (("cues_fts", 1), ("pairs_fts", 2)) if multi and tier == 1 else (("cues_fts", 1),):
            for r in c.execute(f"""
                    SELECT q.id, q.item_id, q.idx, bm25({table}) AS score
                    FROM {table} f JOIN cues q ON q.id = f.rowid JOIN items i ON i.id = q.item_id
                    WHERE {table} MATCH ?{filt} ORDER BY score LIMIT ?""", [match, *args, need]):
                key = (r["item_id"], r["idx"])
                if key in hits or (span == 2 and (r["item_id"], r["idx"] + 1) in hits):
                    continue
                hits[key] = (tier, r["score"], span)

    words = [w for w, _ in _parse(q)[0]]
    texts = {}
    if len(words) > 1 and len(tiers) > 1:        # close matches with the words in order come first
        for (item_id, idx), (tier, score, span) in hits.items():
            if tier > 1:
                texts[(item_id, idx)] = " ".join(r[0] for r in c.execute(
                    "SELECT text FROM cues WHERE item_id = ? AND idx BETWEEN ? AND ? ORDER BY idx",
                    (item_id, idx, idx + span - 1)))
    rank = lambda kv: (kv[1][0], (kv[0] in texts) and not _in_order(texts[kv[0]], words), kv[1][1])  # noqa: E731
    out = []
    for (item_id, idx), (tier, score, span) in sorted(hits.items(), key=rank)[offset:offset + limit]:
        item = c.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        ctx = c.execute("SELECT idx, start_ms, end_ms, text FROM cues WHERE item_id = ? "
                        "AND idx BETWEEN ? AND ? ORDER BY idx",
                        (item_id, idx - 1, idx + span)).fetchall()
        hit = [x for x in ctx if idx <= x["idx"] < idx + span]
        out.append(dict(
            item=dict(item), score=score, tier=tier, item_id=item_id, idx=idx, span=span,
            start_ms=hit[0]["start_ms"], end_ms=hit[-1]["end_ms"],
            text=" ".join(x["text"] for x in hit),
            before=next((x["text"] for x in ctx if x["idx"] == idx - 1), None),
            after=next((x["text"] for x in ctx if x["idx"] == idx + span), None)))
    return out


FACET_MAX = 20000      # beyond this many hits, counting costs seconds and the numbers stop helping


def _facets(c, q, mode="phrase", kind=None, exact=False):
    """Hits per show / movie for the show filter, most first: [{key, label, kind, n}]. Counts the
    same hits search() returns: single-line matches, plus two-line matches neither of whose lines
    matched on its own. A very common query ("you": 1.2 M lines) gets every show and movie
    without counts instead (n = None), so the filter still works."""
    tiers = fts_tiers(q, mode)
    if not tiers:
        return []
    # count the tiers search() returns: exact + close, and "anywhere" only when those are few
    exact_q, match = tiers[0], tiers[0 if exact else min(1, len(tiers) - 1)]
    if not exact and len(tiers) > 2 and c.execute(
            "SELECT COUNT(*) FROM (SELECT rowid FROM cues_fts WHERE cues_fts MATCH ? LIMIT ?)", (match, FALLBACK)).fetchone()[0] < FALLBACK:
        match = tiers[2]
    c.row_factory = sqlite3.Row
    filt, args = _filters(kind=kind)
    many = c.execute("SELECT COUNT(*) FROM (SELECT rowid FROM cues_fts WHERE cues_fts MATCH ? LIMIT ?)",
                     (match, FACET_MAX + 1)).fetchone()[0] > FACET_MAX
    if many:
        rows = c.execute(f"""SELECT {GROUP} AS key, i.kind, i.series, i.title, i.year, NULL AS n, NULL AS e FROM items i
                             WHERE 1 = 1{filt} GROUP BY key ORDER BY i.kind, lower(coalesce(i.series, i.title))""", args).fetchall()
    else:
        rows = c.execute(f"""
        WITH one AS (SELECT rowid AS id FROM cues_fts WHERE cues_fts MATCH ?),
             ex AS (SELECT rowid AS id FROM cues_fts WHERE cues_fts MATCH ?),
             two AS (SELECT rowid AS id FROM pairs_fts WHERE pairs_fts MATCH ?
                     AND rowid NOT IN (SELECT id FROM one) AND rowid + 1 NOT IN (SELECT id FROM one)),
             hit AS (SELECT id, id IN (SELECT id FROM ex) AS exact FROM one UNION ALL SELECT id, 1 FROM two)
        SELECT {GROUP} AS key, i.kind, i.series, i.title, i.year, COUNT(*) AS n, SUM(hit.exact) AS e
        FROM hit JOIN cues q ON q.id = hit.id JOIN items i ON i.id = q.item_id
        WHERE 1 = 1{filt} GROUP BY key ORDER BY e DESC, n DESC, key""", [match, exact_q, exact_q, *args]).fetchall()
    out = []
    for r in rows:
        if r["kind"] == "episode":
            name = r["series"] or "(unnamed show)"
        else:
            name = f"{r['title']} ({r['year']})" if r["year"] else (r["title"] or "(untitled)")
        out.append(dict(key=r["key"], kind=r["kind"], label=name, n=r["n"], exact=r["e"]))
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
