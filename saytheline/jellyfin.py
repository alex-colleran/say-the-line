"""What is in the Jellyfin library: every episode or movie with its file path and its embedded
subtitle tracks. Two ways to ask, same result:

  API  (recommended): SAYTHELINE_JELLYFIN_URL + SAYTHELINE_JELLYFIN_KEY (Dashboard > API Keys).
       Works wherever Jellyfin runs, read-only, no file permissions needed.
  DB   : SAYTHELINE_JELLYFIN_DB = Jellyfin's jellyfin.db, opened read-only (Jellyfin 10.11 schema).

Item ids are kept as 32 lowercase hex digits (no dashes), whichever way they were read."""
import json
import os
import urllib.parse
import urllib.request

from . import db, subs

EPISODE = "MediaBrowser.Controller.Entities.TV.Episode"
MOVIE = "MediaBrowser.Controller.Entities.Movies.Movie"
PAGE = 500


def source():
    url, key = os.environ.get("SAYTHELINE_JELLYFIN_URL"), os.environ.get("SAYTHELINE_JELLYFIN_KEY")
    if url and key:
        return ("api", url.rstrip("/"), key)
    if os.environ.get("SAYTHELINE_JELLYFIN_DB"):
        return ("db", os.environ["SAYTHELINE_JELLYFIN_DB"])
    raise RuntimeError("Jellyfin is not configured: set [jellyfin] url + api_key (or db) in saytheline.toml")


def norm_id(i):
    return str(i).replace("-", "").lower()


def _rank(lang, forced, title):
    # Every embedded text track, best guess first. Language tags lie often enough (anime
    # releases tag the full English track "jpn" and the signs-only track "eng") that the
    # indexer tries them in order and keeps the first that reads as English dialogue.
    r = 0 if lang in subs.ENGLISH else 1 if lang in ("", "und") else 2
    if forced or "sign" in title or "song" in title:
        r += 10
    return r


def list_items(src=None, series=None, movies=False):
    """[{jf_id, kind, series, season, episode, title, year, path, runtime_s, embedded}] where
    embedded = [(stream index, codec)] of text subtitle tracks, best first."""
    src = src or source()
    items = _api_items(src[1], src[2], series, movies) if src[0] == "api" else _db_items(src[1], series, movies)
    for i in items:
        i["kind"] = "movie" if movies else "episode"
        i["embedded"] = [(x, c) for _, x, c in sorted(i.pop("streams"))]
    return items


def _api_get(url, key, path, params):
    req = urllib.request.Request(f"{url}{path}?{urllib.parse.urlencode(params)}",
                                 headers={"Authorization": f'MediaBrowser Token="{key}"', "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def _api_items(url, key, series, movies):
    params = {"IncludeItemTypes": "Movie" if movies else "Episode", "Recursive": "true", "IsMissing": "false",
              "Fields": "Path,MediaStreams", "EnableImages": "false", "EnableUserData": "false", "Limit": PAGE}
    if series:
        found = _api_get(url, key, "/Items", {"IncludeItemTypes": "Series", "Recursive": "true", "SearchTerm": series})
        ids = [s["Id"] for s in found.get("Items", []) if s.get("Name", "").lower() == series.lower()]
        if not ids:
            return []
        params["ParentId"] = ids[0]
    out, start = [], 0
    while True:
        page = _api_get(url, key, "/Items", dict(params, StartIndex=start))
        for it in page.get("Items", []):
            if not it.get("Path") or it.get("LocationType") == "Virtual":
                continue
            streams = [(_rank((s.get("Language") or "").lower(), s.get("IsForced"), (s.get("Title") or "").lower()),
                        s["Index"], (s.get("Codec") or "").lower())
                       for s in it.get("MediaStreams") or []
                       if s.get("Type") == "Subtitle" and not s.get("IsExternal")
                       and (s.get("Codec") or "").lower() in subs.TEXT_CODECS]
            ticks = it.get("RunTimeTicks")
            out.append(dict(jf_id=norm_id(it["Id"]), series=it.get("SeriesName"), season=it.get("ParentIndexNumber"),
                            episode=it.get("IndexNumber"), title=it.get("Name"), year=it.get("ProductionYear"),
                            path=it["Path"], runtime_s=ticks // 10_000_000 if ticks else None, streams=streams))
        start += PAGE
        if start >= page.get("TotalRecordCount", 0) or not page.get("Items"):
            return out


def _sql_str(s):
    return "'" + s.replace("'", "''") + "'"


def _db_items(jf_db, series, movies):
    where = [f"b.Type = '{MOVIE if movies else EPISODE}'", "b.IsVirtualItem = 0", "b.Path IS NOT NULL"]
    if series:
        where.append(f"b.SeriesName = {_sql_str(series)}")
    items = db.jellyfin_query(jf_db, f"""
        SELECT b.Id AS jf_id, b.SeriesName AS series, b.ParentIndexNumber AS season,
               b.IndexNumber AS episode, b.Name AS title, b.ProductionYear AS year,
               b.Path AS path, b.RunTimeTicks / 10000000 AS runtime_s
        FROM BaseItems b WHERE {' AND '.join(where)}""")
    ids = ",".join(_sql_str(i["jf_id"]) for i in items) or "''"
    streams = {}
    for s in db.jellyfin_query(jf_db, f"""
            SELECT ItemId AS jf_id, StreamIndex AS idx, lower(Codec) AS codec,
                   lower(ifnull(Language, '')) AS lang, IsForced AS forced,
                   lower(ifnull(Title, '')) AS title
            FROM MediaStreamInfos
            WHERE StreamType = 2 AND IsExternal = 0 AND ItemId IN ({ids})"""):
        if s["codec"] in subs.TEXT_CODECS:
            streams.setdefault(norm_id(s["jf_id"]), []).append((_rank(s["lang"], s["forced"], s["title"]), s["idx"], s["codec"]))
    for i in items:
        i["jf_id"] = norm_id(i["jf_id"])
        i["streams"] = streams.get(i["jf_id"], [])
    return items
