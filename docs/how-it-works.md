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
- **Order:** subtitle files first (fast), then embedded tracks (ffmpeg reads the whole file), each
  newest added first, and the nightly unit does movies before TV. A run cut short by its time cap
  has then done what was just added.
- **Jellyfin's copies first:** Jellyfin keeps the embedded tracks it has pulled out for playback
  (`data/subtitles/<id[:2]>/<item id>/<stream>.srt|.ass`). The indexer reads those instead of the
  video when the copy is newer than the file (an upgraded file can number its streams differently).
- **Self-overlapping tracks:** a track where over a quarter of the lines start before the previous
  one ends holds more than this episode (one release muxed every earlier episode into each file);
  another track is used when one reads as dialogue (`subs.overlap`).
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
- **Loudness:** word timing also measures each word (its loudest 10 ms frames) against the clip's
  typical word (`stt_worker.loudness`, `"loud"` in dB). Poster and Intense use it: +6 dB is a shout
  (the biggest tier; at most two per poster, so a line said loudly throughout isn't all big), +3 dB
  over the rest of its own poster at least a big word, and -11 dB with a quiet neighbour a whisper
  (a size smaller, drawn in outline only). Drafts timed before this have no loudness; Redo adds it.
- **Kinetic** (`kinetic.py`): the original, calmer motion. Poster's tiers and sizes, set as
  justified stacks (each line scaled to the widest), and on every other card that can take it a
  lockup: the card's first or last word turned 90 degrees to run the stack's height. Words arrive as
  they are said: most rise from behind their line's baseline (an ASS `\clip` mask), tier-4 words slam
  in oversized and settle, the vertical word slides along its column. A held card grows 5 %; the next
  card's arrival drops the old words one by one. `word_state` is the whole animation (JS `kineticState`).
- **Intense** (`intense.py`): Kinetic grown into everything. Poster's tiers and sizes, set as justified stacks (each line scaled
  to the widest, and a line too short for that stretched sideways, up to 1.5x), and on every other
  card that can take it a lockup: the card's first word (on the left, reading up) or last word (on
  the right, reading down) turned 90 degrees and sized to run the stack's height, so the card still
  reads in spoken order (not a word of thin letters, which reads as stripes). Each word arrives by
  what it is: slam (tier 4; a shout also shakes), drop (big words, bouncing), wipe (8+ letters),
  type (after a pause, letter by letter), fade (a whisper), rise (the rest, out of a mask under
  their line), slide (the vertical word). A shouted key word, and the key word on every other card,
  sits on a highlight bar that wipes in behind it, with dark type. A held card grows 5 %; it leaves
  by how its sentence ends: `!` scatters, `?` lifts away, anything else falls. `word_state` and
  `bar_state` are the whole animation; the render writes one ASS event per word or bar per frame
  (30 fps) from them, and the preview runs the same functions in JS.
- **Size and position:** one scale for every look (`media.SIZE_STEPS`, `block_top`, `fill`; = JS
  `SIZE_STEPS`, `capPlace`): XS, S, M, L, XL, each about 1.4x the last (XS half of M, XL twice it);
  Top and Bottom 5 % in from the frame's edges, Upper and Lower a quarter in, Middle centred. The
  laid-out looks (Poster, Kinetic, Intense) scale each card to fill part of the frame, so for them the
  size sets that share (45 % of the width at XS to 97 % at XL) rather than the font size alone. The
  preview balances its line breaks (`text-wrap: balance`) as libass's smart wrapping does.
- **Font sizes:** libass (like VSFilter) sizes a font by its ascent + descent, CSS and Pillow by its
  em, so every ASS writer converts with `media.ass_size` (Inter: x1.21). Without it renders came out
  about 18 % smaller than the editor showed.
- **Timing:** Earlier / Later (Captions tab) moves every caption in the clip, words too, by up to
  5 s (`caption_shift_ms`; `web.shift_cues` = JS `shiftCues`). It is separate from the subtitle
  offset snap learns, which moves the cut: this only moves the text.
- **Word timing:**
  - From speech: Whisper's word times.
  - From subtitles: the subtitle words are aligned to Whisper's (`snap.align_words`), and
    unmatched words are interpolated between heard ones.
- **Timing fixes:**
  - Whisper starts a word where the previous one ended, so after a pause the caption appeared in
    the silence. `stt_worker.refine_starts` moves each start *later* to where the voice energy
    rises, never earlier, searching the whole word (one was stretched 0.9 s into a pause).
  - A faint word followed by silence is one Whisper put on a breath or a noise ahead of the speech
    ("I" 1.3 s before "I didn't do it"). `stt_worker.place_strays` moves it to just before the next
    word: 15 dB under the clip's typical word, with at least 0.3 s of silence after it.
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

