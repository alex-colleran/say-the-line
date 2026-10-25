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
  kinetic.py     Kinetic look (the original, calm motion): Poster's layout as justified stacks + lockups
  intense.py     Intense look: Kinetic with every effect (drop, wipe, type, bars, loudness, exits)
  sayany.py      Say Anything: plan a sentence into heard takes, measured cut points
  slowmo.py      slow motion: time map, caption re-timing, RIFE per camera shot, the slowed master
  music.py       Studio songs: uploads, beat grid, joins on the beat, the ducked mix
  beat_worker.py tempo, beats and bar starts of a song (numpy only, runs in the speech Python)
  ramp.py        speed ramps on the beat: the curve, per-frame source times, the ramped segment
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
  `normalize_words`, `_pop`) and JS (`drawWords`, `chunkWords`, `normalizeWords`, `popAttr`). Kinetic's
  motion is `kinetic.word_state` = JS `kineticState`; Intense's is `intense.word_state` / `bar_state` / `_held` and JS `intenseState` / `intenseBarState` / `intenseHeld`. Change both together; the comments mark the
  pairs (`= media.xyz`). Every ASS font size goes through `media.ass_size` (libass sizes fonts by
  ascent + descent, the preview by the em).
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
- Some releases put a dub ahead of the original audio (Portuguese, then English). Previews, renders
  and speech-to-text all use `media.audio_track(path)` (the first English track, else the first);
  never map `0:a:0` directly.
- Subtitle files can be tens of seconds off: snap tries ±5 s then ±45 s, picks the occurrence whose
  neighbouring lines also match, and remembers the offset per item (`data/offsets.json`).
- Whisper starts a word where the previous one ended; `stt_worker.refine_starts` moves starts later
  to the real voice onset so captions never appear before speech, and `place_strays` moves a faint
  word sitting on a noise before a pause onto the speech after it. The worker imports faster-whisper
  only under `__main__`, so tests can load its timing functions with the speech Python.
- Never try `raw.decode("utf-16")` on a subtitle without a BOM: it "succeeds" on most even-length
  Latin-1 files and yields gibberish. `subs.read_text` needs a BOM or NUL bytes before using UTF-16.
- Snap results are cached in `data/listen.db` (`web.cached_snap`). Bump `web.LISTEN_V` whenever
  `snap.py` or `stt_worker.py` timing changes, or old timings keep being served.
- Say Anything (`sayany.py`): `sayany.cut` = JS `saCut`; a take's measured `cuts` are what both use.
- Slow motion: `slowmo.timemap` = JS `outLength` / `applySlowRate` (the preview plays the part at 1/factor).
- Speed ramp: `ramp.speed` = JS `rampSpeed` (Studio preview sets playbackRate from it).
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
