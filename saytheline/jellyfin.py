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


def subtitle_cache():
    """Jellyfin's folder of already-extracted embedded subtitles, or None."""
    d = os.environ.get("SAYTHELINE_JELLYFIN_SUBS")
    if not d and os.environ.get("SAYTHELINE_JELLYFIN_DB"):
        d = os.path.join(os.path.dirname(os.environ["SAYTHELINE_JELLYFIN_DB"]), "subtitles")
    return d if d and os.path.isdir(d) else None


def cached_subtitle(cache, jf_id, stream_index, codec, video_path):
    """The file Jellyfin extracted for this stream (subtitles/<id[:2]>/<id with dashes>/<index>.<ext>),
    when it is at least as new as the video: an upgraded file can number its streams differently."""
    if not cache:
        return None
    h = norm_id(jf_id)
    guid = f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    p = os.path.join(cache, h[:2], guid, f"{stream_index}.{'ass' if codec in ('ass', 'ssa') else 'srt'}")
    try:
        if os.path.getsize(p) > 0 and os.path.getmtime(p) >= os.path.getmtime(video_path):
            return p
    except OSError:
        pass
    return None


def norm_id(i):
    return str(i).replace("-", "").lower()


def _rank(lang, forced, title):
    # Every embedded text track, best guess first. Language tags lie often enough (anime
    # releases tag the full English track "jpn" and the signs-only track "eng") that the
    # indexer tries them in order and keeps the first that reads as English dialogue.
    r = 0 if lang in subs.ENGLISH else 1 if lang in ("", "und") else 2
    if forced or "sign" in title or "song" in title or "commentary" in title:
        r += 10
    return r


def list_items(src=None, series=None, movies=False):
    """[{jf_id, kind, series, season, episode, title, year, path, runtime_s, added, embedded, images}]
    where embedded = [(stream index, codec)] of text subtitle tracks, best first, and images = the
    stream indexes of English (or untagged) Blu-ray picture tracks, best first, for OCR. Newest added first, so a
    pass cut short by its time cap has done the media the owner just got."""
    src = src or source()
    items = _api_items(src[1], src[2], series, movies) if src[0] == "api" else _db_items(src[1], series, movies)
    for i in items:
        i["kind"] = "movie" if movies else "episode"
        st = sorted(i.pop("streams"))
        i["embedded"] = [(x, c) for _, x, c in st if c in subs.TEXT_CODECS]
        i["images"] = [x for r, x, c in st if c in subs.IMAGE_CODECS and r < 2]
    items.sort(key=lambda i: i.get("added") or "", reverse=True)
    return items


def _api_get(url, key, path, params):
    req = urllib.request.Request(f"{url}{path}?{urllib.parse.urlencode(params)}",
                                 headers={"Authorization": f'MediaBrowser Token="{key}"', "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def _api_items(url, key, series, movies):
    params = {"IncludeItemTypes": "Movie" if movies else "Episode", "Recursive": "true", "IsMissing": "false",
              "Fields": "Path,MediaStreams,DateCreated", "EnableImages": "false", "EnableUserData": "false", "Limit": PAGE}
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
                       and (s.get("Codec") or "").lower() in subs.TEXT_CODECS | subs.IMAGE_CODECS]
            ticks = it.get("RunTimeTicks")
            out.append(dict(jf_id=norm_id(it["Id"]), series=it.get("SeriesName"), season=it.get("ParentIndexNumber"),
                            episode=it.get("IndexNumber"), title=it.get("Name"), year=it.get("ProductionYear"),
                            path=it["Path"], runtime_s=ticks // 10_000_000 if ticks else None,
                            added=it.get("DateCreated"), streams=streams))
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
               b.Path AS path, b.RunTimeTicks / 10000000 AS runtime_s,
               b.DateCreated AS added
        FROM BaseItems b WHERE {' AND '.join(where)}""")
    ids = ",".join(_sql_str(i["jf_id"]) for i in items) or "''"
    streams = {}
    for s in db.jellyfin_query(jf_db, f"""
            SELECT ItemId AS jf_id, StreamIndex AS idx, lower(Codec) AS codec,
                   lower(ifnull(Language, '')) AS lang, IsForced AS forced,
                   lower(ifnull(Title, '')) AS title
            FROM MediaStreamInfos
            WHERE StreamType = 2 AND IsExternal = 0 AND ItemId IN ({ids})"""):
        if s["codec"] in subs.TEXT_CODECS | subs.IMAGE_CODECS:
            streams.setdefault(norm_id(s["jf_id"]), []).append((_rank(s["lang"], s["forced"], s["title"]), s["idx"], s["codec"]))
    for i in items:
        i["jf_id"] = norm_id(i["jf_id"])
        i["streams"] = streams.get(i["jf_id"], [])
    return items


def _guid(h):
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}".upper()


def cover(jf_id):
    """The poster for an item: a movie's own, or for an episode its show's (an episode's own
    image is a still from it). Returns a local image path, image bytes, or None."""
    src, h = source(), norm_id(jf_id)
    if src[0] == "api":
        url, key = src[1], src[2]
        it = (_api_get(url, key, "/Items", {"Ids": h, "Fields": "SeriesId"}).get("Items") or [{}])[0]
        req = urllib.request.Request(f"{url}/Items/{it.get('SeriesId') or h}/Images/Primary?maxWidth=600",
                                     headers={"Authorization": f'MediaBrowser Token="{key}"'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except OSError:
            return None
    jf_db = src[1]
    row = db.jellyfin_query(jf_db, f"SELECT SeriesId FROM BaseItems WHERE Id = {_sql_str(_guid(h))}")
    owner = (row[0]["SeriesId"] if row and row[0].get("SeriesId") else _guid(h))
    img = db.jellyfin_query(jf_db, f"SELECT Path FROM BaseItemImageInfos WHERE ItemId = {_sql_str(owner)} "
                                   "AND ImageType = 0 ORDER BY Width DESC LIMIT 1")
    if not img:
        return None
    p = img[0]["Path"]
    # Jellyfin's own metadata folder sits beside the folder holding jellyfin.db (<data>/data/ and
    # <data>/metadata/), whatever path the container gave it; art saved with the media is path-mapped
    i = p.find("/metadata/")
    for cand in ([os.path.join(os.path.dirname(os.path.dirname(jf_db)), p[i + 1:])] if i >= 0 else []) + [db.to_host(p, db.path_map()), p]:
        if os.path.isfile(cand):
            return cand
    return None
