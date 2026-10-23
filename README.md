# Say the Line

**Search everything ever said in your Jellyfin library, then cut it into a clip, a GIF, a still
or a supercut, from your phone.**

Say the Line indexes the subtitles of your movies and episodes. Type a line ("we need to talk") and you
get every scene where it's said, with thumbnails. Tap one to open a small editor: trim the cut,
snap it onto the spoken words, add captions (plain subtitles, or TikTok-style word-by-word
looks), pick a shape (16:9, 9:16, 1:1…) and render. Or join every hit into one supercut.

It runs on the machine that has your media, next to Jellyfin, and is used from a browser.

![Searching for "story" on a phone, the editor with the cut on the timeline, the Captions tab with TikTok-style captions on the preview, and the finished GIF](docs/screenshots/phone.png)

<p align="center"><img src="docs/screenshots/clip.gif" alt="A GIF made with Say the Line, with TikTok-style word-by-word captions" width="360"></p>

<details>
<summary>On a desktop</summary>

![Search results in a grid](docs/screenshots/desktop-search.png)
![The editor with the cut on the timeline](docs/screenshots/desktop-editor.png)

</details>

<sub>Screenshots use *His Girl Friday* (1940), which is in the public domain.</sub>

## Features

- **Search**: phrase or any-word search across every subtitle line (SQLite FTS5, a few ms),
  filter by TV / movies / show, with the lines before and after each hit and recent searches.
- **Editor**: preview window, drag-to-trim timeline, ±0.1 s nudges, play / pause / loop / mute,
  line list (clip just this line, grow, trim), editable caption text.
- **Clean edges**: a new clip never opens or closes on half of the line before or after it, and it
  starts on the line's own shot and ends before the next one (no half-second of another scene).
- **Camera cuts**: marked on the timeline; the trim handles stick to them, the arrow buttons jump
  an end to the next cut, *Whole* takes the entire shot. Frame-exact.
- **Snap to speech**: listens to the cut (faster-whisper) and moves it onto the spoken words.
  It also finds subtitles that run seconds early or late, and remembers that per episode.
- **Captions**: burned in with libass. Classic subtitles, or word-synced looks: **TikTok**,
  **Pop-in**, **Big word**, and **Poster** (automatic expressive typography sized by part of
  speech). Text from the subtitles or from speech-to-text, editable before rendering, and
  movable earlier or later for subtitles that run early or late.
- **Output**: MP4, WebM, GIF or a **still** (JPEG); original / 16:9 / 4:3 / 1:1 / 9:16 with
  crop or bars; 480–1080 or source size.
- **Audio** (optional): *Dialogue only* or *No dialogue* via demucs stem separation.
- **Supercut**: every hit for a search in one video. It checks each hit against the audio,
  skips lines that are never actually heard, can cut to just the phrase, trims each clip to its
  shot, hides repeats, evens out loudness and adds labels and captions.
- **Library**: heart moments and clips, tag them for projects; every edited clip is kept as a
  draft (Library → Recent edits) with its captions, transcript and settings.

## What you need

- **Jellyfin** with your media (10.10+; tested on 10.11).
- **The media files readable** from the machine running Say the Line (same box, or a mounted share).
- **Subtitles**: external `.srt/.ass/.vtt` files next to the videos and/or embedded *text* tracks.
  Image subtitles (PGS / VobSub) can't be searched. Bazarr is the easy way to fill gaps.
- **Python 3.11+** and **ffmpeg** built with libass (most distro and Homebrew builds are).
- About 2 GB of disk for the speech model and Python packages. A modern 4-core CPU is plenty;
  no GPU needed.

### Platforms

| | Status |
|---|---|
| **Linux** | Native, the main target. VAAPI hardware previews are used when present. |
| **macOS** | Native should work (`brew install ffmpeg python`); previews encode on the CPU. Not yet tested. |
| **Windows** | Use **Docker Desktop** or **WSL2** (run the Linux steps inside). Native Windows is untested. |
| **Docker** | `compose.yaml` included: one container, media mounted read-only. |

## Quick start (native)

```bash
git clone https://github.com/alex-colleran/say-the-line && cd say-the-line
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm        # for the Poster caption look
cp saytheline.example.toml saytheline.toml       # then edit it (see below)
python -m saytheline doctor                    # checks everything, says what to fix
python -m saytheline index                     # TV episodes (add --movies for films)
python -m saytheline serve                     # http://127.0.0.1:8765/
```

### Configure (`saytheline.toml`)

The minimum is how to reach Jellyfin and, if Jellyfin sees your files under different paths
(Docker), how to translate them:

```toml
[jellyfin]
url = "http://localhost:8096"
api_key = "…"            # Jellyfin Dashboard → API Keys → +
path_map = { "/media" = "/srv/media" }   # Jellyfin's path = this machine's path
```

The DB source (`db = ...`) is what the author runs day to day; the API source has so far only been
tested against recorded Jellyfin responses, so please report anything odd.

Every setting can also be an environment variable (`SAYTHELINE_JELLYFIN_URL`, …); see
[`saytheline.example.toml`](saytheline.example.toml) for all of them.

