# Say the Line MCP server

`python -m saytheline mcp` lets an AI agent (Claude Code, Claude Desktop, any MCP client) search your
library, read a title's script, and make clips, reaction GIFs, stills, supercuts and Say Anything
sentences, then refine them from your feedback.

## Set up

```bash
pip install -r requirements-mcp.txt          # the MCP SDK (Python 3.10+); a separate venv is fine
python -m saytheline serve                   # the web server must be running (systemd unit in deploy/)
claude mcp add -s user say-the-line -- /path/to/python -m saytheline mcp   # Claude Code, all projects
```

Run the `claude mcp add` command from the repo folder, or add `--env PYTHONPATH=/path/to/say-the-line`.
Inside the repo, `.mcp.json` registers it for that project automatically. For Claude Desktop, add
the same command under `mcpServers` in its config. If the web server is not on `127.0.0.1:8765`,
set `SAYTHELINE_URL`. Set `[server] public_url` (for example your Tailscale HTTPS address) so the
links an agent hands you open on your phone.

## Tools

| Tool | What it's for |
|---|---|
| `saytheline_search` | Find a line. Filters: TV / movies, titles to keep or drop, **short lines only** (clean reactions). Returns moment ids like `51:99`. |
| `saytheline_look` | One numbered image: a frame per candidate moment, frames across one line, or frames of a clip already made. |
| `saytheline_make_clip` | Moment → GIF (default) / MP4 / WebM / still. Snapped to speech, padding trimmed so it never starts or ends partway through a neighbouring line, and to the camera cuts (`shots`), or `whole_shot`; captions plain, word-by-word looks, speech-to-text or your own text; any shape. Lists the subtitle lines on screen; a neighbouring line cut off at an edge and on screen under a second is hidden, and `hide_lines` / `line_text` hide or reword others. |
| `saytheline_refine_clip` | A new version from feedback: trim or extend either end, `whole_shot`, captions on or off, look, text, timing shift, size, position, shape, format, and `hide_lines` / `show_lines` / `line_text` (the editor's caption-line edits). |
| `saytheline_recent_clips` | What was rendered lately (here or in the app), with ids. |
| `saytheline_save` | Heart a clip into the Library with tags. |
| `saytheline_supercut` | Every place a line is said, checked against the speech, in one MP4. |
| `saytheline_say_anything` | A typed sentence said by the library: each word or phrase from a line where it's heard, joined into one MP4. Words nobody says are left out and named. |
| `saytheline_script` | One movie's or episode's subtitles in order (or just the lines with some words, with a line either side), each with a moment id: for scenes whose exact words aren't known. |

Prompts (slash commands in Claude Code): `reaction_gif` (feeling) and `clip_from_description`.

Each result has a short text summary, structured fields (`clip_id`, `path`, `url`, `edit_url`, …),
and for anything rendered, an image of frames from the output.

## Design: an agent is not a person in the UI

The design follows the MCP spec (2025-11-25: tool annotations, structured content, image content,
isError results) and Anthropic's guidance on tools for agents: few workflow-level tools,
high-signal responses, readable identifiers, actionable errors, and careful descriptions.

| A person in the web UI… | An agent… | So the MCP… |
|---|---|---|
| watches the preview and drags handles | can't watch video | snaps to the spoken words by default, and returns **frames of the result** so it can check captions and framing before sending |
| scrolls 30 results with thumbnails | pays per token | returns 8 concise hits by default (up to 25) with the line before, the total, and which titles dominate |
| tries a few searches by hand | will search many phrasings fast | offers `short_lines_only` for clean reaction lines; one call per phrasing is cheap (~10–80 ms) |
| clicks through Cut / Captions / Output | wants one call per outcome | `make_clip` does the whole pipeline: line → subtitle offset → snap → camera cuts → word timing → render → preview |
| sees the clip open on another shot | can't see where shots change | trims the padding to the camera cuts by default, and `whole_shot` takes the reaction's entire shot |
| sees what's wrong | has to describe changes | `refine_clip` takes changes in plain units (start 500 ms earlier, captions 200 ms later, bigger text) and keeps the old version |
| reads a spinner | may time out | reports progress for long jobs; renders are seconds; supercuts report each step |
| gets the file in the browser | has to hand it over | returns the local **path** (Claude Code can send it to you), a link, and an **edit link** that opens it in the editor |

Everything goes through the web server's API, so agents and people share one job queue (one heavy
job at a time), one library and the learnt subtitle offsets, and an agent's clips show up in the app.

## The feedback loop

1. The agent makes a clip and sends it ("here it is, 1.4 s, Big word captions").
2. You reply in words: "start a beat earlier and drop the captions", "make it vertical", "the
   text is a bit early".
3. The agent calls `saytheline_refine_clip` with the clip id and those changes and sends v2. Each
   version is its own file; the recipe (the render's sidecar JSON) records its parent.
4. Anything fiddly: open `edit_url` and finish it by hand. The editor opens with the exact cut,
   captions (including the word timing and any custom text) and output settings. A clip made in the
   app can be refined by an agent the same way, by its id (`saytheline_recent_clips`).

## Reaction GIFs

The `reaction_gif` prompt asks the agent to:
1. Brainstorm a few short, famous lines that say the feeling.
2. Search each with `short_lines_only`.
3. `look` at the best candidates together and pick the face that fits.
4. Make a GIF (a beat after the line is added by default) and send it with where it's from.

## Safety

- **Read-only on media and Jellyfin.** Everything is written under the data folder.
- **Nothing is deleted.** Refining makes a new file.
- **Annotations:** search, look, script and recent are read-only; make, refine, save, supercut and
  say_anything create files.
- **Same limits as the UI:** 3 min per clip; supercuts of 40 clips at most, one at a time, each
  hit checked against the speech.
- **stdio only, for now.** The server is a local process of the agent app; nothing is exposed on
  the network.

## Next

- **Remote / phone use:** streamable HTTP with authentication, so the Claude app can use it
  away from home. Needs login on the web server first.
- **Reaction search without dialogue:** keep SDH tags like "(gasps)" and "[laughs]" as searchable
  cues; later, face and expression tagging of close-ups.
- **MCP resources** for clips (`saytheline://clip/<id>`), so clients can show them inline.
- **An eval set:** realistic requests ("reply with a disappointed GIF", "the scene where he says
  'I am the one who knocks', vertical, big text"), scored on choosing the right scene, call count
  and tokens, to tune the tool descriptions.
