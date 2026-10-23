# How Say the Line works

Design notes: what each part does and why it is built that way. For setup, see the README.

## Index
- `jellyfin.list_items` gets every episode or movie with its path and embedded subtitle tracks,
  from the Jellyfin API (or its database, read-only).
- **Subtitle source per item:** the best English external `.srt/.ass/.vtt` beside the video,
  else each embedded *text* track in turn. A track is kept only if it reads as English dialogue
  (enough lines for the runtime, plus a common-word check), because language tags lie: anime
  releases often tag the full English track `jpn` and the signs-only track `eng`.
- **ASS:** sign styles and `\pos` / `\move` / `\p1` / `\clip` lines are dropped (typeset signs,
  karaoke, drawings). Embedded ASS is extracted as ASS so the style names survive.
- **Cleaning:** subtitle-site credit lines are dropped; `¶` becomes `♪`; pure sound-effect
  cues ("[DOOR SLAMS]") are skipped.
- **Storage:** SQLite FTS5 with the porter stemmer. `cues_fts` has one row per line;
  `pairs_fts` has one row per line plus the next one, because subtitles split sentences across
  cues and a phrase can straddle two.
- **Incremental:** an item is re-read only when its subtitle source or that file's mtime
  changes, and every item is committed on its own, so a run can be stopped at any time.
- Bitmap subtitles (PGS / VobSub) can't be searched; those items need text subtitles (Bazarr) or
  speech-to-text.

## Web server
- Python standard library only (`ThreadingHTTPServer`). The UI is one HTML file with vanilla JS
  and no build step.
- **Thumbnails:** one keyframe seek each (`-noaccurate_seek -skip_frame nokey`), two at a time,
  cached.
- **Previews:** a window of up to 170 s around the line, 480p H.264 (VAAPI when available, else
  x264 ultrafast), cached. Seeking is frame-accurate, so t=0 is the window start, and a 90 kHz
  time base keeps every frame's own time (the default one-tick-per-frame base would shift a
  23.976 fps source by up to a frame, and the editor shows exact frames).
- **Jobs:** render, still, snap, transcribe, poster and supercut are background jobs. The
  browser polls `/api/job/<id>`. All heavy work shares **one** slot, so jobs queue instead of
  stacking up on a small machine.

## Editor
- **Cut:** a timeline with drag handles, nudges, and a line list (Only / + / −). Selecting past
  the loaded preview grows it.
- **Snap to speech:** transcribes the cut ±5 s with word timestamps, finds the subtitle words in
  the transcript (difflib) and moves in/out to the spoken words (0.25 s lead, 0.4 s tail).
  - If the line isn't heard, it retries ±45 s: some subtitle files are 20–30 s off.
  - When a line is said twice, the right occurrence is the one where the *neighbouring*
    subtitle lines also line up.
  - The offset found is remembered per item (`data/offsets.json`), and the editor applies it when
    the episode is opened again.
- **Neighbouring lines:** a new clip's padding stops between lines (`shots.fit_lines`): if it would
  start partway through the line before, it starts once that line ends; if it would end partway
  through the next, it ends before that one starts. Otherwise most clips open or close on half a
  sentence. Like the shot trim it only shortens the padding, never the line itself.
- **Camera cuts:** see below. A new clip's padding is trimmed to the cuts in it, with a note and
  Undo; ticks mark the cuts on the timeline, the handles stick to them, and the arrow buttons (Shot row) move
  either end to the cut before or after it. *Whole* takes the entire shot the lines are in.
  Snap to speech trims again around the heard words.
- **Exact frames:** with the frame grid from the cut scan, the preview shows the clip's real
  first frame at the in point and its last at the out point, "Set here" takes the frame on screen,
  and playback stops on the last frame (`requestVideoFrameCallback`, where the browser has it).
- **Drafts:** the editor state (cut, offset, caption look and style, the edited transcript,
  output settings) is saved as JSON whenever it changes. The last 40 edited clips are kept;
  hearted moments keep theirs permanently. A new clip always opens with plain subtitles so there
  is text on screen at once.