## Slow motion (`slowmo.py`)
- **What:** one part of the cut (`slow: {factor 2|4, a_ms, b_ms, sound}`, up to 5 s of source) plays
  2× or 4× slower. The slowed cut is built first as an intermediate video at the output size, then
  `media.render` makes the clip from it like from any source: every format, caption look and
  dialogue separation works unchanged.
- **Frames:** the part's frames are read at the output size through the same crop/scale chain,
  then RIFE (rife-ncnn-vulkan, model v4.6, Vulkan) makes n × factor frames from n, played at the
  source's frame rate. Interpolating across a camera cut morphs one shot into the next, so the part
  is split at the cuts `shots.find_cuts` finds and each shot is interpolated on its own (a one-frame
  shot is repeated).
- **Joins:** before | slow | after are trimmed from one `-ss` point and concatenated, so the frames
  either side of each join are exactly the ones the trims leave out. Audio: pitched down
  (`asetrate`, like slowed tape), the same pitch (`atempo`), or silent, with 10 ms fades at the joins.
- **Captions:** `slowmo.timemap` moves every line and word time onto the slowed timeline
  (unchanged before the part, stretched inside it, shifted after it).
- **Cost:** about 0.15 s per megapixel of each frame RIFE writes on a Radeon 680M (47 s to slow
  2 s of 1920×800 to 4×; 13 s at 1152×480). A part estimated at over 5 minutes is refused. It runs in
  `RENDER_SLOT` like any render, and the job reports `slowing` with frames done / total.
- **60 fps (`smooth`):** the same machinery at normal speed: every span of the cut is made of RIFE
  frames, n × 60 / source fps of them (2.5× for 24 fps), played at 60 fps; with slow motion too, the
  slow span gets factor × that. A source already within 15 % of 60 fps is left alone, and GIFs (12 fps)
  never use it. Cost is the whole clip's frames: about 80 s for 10 s at 720p.
- **Preview button:** a render with `preview: true`: at most 360p, just the slow part ±0.7 s (or the
  first 3 s at 60 fps), written to `previews/interp/` (pruned with the other previews), never to
  All rendered.
- **Live preview:** the editor plays the part at `1/factor` speed (`preservesPitch` off for Deep, muted
  for Silent), so the timing matches the render; only the extra frames are missing.

## Studio
- **Clips:** `web.studio_clip` turns a Library entry into a clip: a moment or recent edit gives its
  editor state (cut, subtitle offset, caption edits and shift, word timing, slow motion); a rendered
  clip the recipe in its sidecar. A supercut or Say Anything (many sources) can't be one clip yet.
- **Render:** `web.run_studio` keeps each clip's exact cut (no padding or shot trimming: the editor
  did that) and joins them with `media.supercut` (one size, 30 fps, loudness evened, cut or fade
  joins, optional source labels). A word look times the words of any clip that has none yet
  (`snap.subtitle_words`). A clip nobody edited shows only the lines mostly inside its cut, so the end
  of the line before doesn't flash up. Slow motion is per clip (`media.render(slow=…)`).
- **Draft:** a Studio is a Library draft of type `studio`: its name, clips in order, settings and song.
- **Music (`music.py`, `beat_worker.py`):** an uploaded song is kept in `data/uploads/` under a hash
  of its bytes. Its beats are found once, in the speech Python (numpy only): positive spectral flux
  on a log spectrogram (centred 2048-sample frames, 512 hop at 22.05 kHz) with its 1 s trend removed;
  tempo from the envelope's autocorrelation weighted towards ~115 BPM; beats by Ellis's dynamic
  programme; bar starts as the phase of four with the most low-band (<150 Hz) onset. On click tracks
  at 70-174 BPM the tempo is within 0.5 % and beats within a frame (15 ms bias corrected). A BPM set
  by hand becomes an even grid through the first downbeat (`music.grid`).
- **Joins on the beat:** `music.snap_joins` moves each clip's end (and the cut's end) to the nearest
  beat, by at most half a beat (half a bar, up to 1 s, for bars), and `web.studio_snap` rounds every
  join to a whole 30 fps frame. For that to hold in the output, each supercut segment is exactly
  `round(dur x 30)` frames with its audio trimmed and padded to match, and the concat list gives each
  part's duration (AAC pads its last frame, which used to make long supercuts drift by ~50 ms a clip).