### Indexing

```bash
python -m saytheline index --series "Your Show"    # one show first, to try it
python -m saytheline index                          # all episodes
python -m saytheline index --movies                 # all movies
python -m saytheline search "we need to talk"          # search from the terminal
```

External subtitle files index fast: thousands of episodes in a couple of minutes. Embedded
tracks mean reading through each video file, so a big library takes hours the first time. It is
incremental, so later runs only pick up new or changed subtitles. Run it nightly (cron, a
systemd timer: see `deploy/`).

## Docker

```bash
cp saytheline.example.toml saytheline.toml   # set url/api_key; path_map to the /media mount below
docker compose up -d --build
docker compose exec say-the-line python -m saytheline doctor
docker compose exec say-the-line python -m saytheline index
```

Edit the media volume in `compose.yaml` so the container sees your library (read-only). If
Jellyfin itself runs in Docker, mounting the same host folder at the same path as Jellyfin's
container means no `path_map` is needed.

## Security

There is **no login**. By default Say the Line listens on `127.0.0.1` only. To use it from a phone,
put it on a private network (for example **Tailscale**: `tailscale serve --bg 8765`) or behind a
reverse proxy with authentication. **Don't expose it to the internet**: anyone who can reach it
can browse your library and start heavy video jobs.

Say the Line only *reads* your media and Jellyfin. Everything it writes (index, previews, clips,
library) goes into the data folder (`./data` by default).

## How it works (short)

- `python -m saytheline index` asks Jellyfin for every item and its subtitle tracks, picks the best English
  dialogue track per item, and stores every line in a SQLite FTS5 index.
- The web server (Python standard library only) serves the single-page UI and runs ffmpeg for
  previews, thumbnails and renders, one heavy job at a time.
- Speech features run faster-whisper (`small.en`, CPU) in a child process. Word starts are
  refined against the audio energy so captions never appear before the word is said.

More detail, including design decisions and their reasons, is in [`docs/how-it-works.md`](docs/how-it-works.md).

## For AI agents: the MCP server

`python -m saytheline mcp` gives an agent (Claude Code, Claude Desktop, …) tools to search your
library, look at candidate frames, make GIFs / clips / stills / supercuts, and refine them from
your feedback ("start earlier, bigger text"). Ask it to "reply with a reaction GIF from my
library" and it can. Setup and design: [`docs/mcp.md`](docs/mcp.md).

```bash
pip install -r requirements-mcp.txt
claude mcp add -s user say-the-line -- "$(which python)" -m saytheline mcp   # run from the repo folder
```

## Using it with an AI coding agent

[`AGENTS.md`](AGENTS.md) (also read by Claude Code via `CLAUDE.md`) describes the layout, how to
run and test it, and the rules to follow when changing it. Point your agent at it before asking
it to set Say the Line up or change it.

## Similar projects

Plenty of people have had this idea. If Say the Line isn't quite what you want, one of these might
be. It shares no code with any of them.

- [videogrep](https://github.com/antiboredom/videogrep) by Sam Lavigne was the first: search a
  video's subtitles and cut the matches into a supercut (command line).
- [Frinkiac](https://frinkiac.com/) is the classic quote-to-screenshot search, for The Simpsons
  (hosted, not self-hosted).
- [jellymeme](https://github.com/armanckeser/jellymeme): describe a scene in your Jellyfin
  library in your own words and get it as a captioned GIF, clip or still, or a montage.
- [CineSnip](https://github.com/jayhosted/CineSnip): a Discord bot that makes GIFs and clips from
  Plex or Jellyfin by quote or timecode.
- [jelly-clipper](https://github.com/arnolicious/jelly-clipper): make and share clips of Jellyfin
  content, Twitch-clip style.
- [Cliparr](https://github.com/TechSquidTV/Cliparr): a timeline clip editor for Plex, Jellyfin
  and local files, with GIF export and styled subtitles.
- [GifHorse](https://github.com/Coyote-git/gifhorse): search the dialogue of your video files and
  make subtitled GIFs from the command line.
- [PlexGifMaker](https://github.com/TechBuckler/PlexGifMaker): captioned GIFs from a Plex library.
- [quote-clipper](https://github.com/victornpb/quote-clipper) and
  [Quote2Clip](https://github.com/Franckevicius/Quote2Clip): command-line quote compilations.

What Say the Line adds to the mix: exact search across the whole library with the lines around each
hit, cuts that land on the shot changes so no stray frames from the next scene slip in, snapping
a cut onto the spoken words (which also spots out-of-sync subtitles), word-by-word caption
looks, supercuts that check each line is really said, and an MCP server so an AI agent can make
clips for you.

## Personal use

Say the Line is a tool for working with media you own, on your own machine. Respect the rights of
whoever made the films and shows when you share what you cut.

## Licence

MIT, see [LICENSE](LICENSE). The bundled caption fonts keep their own open licences
([`saytheline/fonts/licenses`](saytheline/fonts/licenses)), and the interface icons are
[Tabler Icons](https://tabler.io/icons) (MIT).