## Camera cuts (`shots.py`)
- Frames shrunk to 32×18 grey (decoded on the GPU with VAAPI), about 1.3 s per 20 s of video.
  A frame is a cut when, after normalising each frame to its own brightness and contrast:
  - the **median** pixel change is large (most of the picture changed: a mouth or hand moving
    in an anime close-up doesn't count);
  - it stands out from the frames either side (fast action changes every frame);
  - and it survives the best shift of up to 2 px (camera shake, pans).
  Two "cuts" within 3 frames are a flash and both are dropped. Fades and dissolves aren't
  reported. ffmpeg's own scene score was tried first: it missed cuts in dark scenes and fired on
  anime mouth flaps. The rule was tuned by eye on 141 frame pairs from 18 titles.
- A cut is the boundary **half a frame before** the new shot's first frame, so a clip starting
  there opens on the new shot and one ending there closes on the outgoing shot's last frame.
- Files without real frame timestamps (old XviD AVIs with packed B-frames) have their frame
  times guessed from the decode order, which shifts by a frame with the seek point. There each
  cut is used one frame inward (`margin_ms`).
- **Trimming** (`shots.fit`) only moves the padding: the start goes to the last cut before the
  line (or the heard words), the end to the first cut after it, never into the words beyond
  120 ms (word times are that loose). When speech starts before a cut (a J-cut) the clip keeps
  the words and the sliver of the previous shot.
- Scans are cached per item (`data/shots/<item>.json`) with the spans already scanned, so the
  editor, supercuts and agents scan each stretch once.

## Captions
- **Classic:** the subtitle lines re-timed to the cut, written as SRT and burned in by libass
  after scaling (so the size is relative to the output) with a `force_style`.
- **Word looks** (`media.word_ass`): one ASS event per word, with `\c` / `\fscx` overrides and
  `\t` for the entrance pop.
  - **TikTok:** 1–3 words; the spoken word is in the highlight colour at 112 %.
  - **Pop-in:** up to 5 words; each appears as it's said. Unsaid words are laid out invisible,
    so the line never shifts.
  - **Big word:** exactly one word at a time.
  - The browser preview runs the same chunking and timing rules in JS, so what you see is what
    renders.
- **Poster:** each word gets an emphasis tier from spaCy (exclamations / greetings / expletives
  largest, the clause's core nouns and main verb next, then other content words; function words
  are smallest and lighter). `poster.py` lays out one poster per clause with real font metrics:
  one big word per line, and the whole poster scaled to fill the frame.
- **Word timing:**
  - From speech: Whisper's word times.
  - From subtitles: the subtitle words are aligned to Whisper's (`snap.align_words`), and
    unmatched words are interpolated between heard ones.
- **Timing fixes:**
  - Whisper starts a word where the previous one ended, so after a pause the caption appeared in
    the silence. `stt_worker.refine_starts` moves each start *later* to where the voice energy
    rises, never earlier.
  - Given silence between lines, greedy decoding can drop a line and stretch the next over the
    gap (in one test episode the caption sat over the previous speaker's line). A snapped match
    with a word longer than 1.2 s is heard again ±3 s with beam search (`stt_worker --careful`)
    and re-timed from that when the line is heard there too. On 40 random library lines this
    changed nothing else (one 80 ms re-time); a plain beam-search, VAD or burst-by-burst
    transcript instead fixed this case but mistimed or lost others.
  - In fast speech Whisper bunches words into zero-length runs. `normalize_words` re-spaces them
    evenly within the time actually spoken (at least 70 ms each).
  - The pop animation shrinks to fit a short word, and is skipped under 110 ms on screen.
- **Fonts** are bundled and passed to libass with `fontsdir`. The browser loads the same files,
  so the preview matches the render.

## Output
- MP4 (x264 / AAC), WebM (VP9 / Opus), GIF (12 fps, ≤ 480 px, two-pass palette; input-side `-t`
  because palettegen waits for end of input), still (JPEG).
- **Exact ends:** input-side `-ss` drops the frames before the in point, but input-side `-t` can
  let one frame past the out point through (depending on where the start falls between frames).
  Every render's video starts with `trim=end=`, so the last frame is the last one before the out
  point: with a camera cut there, the next shot never shows.
- **Still:** seeks to the frame and shifts its timestamp so the caption track lines up. The word
  looks render without their pop, which a single frame could catch mid-grow.
- **Shapes:** original / 16:9 / 4:3 / 1:1 / 9:16, crop or bars. Sizes by short side, never
  upscaled.
- **Audio (optional):** demucs `htdemucs --two-stems=vocals` replaces the clip's audio with the
  vocals (dialogue) stem or the rest (music + effects). About 1.6× the clip length on a CPU.

## Supercut
- Loads up to 300 hits for the search. "Hide repeats" drops the same line in the same item, and
  hits less than 1.5 s apart.
- **Check against speech:** each hit is snapped like above.
  - All hits in one item must agree on one offset; outliers are re-checked at the majority
    offset.
  - Lines that are never heard (songs, a TV in the background, a wrong release) are flagged and
    skipped by default.
  - Foreign-language audio with English subtitles can't be checked (speech-to-text is English
    only). When the audio track is tagged as another language (`media.foreign_audio`), the hit is
    not listened to: it keeps its subtitle timing and is never skipped, and word looks spread each
    line's words evenly across it. Untagged tracks are assumed to be English.
- **Order:** best match, air date, show, shuffle, or drag clips by their handle into your own.
- **Cut:** whole line, or just the searched phrase (from word timing), with each clip's padding
  trimmed to the camera cuts (Shots: on by default). Overlapping cuts in one item merge.
- **Render:** each segment is rendered uniform (same size, 30 fps, 48 kHz stereo, a silent track
  if the source has none) with per-segment `loudnorm` and 15 ms audio fades (no clicks), then
  joined with the concat demuxer without re-encoding.
- Limits: 80 clips, 10 minutes.

## Speech-to-text cost
faster-whisper `small.en` int8 on 4 CPU cores runs at about 2–3× realtime with word timestamps.
That is a few seconds per clip, but far too slow to transcribe a whole library. Use subtitles
where they exist.