- **Speed ramp (`ramp.py`):** a strength is a fastest and slowest speed (Subtle 1.8x / 0.55x, Strong
  3.5x / 0.3x, Extreme 6x / 0.15x). Speed within a beat is `lo + (hi - lo) x g(p)` (or `hi - ...` for
  fast between beats), where the bump g is 1 on the beat: Smooth is `((1 + cos 2πp) / 2)^k`, Sharp a
  flat top with cosine edges of 8 % of a beat. For every beat to average 1x, g must average
  `(1 - lo) / (hi - lo)`, which fixes k (by bisection) or the flat top's width: the faster the peak,
  the shorter the burst. So the cut keeps its length and joins on the beat stay there. (An earlier
  version fixed the ratio and the curve, which capped the peak at 2x for Smooth whatever the strength.)
  Each segment's output frames get source times from the cumulative speed, scaled to land exactly on
  the segment's end; the source is made k times denser by RIFE (per camera shot, k from the slowest
  speed, at most 10) and each output frame takes the nearest dense frame. Captions follow by the
  inverse map, and a clip's own slow motion is replaced. JS `rampSpeed` = `ramp.speed`; the preview
  sets the clip's playbackRate from the song's position every frame.
  **Peak:** the curve is read `PEAK_LEAD` ahead of the song (Just before 80 ms, the default; Earlier
  160 ms; On the beat 0), so the fastest moment lands a little before each beat. Motion is perceived
  late, so a peak right on the beat feels behind it. Joins between clips stay on the beat itself.
- **Kept lines:** with Lines at normal speed, `ramp.keep_spans` takes each word actually said
  (Whisper, `web.studio_spoken`; subtitles stay up well past the speech and would leave nothing to
  ramp), pads it (80 ms before, 150 ms after) and joins gaps under 0.6 s. Those frames show the
  source as it was; the ramp eases back to 1x over 0.25 s either side, and each stretch between kept
  spans is scaled to cover exactly its own source, so a kept line stays at its moment and in sync.
  Kept spans need no RIFE (their frames are repeated to keep the dense sequence even). Their sound is
  the source's, gated to the kept spans with 40 ms fades (`aeval`); the clips' sound is then mixed
  under the song as usual. Lines ramped too: silent, the song alone.
- **Time left (`web.eta_view`):** each job kind lists its phases and the share of the work each stands
  for (`web.PHASES`); counters inside a phase (frames made, clips done) give the fraction done f, and
  time left is `elapsed x (1 - f) / f`. A job's time per unit of work (second of clip, hit, word) is
  kept per kind and variant in `data/eta.json` (a running average), and early on, before the
  counters say much (f < 0.3), the estimate leans on that history. Readings are smoothed (counting
  down, drifting 40 % towards each new one) so the number doesn't jump. A queued job's wait is the
  time left on the jobs ahead of it.
- **Mix:** the song from the chosen start, loudness-normalised (-27 / -22 / -17 LUFS under the clips,
  -14 alone), faded out over the last 1.5 s; with "Music dips" it goes through a sidechain compressor
  keyed on the clips' own audio, then a limiter.

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

## Say Anything (`sayany.py`)
- **Plan:** the sentence is split into words (hyphens and slashes split too). From each position
  the longest phrase (up to 6 words) that some subtitle line has exactly is tried first; a
  multi-word piece is kept only if it's heard cleanly, otherwise shorter pieces are tried.
- **Candidates:** up to 16 lines per phrase, picked from 150 search hits: only lines with the words
  exactly as typed (search matches stems, so "chocolates" also finds "chocolate"), at most 2 from one
  title. The line that is just the phrase comes first, then lines that start or end with it, then
  short lines; lines that say it more than once or are sung (♪) come last, since chants are seldom
  heard word for word. The first few are listened to; the rest wait for ›. Each candidate is listened to (snap, word timing) and
  scored on the gap before and after the words, each word's length and loudness.
- **Budget:** about 5 listens per word (40–150 a sentence). Past it, the rest get one try each and
  a missing word says so ("rushed"), with the untried lines kept so › can listen to the next one.
  Lines already heard in this sentence are reused, and results are cached in `data/listen.db`
  (bump `LISTEN_V` when listening changes).
- **Cuts:** each take's cut points are measured on the audio (quiet 10 ms frames) per padding, so
  the browser preview (`saCut`) and the render (`sayany.cut`) cut the same place; trim nudges add
  to them.
- **Render:** the supercut renderer with plain cuts; Subtitles show the sentence so far, word
  looks show each piece's own words.

## Scripts and drafts
- `/api/browse` finds titles by name (exact, then starts-with, then contains) for the main search
  and the script picker; `/api/lines` returns a title's whole subtitle in order.
- A Say Anything or supercut that is hearted is a library entry of kind `draft` whose state is
  the whole sheet (pieces and takes, or hits, ticks and checks). Later changes are saved into it
  about a second after they happen.

## Speech-to-text cost
faster-whisper `small.en` int8 on 4 CPU cores runs at about 2–3× realtime with word timestamps.
That is a few seconds per clip, but far too slow to transcribe a whole library. Use subtitles
where they exist.
