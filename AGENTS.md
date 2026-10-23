# AGENTS.md: working on Say the Line

Context for AI coding agents (Claude Code reads this through `CLAUDE.md`). Humans: see README.md.

## What it is
A self-hosted web app: search a Jellyfin library by its subtitles, then preview, trim, caption and
render clips / GIFs / stills / supercuts. Single user, runs next to the media.

## Layout
```
saytheline/
  config.py      settings: env SAYTHELINE_* > saytheline.toml > defaults (loaded on package import)
  jellyfin.py    list items + embedded subtitle tracks (Jellyfin API, or its SQLite DB read-only)
  db.py          our index schema (SQLite FTS5: cues_fts, pairs_fts), path_map
  subs.py        find/parse/clean subtitles (srt/ass/vtt, embedded via ffmpeg), English check
  index.py       incremental indexer (re-reads an item only if its subtitle source/mtime changed)
  search.py      FTS queries, context lines
  media.py       ffmpeg: probe, preview, geometry, caption ASS/SRT writers, render, still, supercut
  snap.py        speech alignment (snap to speech, word timing, repeated-line resolution)
  shots.py       camera-cut detection (tiny grey frames, calibrated rule), per-item cache, fit/whole_shot
  stt_worker.py  faster-whisper child process (+ energy-onset refinement of word starts)
  pos_worker.py  spaCy child process: emphasis tier per word (Poster look)
  poster.py      Poster layout with real font metrics (Pillow)
  library.py     favourites, tags, drafts (data/library.db; separate from the index)
  web.py         HTTP server (stdlib ThreadingHTTPServer) + JSON API + job queue
  doctor.py      `python -m saytheline doctor`
  mcp_server.py  MCP server for agents (`python -m saytheline mcp`): a thin client of web.py's HTTP API;
                 the only module that needs a non-stdlib package (mcp, see requirements-mcp.txt)
  static/index.html   the whole UI: one file, vanilla JS, no build step
  fonts/         bundled caption fonts (open licences) - libass uses them via fontsdir
```

## Run / check
```
python -m saytheline doctor               # environment check
python -m saytheline index --series "Name"  # small index to work against
python -m saytheline serve                # http://127.0.0.1:8765/
python -m unittest discover -s tests      # tests (no media or Jellyfin needed)
sed -n '/<script>/,/<\/script>/p' saytheline/static/index.html | sed '1d;$d' > /tmp/ui.js && node --check /tmp/ui.js   # UI syntax
```

## Rules
- **Media and Jellyfin are read-only.** Never write next to media files or into Jellyfin's DB.
  Everything Say the Line writes goes under the data dir (`config.data_dir()`).
- **One heavy job at a time.** Renders, snaps, transcriptions, stills and supercuts all take
  `web.RENDER_SLOT`. Don't add parallel ffmpeg/whisper work; heavy children go through `config.nice()`.
- **Never run two indexers at once** (they collide on `items.jf_id`).
- **The web server stays standard-library only.** Heavy dependencies (faster-whisper, spaCy,
  demucs) run in child processes using `config.stt_python()` / `config.demucs()`, and every one of
  them is optional: features degrade with a clear message, never a crash.
- **Preview = render.** Shot trimming exists twice too: `shots.fit` / `whole_shot` and JS
  `fitShots` / `shotTargets`. Caption logic exists twice: Python (`media.word_ass`, `chunk_words`,
  `normalize_words`, `_pop`) and JS (`drawWords`, `chunkWords`, `normalizeWords`, `popAttr`). Change
  both together; the comments mark the pairs (`= media.xyz`).
- **No private data in the repo**: no hostnames, IPs, personal paths, API keys or library contents.
  Machine-specific settings belong in `saytheline.toml` (gitignored). Screenshots only ever show a
  public-domain film from a one-title demo index: after a visible UI change, retake them with
  `docs/screenshots/take.py` (steps in docs/screenshots/README.md).
- **No login exists**: the default bind is 127.0.0.1; don't change that default.
- **MCP tools are for models**: few tools, concise text + structured fields, readable ids
  ("51:99", "c1a2b3c4"), errors that say what to change (raise `ToolError`). Keep them talking to
  the web API rather than importing web.py, so jobs stay in one queue. See docs/mcp.md.
- Keep the code style: short modules, docstrings that explain *why*, comments only where the reason
  isn't obvious. The UI is phone-first (≥40 px tap targets, works at 360 px wide).

## Gotchas learnt the hard way
- GIF palettegen waits for end of input: use input-side `-t`.
- Drop chapter/data streams from previews (`-map_chapters -1 -dn`).
- Anime releases mislabel subtitle languages: the indexer tries every text track and keeps the
  first that reads as English dialogue (`subs.is_dialogue`).
- Subtitle files can be tens of seconds off: snap tries ±5 s then ±45 s, picks the occurrence whose
  neighbouring lines also match, and remembers the offset per item (`data/offsets.json`).
- Whisper starts a word where the previous one ended; `stt_worker.refine_starts` moves starts later
  to the real voice onset so captions never appear before speech.
- A systemd `Type=oneshot` job is capped with `TimeoutStartSec`, not `RuntimeMaxSec`.
- Input-side `-t` can let one frame past the out point through: renders start their video chain
  with `trim=end=` so a clip ending on a camera cut never shows the next shot.
- Raw frames + showinfo times need `-fps_mode passthrough`, or ffmpeg inserts duplicate frames on
  files with uneven timestamps and the pairing shifts.
- Encoders default to a one-tick-per-frame time base, which moves frames by up to a frame: the
  preview sets `-enc_time_base:v 1:90000` so the editor can show exact frames.
- Old XviD AVIs (packed B-frames) have no frame timestamps; ffmpeg guesses them per seek point,
  so frame-exact work there is ±1 frame (`shots.loose_timing`).
- Don't tune the cut detector without labelled frames from varied titles (dark drama, anime,
  action): ffmpeg's scene score looked right on one show and failed on the others.
- `tmp` dirs may be tmpfs: use `shutil.move`, not `os.replace`, into the data dir.
