"""Core logic tests: no media, Jellyfin, ffmpeg or speech model needed.

  python -m unittest discover -s tests
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SAYTHELINE_CONFIG", os.devnull)       # never pick up a real saytheline.toml
os.environ.setdefault("SAYTHELINE_PERF", os.devnull)         # nor write to the real timing log

from saytheline import config, db, jellyfin, library, media, search, snap, subs  # noqa: E402

SRT = """1
00:00:01,000 --> 00:00:02,500
We need to talk.

2
00:00:03,000 --> 00:00:04,000
<i>About what?</i>

3
00:00:05,000 --> 00:00:06,000
Subtitles by www.example.com
"""

ASS = """[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Good morning
Dialogue: 0,0:00:01.00,0:00:02.00,Sign,,0,0,0,,{\\pos(100,200)}MOTEL
Dialogue: 0,0:00:03.00,0:00:04.50,Default,,0,0,0,,{\\i1}Morning to you{\\i0}
"""


class Subtitles(unittest.TestCase):
    def test_srt(self):
        cues = subs.tidy(subs.parse(SRT, "srt"))
        self.assertEqual(cues[:2], [(1000, 2500, "We need to talk."), (3000, 4000, "About what?")])
        self.assertEqual(len(cues), 2, "credit line dropped")

    def test_ass_skips_signs(self):
        self.assertEqual([t for _, _, t in subs.parse(ASS, "ass")], ["Good morning", "Morning to you"])

    def test_pilcrow_is_music(self):
        self.assertEqual(subs.clean("¶ la la ¶"), "♪ la la ♪")

    def test_overlap(self):
        own = [(i * 3000, i * 3000 + 2500, "line") for i in range(10)]
        merged = sorted(own + [(i * 3000 + 1000, i * 3000 + 2000, "other episode") for i in range(10)])
        self.assertEqual(subs.overlap(own), 0.0)
        self.assertGreater(subs.overlap(merged), subs.MAX_OVERLAP)

    def test_encodings(self):
        text = "1\r\n00:00:01,000 --> 00:00:02,000\r\nCaf\u00e9? S\u00ed, se\u00f1or.\r\n"
        with tempfile.TemporaryDirectory() as d:
            for enc in ("cp1252", "utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
                p = os.path.join(d, enc + ".srt")
                with open(p, "wb") as f:
                    f.write(text.encode(enc))
                self.assertEqual(subs.read_text(p), text, enc)   # Latin-1 must not read as UTF-16


class Words(unittest.TestCase):
    def test_bunched_words_get_screen_time(self):
        ws = [dict(w=w, s=1000, e=1000) for w in "I don't know".split()]
        out = media.normalize_words(ws)
        starts = [w["s"] for w in out]
        self.assertEqual(starts, sorted(starts))
        self.assertTrue(all(b - a >= media.MIN_WORD_MS for a, b in zip(starts, starts[1:])))
        self.assertTrue(all(w["e"] - w["s"] >= media.MIN_WORD_MS for w in out))

    def test_unheard_word_stays_in_reading_order(self):
        # subtitles ~4 s ahead of the voice; the second "Anne" isn't matched. It used to fall back
        # to its line's subtitle time, landing among the previous line's words ("call Anne me").
        cues = [dict(idx=1, start_ms=0, end_ms=1000, text="Did you just call me"),
                dict(idx=2, start_ms=1000, end_ms=2000, text="than Anne Bancroft."),
                dict(idx=3, start_ms=2000, end_ms=3000, text="Anne Bancroft was hot.")]
        heard = [dict(w=w, s=s, e=s + 90) for w, s in [("Did", 900), ("you", 1000), ("just", 1100), ("call", 1200),
                 ("me", 1400), ("than", 5000), ("Anne", 5200), ("Bancroft", 5300), ("Bancroft", 6000), ("was", 6500), ("hot", 6600)]]
        out = snap.align_words(cues, heard)
        flat = [w for c in out for w in c["words"]]
        self.assertEqual([w["s"] for w in flat], sorted(w["s"] for w in flat), [(w["w"], w["s"]) for w in flat])
        anne = out[2]["words"][0]
        self.assertTrue(5300 <= anne["s"] < 6000, anne)

    def test_word_far_off_the_scene_offset_is_retimed(self):
        # Whisper pins the first words of a long window to its start: "I went to" 2 s early, while
        # every other word lands ~0.4 s after its subtitle. They belong just before "the".
        cues = [dict(idx=1, start_ms=10000, end_ms=12500, text="I went to the woods because I wanted"),
                dict(idx=2, start_ms=12700, end_ms=14000, text="to live deliberately."),
                dict(idx=3, start_ms=14200, end_ms=15000, text="I'll second that."),
                dict(idx=4, start_ms=15200, end_ms=16500, text="To put to rout")]
        heard = [dict(w=w, s=s, e=s + 150) for w, s in [("I", 8000), ("went", 8300), ("to", 8300), ("the", 10500),
                 ("woods", 10600), ("because", 10850), ("I", 11100), ("wanted", 11300), ("to", 13100), ("live", 13300),
                 ("deliberately", 13500), ("I'll", 14600), ("second", 14800), ("that", 15000), ("To", 15600),
                 ("put", 15800), ("to", 16000), ("rout", 16200)]]
        out = snap.align_words(cues, heard)
        first = out[0]["words"][:3]
        self.assertTrue(all(9500 <= w["s"] < 10500 for w in first), first)
        flat = [w["s"] for c in out for w in c["words"]]
        self.assertEqual(flat, sorted(flat))

    def test_speaker_label_comes_up_with_its_line(self):
        # "[Swiper]" is never said; the voice comes before the subtitle. The label used to take the
        # subtitle's own start, after "Huh?", and pop on its own once the line was over.
        cues = [dict(idx=1, start_ms=4457453, end_ms=4458442, text="[Swiper] Huh?"),
                dict(idx=2, start_ms=4458500, end_ms=4460000, text="-Swiper, no-- [gasps]")]
        heard = [dict(w="Huh", s=4457300, e=4457500), dict(w="Swiper", s=4458600, e=4458900),
                 dict(w="no", s=4458900, e=4459100)]
        out = snap.align_words(cues, heard)
        self.assertEqual([w["w"] for w in out[0]["words"]], ["[Swiper]", "Huh?"])
        label, huh = out[0]["words"]
        self.assertEqual((huh["s"], out[0]["heard"]), (4457300, 1))
        self.assertTrue(huh["s"] - media.MIN_WORD_MS <= label["s"] <= label["e"] <= huh["s"], label)
        flat = [w for c in out for w in c["words"]]
        self.assertEqual([w["w"] for w in media.normalize_words(flat)], [w["w"] for w in flat])
        self.assertEqual(snap._tags("-[man 2] (laughs) Hey [x".split()), [True, True, True, False, False])
        # a line that is only a note is still timed as words
        self.assertEqual([w["w"] for w in snap.align_words([dict(idx=3, start_ms=0, end_ms=900, text="[laughs]")], [])[0]["words"]],
                         ["[laughs]"])

    def test_matched_words_keep_their_loudness(self):
        cues = [dict(idx=1, start_ms=0, end_ms=1000, text="Get out!")]
        heard = [dict(w="Get", s=0, e=200, loud=2.0), dict(w="out", s=200, e=500, loud=9.5)]
        self.assertEqual([w.get("loud") for w in snap.align_words(cues, heard)[0]["words"]], [2.0, 9.5])

    def test_bigword_is_one_word(self):
        ws = [dict(w=w, s=i * 100, e=i * 100 + 90) for i, w in enumerate("one two three four".split())]
        chunks = media.chunk_words(ws, *media.CHUNK_RULES["bigword"])
        self.assertTrue(all(len(c) == 1 for c in chunks))
        self.assertTrue(all(len(c) <= 3 for c in media.chunk_words(ws, *media.CHUNK_RULES["tiktok"])))


class Snap(unittest.TestCase):
    """A test episode, subtitles ~27 s late: "Your turn, then." is said at 42.7 s and
    "You're on your own." at 44.6 s. Listened to in one go, Whisper dropped the first line and
    stretched the second over the pauses from 41.6 s; with beam search it hears both where they are."""
    @staticmethod
    def said(*parts):
        return [dict(w=w, s=s + i * 200, e=s + i * 200 + 180) for text, s in parts for i, w in enumerate(text.split())]

    def setUp(self):
        self.real = snap.transcribe
        self.calls = []

    def tearDown(self):
        snap.transcribe = self.real

    def listen(self, wide, close):
        def fake(src, a, b, careful=False):
            self.calls.append(careful)
            ws = close if careful else wide
            return [w for w in ws if a <= w["s"] < b]
        snap.transcribe = fake

    def test_a_stretched_match_is_retimed_up_close(self):
        smeared = [dict(w="You're", s=41560, e=42900), dict(w="on", s=42900, e=43100),
                   dict(w="your", s=43100, e=45140), dict(w="own.", s=45140, e=45420)]
        self.listen(smeared, self.said(("Your turn, then.", 42680), ("You're on your own.", 44590)))
        cue = dict(idx=15, start_ms=72142, end_ms=74143, text="You're on your own.")
        r = snap.snap("x", 71992, 74493, [cue], context=[dict(idx=14, start_ms=69406, end_ms=72108, text="Your turn, then.")])
        self.assertTrue(r["found"])
        self.assertEqual((r["first_s"], r["offset_ms"]), (44590, 44590 - 72142))
        self.assertTrue(r["wide"] and r["retimed"])

    def test_plain_matches_are_heard_once(self):
        self.listen(self.said(("Did you go to the funeral?", 2459100)), [])
        cue = dict(idx=1, start_ms=2459000, end_ms=2461000, text="Did you go to the funeral?")
        r = snap.snap("x", 2459000, 2461000, [cue])
        self.assertEqual((r["first_s"], self.calls), (2459100, [False]))

    def test_stretched_but_not_heard_up_close_keeps_the_match(self):
        stretched = [dict(w="Did", s=1000, e=2500), *self.said(("you go to the funeral?", 2500))]
        self.listen(stretched, self.said(("Let's go to the femur.", 2300)))
        cue = dict(idx=1, start_ms=1000, end_ms=3500, text="Did you go to the funeral?")
        r = snap.snap("x", 1000, 3500, [cue])
        self.assertEqual((r["first_s"], self.calls, r.get("retimed")), (1000, [False, True], None))


class CropWindow(unittest.TestCase):
    def test_placed_window_moves_and_shrinks(self):
        # 1920x1080 to 9:16: the largest window is 608x1080, centred
        self.assertEqual(media.geometry(1920, 1080, "9:16", "crop", 720)[2][0], "crop=ih*0.562500:ih")
        self.assertEqual(media.crop_box(1920, 1080, 9 / 16), (608, 1080, 656, 0))
        # pushed to the right edge and half size: clamped inside the picture
        self.assertEqual(media.crop_box(1920, 1080, 9 / 16, {"x": 1, "y": 0.5, "s": 0.5}), (304, 540, 1616, 270))
        w, h, chain = media.geometry(1920, 1080, "9:16", "crop", 720, crop={"x": 0.25, "y": 0.4, "s": 0.5})
        self.assertEqual((w, h, chain), (720, 1280, ["crop=304:540:328:162", "scale=720:1280"]))
        # the same shape as the source can still be zoomed in; bars ignore the window
        self.assertEqual(media.geometry(1920, 1080, "16:9", "crop", 720, crop={"s": 0.5})[2][0], "crop=960:540:480:270")
        self.assertEqual(media.geometry(1920, 1080, "16:9", "crop", 720, crop={"s": 1})[2], ["scale=1280:720"])
        self.assertTrue(media.geometry(1920, 1080, "1:1", "pad", 720, crop={"s": 0.5})[2][1].startswith("pad="))
        self.assertEqual(media.crop_box(1920, 1080, 1, {"s": 0.01})[0], 270)        # never below a quarter


class Paths(unittest.TestCase):
    def test_path_map_longest_prefix(self):
        os.environ["SAYTHELINE_PATH_MAP"] = "/media=/srv/media,/media/tv=/mnt/tv"
        try:
            pm = db.path_map()
            self.assertEqual(db.to_host("/media/tv/Show/e1.mkv", pm), "/mnt/tv/Show/e1.mkv")
            self.assertEqual(db.to_host("/media/movies/m.mkv", pm), "/srv/media/movies/m.mkv")
            self.assertEqual(db.to_host("/mediaX/m.mkv", pm), "/mediaX/m.mkv")
        finally:
            del os.environ["SAYTHELINE_PATH_MAP"]

    def test_toml_table_flattens(self):
        self.assertEqual(config._flat({"/a": "/b", "/c": "/d"}), "/a=/b,/c=/d")

    def test_filter_path_escaping(self):
        # escaped for the option parser (C\:, it\'s), then again for the graph parser (\ and ')
        self.assertEqual(media._fpath("C:\\x\\it's.ass"), "C\\\\:/x/it\\\\\\'s.ass")

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_filter_path_reaches_ffmpeg_intact(self):
        # a quote, a comma and brackets in the folder; on Windows the drive colon as well
        with tempfile.TemporaryDirectory() as d:
            folder = os.path.join(d, "it's, [x]")
            os.makedirs(folder)
            srt = os.path.join(folder, "a.srt")
            with open(srt, "w", encoding="utf-8") as f:
                f.write("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
            r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "color=s=160x90:d=0.5",
                                "-vf", f"subtitles={media._fpath(srt)}:fontsdir={media._fpath(media.FONT_DIR)}",
                                "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(r.returncode, 0, r.stderr[-300:])


class Index(unittest.TestCase):
    def test_search_phrase_and_straddle(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "cues.db")
            c = db.open_index(p)
            c.execute("INSERT INTO items (id, jf_id, kind, series, season, episode, title, path) "
                      "VALUES (1, 'abc', 'episode', 'Show', 1, 2, 'Pilot', '/x.mkv')")
            lines = ["I am not in danger, Skyler.", "I am the danger.", "A guy opens his door", "and gets shot"]
            for i, t in enumerate(lines):
                cid = c.execute("INSERT INTO cues (item_id, idx, start_ms, end_ms, text) VALUES (1, ?, ?, ?, ?)",
                                (i, i * 2000, i * 2000 + 1500, t)).lastrowid
                c.execute("INSERT INTO cues_fts(rowid, text) VALUES (?, ?)", (cid, t))
                if i:
                    c.execute("INSERT INTO pairs_fts(rowid, text) VALUES (?, ?)", (cid - 1, lines[i - 1] + " " + t))
            c.commit()
            c.close()
            self.assertEqual([h["idx"] for h in search.search(p, "i am the danger")], [1])
            self.assertEqual([h["idx"] for h in search.search(p, "opens his door and gets")], [2])
            self.assertEqual(search.facets(p, "danger"), [{"key": "s:Show", "kind": "episode", "label": "Show", "n": 2, "exact": 2}])
            self.assertEqual(search.search(p, "danger", exclude=["s:Show"]), [])
            self.assertEqual(len(search.search(p, "danger", only=["s:Show"])), 2)
            self.assertEqual(search.search(p, "danger", only=[]), [])


class Library(unittest.TestCase):
    def test_drafts(self):
        with tempfile.TemporaryDirectory() as d:
            lib = library.Library(os.path.join(d, "library.db"))
            for i in range(library.RECENT_KEEP + 5):
                lib.save({"kind": "recent", "item_id": 1, "start_ms": i, "state": "{}"})
            self.assertEqual(len(lib.list(kind="recent")), library.RECENT_KEEP)
            self.assertEqual(lib.list(), [], "drafts stay out of the saved list")
            lib.save({"kind": "moment", "item_id": 1, "start_ms": 44, "state": '{"a": 1}', "tags": ["Ideas "]})
            self.assertEqual(lib.draft(1, 44)["kind"], "moment", "a hearted moment wins over its draft")
            self.assertEqual(lib.draft(1, 43)["kind"], "recent")
            self.assertIsNone(lib.draft(2, 0))
            self.assertEqual(lib.tags(), [{"tag": "ideas", "n": 1}])

    def test_sheet_drafts(self):
        with tempfile.TemporaryDirectory() as d:
            lib = library.Library(os.path.join(d, "library.db"))
            a = lib.save({"kind": "draft", "label": "Say Anything", "text": "“hi”", "item_id": 1, "start_ms": 5, "state": "{}"})
            b = lib.save({"kind": "draft", "label": "Supercut", "item_id": 1, "start_ms": 5, "state": "{}"})
            self.assertNotEqual(a["id"], b["id"], "two drafts may start on the same line")
            lib.update(a["id"], {"text": "“hi there”", "item_id": 2, "start_ms": 9, "state": '{"type": "sayany"}'})
            got = lib.get(a["id"])
            self.assertEqual((got["text"], got["item_id"], got["start_ms"], got["state"]), ("“hi there”", 2, 9, '{"type": "sayany"}'))
            self.assertEqual(len(lib.list(kind="draft")), 2)
            self.assertEqual(len(lib.list()), 2, "sheet drafts show with the saved entries")


class JellyfinAPI(unittest.TestCase):
    def test_items_and_tracks(self):
        page = {"TotalRecordCount": 2, "Items": [
            {"Id": "AB-CD", "Name": "Pilot", "SeriesName": "Show", "ParentIndexNumber": 1, "IndexNumber": 1,
             "Path": "/media/s1e1.mkv", "RunTimeTicks": 13_200_000_000, "MediaStreams": [
                 {"Type": "Subtitle", "Index": 3, "Codec": "subrip", "Language": "eng", "IsExternal": False},
                 {"Type": "Subtitle", "Index": 2, "Codec": "ass", "Language": "eng", "IsForced": True, "IsExternal": False},
                 {"Type": "Subtitle", "Index": 4, "Codec": "hdmv_pgs_subtitle", "Language": "eng", "IsExternal": False},
                 {"Type": "Audio", "Index": 1, "Codec": "aac"}]},
            {"Id": "ef", "Name": "Missing", "LocationType": "Virtual"}]}
        real = jellyfin._api_get
        jellyfin._api_get = lambda *a, **k: page
        try:
            items = jellyfin.list_items(("api", "http://x", "k"))
        finally:
            jellyfin._api_get = real
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual((it["jf_id"], it["runtime_s"], it["kind"]), ("abcd", 1320, "episode"))
        self.assertEqual(it["embedded"], [(3, "subrip"), (2, "ass")], "text tracks only, forced last")


try:
    from saytheline import mcp_server
except ImportError:                       # the MCP SDK is optional
    mcp_server = None


@unittest.skipUnless(mcp_server, "mcp package not installed")
class MCPHelpers(unittest.TestCase):
    def test_moment_ids(self):
        self.assertEqual(mcp_server._mid(" 51:99 "), (51, 99))
        with self.assertRaises(Exception):
            mcp_server._mid("51-99")

    def test_custom_caption_keeps_heard_times(self):
        cues = [dict(start_ms=0, end_ms=900, words=[dict(w="come", s=100, e=300), dict(w="here", s=300, e=500), dict(w="now", s=500, e=900)])]
        same = mcp_server._caption_text_cues("COME HERE NOW", cues)[0]["words"]
        self.assertEqual([(w["w"], w["s"]) for w in same], [("COME", 100), ("HERE", 300), ("NOW", 500)])
        other = mcp_server._caption_text_cues("COME ON", cues)[0]["words"]
        self.assertEqual([w["w"] for w in other], ["COME", "ON"])
        self.assertEqual((other[0]["s"], other[-1]["e"]), (100, 900), "spread over the heard speech")

    def test_line_edits(self):
        lines = [dict(line=14, text="Your turn, then.", from_ms=0, to_ms=216, cut_off=True, hidden=False),
                 dict(line=15, text="You're on your own.", from_ms=250, to_ms=2250, cut_off=False, hidden=False)]
        req = dict(moment=dict(idx=15), sub_text={"9": "old"})
        mcp_server._edit_lines(req, lines, hide=[15], text={"14": "Your move."})
        self.assertEqual(req["sub_text"], {"9": "old", "14": "Your move.", "15": None})
        mcp_server._edit_lines(req, lines, show=[15])
        self.assertNotIn("15", req["sub_text"])
        with self.assertRaises(Exception):
            mcp_server._edit_lines(req, lines, hide=[16])
        with self.assertRaises(Exception):
            mcp_server._edit_lines(req, [], hide=[14])

    def test_flash_at_the_edge_is_hidden(self):
        line = lambda n, a, b, cut=True: dict(line=n, text="x", from_ms=a, to_ms=b, cut_off=cut, hidden=False)  # noqa: E731
        lines = [line(14, 0, 216), line(15, 250, 2000), line(16, 2400, 2670), line(17, 1000, 1300, cut=False)]
        req = dict(moment=dict(idx=15))
        self.assertIn("line 14", mcp_server._hide_flashes(req, lines, keep={16}))
        self.assertEqual(req["sub_text"], {"14": None}, "kept, the moment and a short whole line stay")
        req = dict(moment=dict(idx=14))
        mcp_server._hide_flashes(req, lines)
        self.assertEqual(req["sub_text"], {"16": None}, "never the moment's own line")

    def test_clip_id_matches_server(self):
        self.assertRegex(mcp_server.clip_id("a.gif"), r"^c[0-9a-f]{8}$")


class ClipNames(unittest.TestCase):
    def test_a_refinement_never_replaces_a_clip(self):
        from saytheline import web
        with tempfile.TemporaryDirectory() as d:
            real, web.CLIPS = web.CLIPS, d
            try:
                open(os.path.join(d, "a - 0m41s.gif"), "w").close()
                self.assertEqual(web.version_name("a - 0m41s.gif", {}), "a - 0m41s.gif", "editor re-render")
                self.assertEqual(web.version_name("a - 0m41s.gif", {"parent": "c1"}), "a - 0m41s v2.gif")
                open(os.path.join(d, "a - 0m41s v2.gif"), "w").close()
                self.assertEqual(web.version_name("a - 0m41s.gif", {"parent": "c1"}), "a - 0m41s v3.gif")
                self.assertEqual(web.version_name("b.gif", {"parent": "c1"}), "b.gif")
            finally:
                web.CLIPS = real

    def test_speaker_dashes_left_out_of_the_name(self):
        from saytheline import web
        row = {"kind": "movie", "title": "Some Film", "year": 1999}
        self.assertEqual(web.clip_name(row, 75000, "- This is it! - Is it?", "gif"),
                         "Some Film (1999) - This is it! Is it - 1m15s.gif")
        self.assertEqual(web.clip_name(row, 75000, "Well-known line", "gif"), "Some Film (1999) - Well-known line - 1m15s.gif")


class CaptionEdits(unittest.TestCase):
    def test_word_cues_take_line_edits(self):
        from saytheline import web
        w = lambda *ws: [dict(w=x, s=i * 100, e=i * 100 + 90) for i, x in enumerate(ws)]  # noqa: E731
        cues = [dict(idx=14, start_ms=0, end_ms=500, text="Your play.", words=w("Your", "play.")),
                dict(idx=15, start_ms=500, end_ms=900, text="On your own.", words=w("On", "your", "own.")),
                dict(start_ms=900, end_ms=1000, text="no idx", words=[])]
        out = web.caption_cues(dict(caption_cues=cues, caption_source="subs", sub_text={"14": None, "15": "Alone now, then."}))
        self.assertEqual([c["text"] for c in out], ["Alone now, then.", "no idx"])
        self.assertEqual([x["w"] for x in out[0]["words"]], ["Alone", "now,", "then."])
        self.assertEqual((out[0]["words"][0]["s"], out[0]["words"][-1]["e"]), (0, 290), "spread over the heard words")
        same = web.caption_cues(dict(caption_cues=cues, caption_source="subs", sub_text={"15": "Off you go."}))
        self.assertEqual([x["s"] for x in same[1]["words"]], [0, 100, 200], "same count keeps the times")
        speech = web.caption_cues(dict(caption_cues=cues, caption_source="speech", sub_text={"14": None}))
        self.assertEqual(len(speech), 3, "a transcript isn't subtitle lines")


class DroppedG(unittest.TestCase):
    def test_search_tries_both_spellings(self):
        self.assertEqual(search.fts_query("how you doin", "phrase"), '(("how you doin" OR "how you doing"))')
        self.assertEqual(search.fts_query("nothin' to lose", "phrase"), '(("nothin to lose" OR "nothing to lose"))')
        for w in ("thing", "sing", "king", "bring", "tin", "skin"):         # not a dropped g
            self.assertEqual(search.spellings(w), [w])

    def test_speech_and_subtitles_compare_as_one_word(self):
        self.assertEqual({snap.norm(w) for w in ("doing", "doin'", "Doin", "DOING?")}, {"doin"})
        self.assertEqual(snap.norm("thing"), "thing")


class SearchSyntax(unittest.TestCase):
    def test_tiers(self):
        t = search.fts_tiers("an offer refuse", "phrase")
        self.assertEqual(len(t), 3)
        self.assertIn('"an offer refuse"', t[0])
        self.assertIn("NEAR(", t[1])
        self.assertIn("AND", t[2])

    def test_quotes_minus_and_prefix(self):
        t = search.fts_tiers('"an offer" refus* -godfather', "phrase")
        self.assertTrue(all('"an offer"' in x and 'NOT ("godfather")' in x for x in t))
        self.assertIn('"refus" *', t[0])
        self.assertEqual(search.fts_tiers('"exactly this"', "phrase"), ['("exactly this")'])

    def test_in_order(self):
        self.assertTrue(search._in_order("I'll make him an offer he can't refuse.", ["an", "offer", "refuse"]))
        self.assertFalse(search._in_order("Refuse? He made an offer.", ["an", "offer", "refuse"]))


class CaptionShift(unittest.TestCase):
    def test_shift_moves_lines_and_words(self):
        from saytheline import web
        cues = [dict(start_ms=1000, end_ms=2000, text="Hi there", words=[dict(w="Hi", s=1000, e=1400), dict(w="there", s=1400, e=2000)]),
                dict(start_ms=2500, end_ms=3000, text="Bye")]
        out = web.shift_cues(cues, 300)
        self.assertEqual([(c["start_ms"], c["end_ms"]) for c in out], [(1300, 2300), (2800, 3300)])
        self.assertEqual([(w["s"], w["e"]) for w in out[0]["words"]], [(1300, 1700), (1700, 2300)])
        self.assertNotIn("words", out[1])
        self.assertIs(web.shift_cues(cues, 0), cues)
        self.assertEqual(web.caption_shift({"caption_shift_ms": 9000}), 5000)


try:
    import PIL  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


@unittest.skipUnless(HAVE_PIL, "the laid-out looks need Pillow")
class Kinetic(unittest.TestCase):
    def cards(self, text):
        from unittest import mock
        from saytheline import kinetic, poster
        words = [dict(w=w, s=i * 300, e=i * 300 + 250) for i, w in enumerate(text.split())]
        tiers = lambda ws: [4 if w.endswith("!") else 3 if len(w) > 4 else 2 for w in ws]   # noqa: E731
        with mock.patch.object(poster, "tiers", tiers):         # no spaCy needed
            return kinetic.layout(words, 1280, 720, dict(size="m", font="Inter Black", position="middle"))

    def test_lockup_keeps_reading_order(self):
        cards = self.cards("Only this time it'll be really true.")
        ws = cards[0]["words"]
        self.assertEqual([x["w"] for x in ws], "Only this time it'll be really true.".split())
        vert = [x for x in ws if x["rot"]]
        self.assertEqual(len(vert), 1)
        self.assertIn(vert[0], (ws[0], ws[-1]), "only a first or last word turns vertical")
        if vert[0] is ws[-1]:
            self.assertGreater(vert[0]["cx"], max(x["cx"] for x in ws[:-1]), "the last word stands on the right")

    def test_fits_the_frame(self):
        for c in self.cards("Never mind that. All right. I'll give you a story. A wonderful story! Only this time it'll be true."):
            for x in c["words"]:
                self.assertTrue(0 < x["cx"] < 1280 and 0 < x["cy"] < 720, x)

    def test_motion(self):
        from saytheline import kinetic
        card = self.cards("You'll never find me now!")[0]
        x = card["words"][1]
        self.assertIsNone(kinetic.word_state(card, x, x["s"] - 1), "not before it is said")
        settled = kinetic.word_state(card, x, x["s"] + 600)
        self.assertIsNone(settled["clip"])
        self.assertAlmostEqual(settled["alpha"], 1)
        gone = card["end_ms"] + kinetic.EXIT_STAGGER_MS * x["k"] + kinetic.EXIT_MS
        self.assertIsNone(kinetic.word_state(card, x, gone))
        self.assertGreater(kinetic.word_state(card, x, gone - 50)["cy"], settled["cy"], "it falls away")


@unittest.skipUnless(HAVE_PIL, "the laid-out looks need Pillow")
class Intense(unittest.TestCase):
    def cards(self, text):
        from unittest import mock
        from saytheline import intense, poster
        words = [dict(w=w, s=i * 300, e=i * 300 + 250) for i, w in enumerate(text.split())]
        tiers = lambda ws: [4 if w.endswith("!") else 3 if len(w) > 4 else 2 for w in ws]   # noqa: E731
        with mock.patch.object(poster, "tiers", tiers):         # no spaCy needed
            return intense.layout(words, 1280, 720, dict(size="m", font="Inter Black", position="middle"))

    def test_lockup_keeps_reading_order(self):
        cards = self.cards("Only this time it'll be really true.")
        ws = cards[0]["words"]
        self.assertEqual([x["w"] for x in ws], "Only this time it'll be really true.".split())
        vert = [x for x in ws if x["rot"]]
        self.assertEqual(len(vert), 1)
        self.assertIn(vert[0], (ws[0], ws[-1]), "only a first or last word turns vertical")
        if vert[0] is ws[-1]:
            self.assertGreater(vert[0]["cx"], max(x["cx"] for x in ws[:-1]), "the last word stands on the right")

    def test_fits_the_frame(self):
        for c in self.cards("Never mind that. All right. I'll give you a story. A wonderful story! Only this time it'll be true."):
            for x in c["words"]:
                self.assertTrue(0 < x["cx"] < 1280 and 0 < x["cy"] < 720, x)

    def loud_cards(self, said):
        from unittest import mock
        from saytheline import intense, poster
        words = [dict(w=w, s=i * 300, e=i * 300 + 250, loud=l) for i, (w, l) in enumerate(said)]
        with mock.patch.object(poster, "tiers", lambda ws: [2] * len(ws)):
            return intense.layout(words, 1280, 720, dict(size="m", font="Inter Black", position="middle"))

    def test_loudness_and_texture(self):
        ws = self.loud_cards([("you", 0), ("never", 8), ("listen", 0), ("to", -1), ("me!", 1)])[0]["words"]
        never = next(x for x in ws if x["w"] == "never")
        self.assertEqual((never["tier"], never["enter"]), (4, "slam"), "a shout lands big")
        self.assertTrue(never["onbar"], "a shouted key word sits on the bar")
        quiet = self.loud_cards([("keep", 0), ("it", -14), ("down", -13), ("please.", 0)])[0]["words"]
        self.assertEqual([x["w"] for x in quiet if x["whisper"]], ["it", "down"])
        alone = self.loud_cards([("keep", 0), ("it", -14), ("going", 0), ("now.", 0)])[0]["words"]
        self.assertFalse(any(x["whisper"] for x in alone), "one soft word isn't a whisper")

    def test_exit_follows_the_punctuation(self):
        for end, exit in (("now!", "scatter"), ("now?", "push"), ("now.", "fall")):
            self.assertEqual(self.cards(f"You'll never find me {end}")[0]["exit"], exit)

    def test_motion(self):
        from saytheline import intense
        card = self.cards("You'll never find me now!")[0]
        x = card["words"][1]
        self.assertIsNone(intense.word_state(card, x, x["s"] - 1), "not before it is said")
        settled = intense.word_state(card, x, x["s"] + 600)
        self.assertIsNone(settled["clip"])
        self.assertAlmostEqual(settled["alpha"], 1)
        gone = card["end_ms"] + intense.EXIT_STAGGER_MS * x["k"] + intense.EXIT_MS
        self.assertIsNone(intense.word_state(card, x, gone))
        self.assertGreater(intense.word_state(card, x, gone - 50)["cy"], settled["cy"], "it falls away")


class Studio(unittest.TestCase):
    def test_only_lines_mostly_inside_the_cut(self):
        from saytheline import web
        cues = [dict(start_ms=0, end_ms=1000, text="the end of the line before"),
                dict(start_ms=1000, end_ms=3000, text="the line itself"),
                dict(start_ms=2800, end_ms=6000, text="the next one, barely started")]
        self.assertEqual([c["text"] for c in web.lines_mostly_in(cues, 800, 3300)], ["the line itself"])


class Music(unittest.TestCase):
    song = dict(bpm=120.0, duration=20.0, beats=[0.25 + 0.5 * k for k in range(39)], downbeats=[0.25 + 2 * k for k in range(10)])

    def test_joins_land_on_the_beat(self):
        from saytheline import music
        beats = music.grid(self.song)
        # clips 1.9 s and 2.2 s long, the song starting 1 s in: joins move onto beats, by under half a beat
        d = music.snap_joins([1.9, 2.2], beats, 1.0, 0.25)
        ends = [1.9 + d[0], 1.9 + d[0] + 2.2 + d[1]]
        for e in ends:
            self.assertAlmostEqual((e + 1.0 - 0.25) / 0.5 % 1, 0, delta=0.002, msg=ends)
        self.assertTrue(all(abs(x) <= 0.25 for x in d))
        self.assertEqual(music.snap_joins([1.9], beats, 1.0, 0.05), [0.0], "too far to move: left alone")

    def test_bpm_set_by_hand_keeps_the_bar(self):
        from saytheline import music
        g = music.grid(self.song, bpm=100)
        self.assertAlmostEqual(g[1] - g[0], 0.6, places=3)
        self.assertIn(0.25, g, "the grid runs through the detected first downbeat")
        bars = music.grid(self.song, bpm=100, bars=True)
        self.assertAlmostEqual(bars[1] - bars[0], 2.4, places=3)
        self.assertIn(0.25, bars)


def _has_numpy(py):
    import subprocess
    try:
        return subprocess.run([py, "-c", "import numpy"], capture_output=True, timeout=30).returncode == 0
    except OSError:
        return False


BEAT_PY = config.stt_python()


@unittest.skipUnless(_has_numpy(BEAT_PY), "beat finding needs numpy in the speech Python")
class Beats(unittest.TestCase):
    def test_click_track(self):
        """A 128 BPM click track with a kick on each bar: tempo, beat times and bar starts."""
        import json, subprocess
        gen = (
            "import sys, wave, numpy as np\n"
            "sr, bpm = 22050, 128.0; x = 0.02 * np.random.default_rng(1).standard_normal(sr * 20); k = 0\n"
            "while True:\n"
            "    s = int((0.37 + k * 60 / bpm) * sr); n = int(0.05 * sr)\n"
            "    if s + n >= len(x): break\n"
            "    f = 60 if k % 4 == 0 else 2000\n"
            "    x[s:s + n] += 0.6 * np.exp(-np.arange(n) / (0.01 * sr)) * np.sin(2 * np.pi * f * np.arange(n) / sr); k += 1\n"
            "w = wave.open(sys.argv[1], 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)\n"
            "w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()); w.close()\n")
        with tempfile.TemporaryDirectory() as d:
            wav = os.path.join(d, "click.wav")
            subprocess.run([BEAT_PY, "-c", gen, wav], check=True)
            worker = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "saytheline", "beat_worker.py")
            out = json.loads(subprocess.run([BEAT_PY, "-I", worker, wav], capture_output=True, text=True, check=True).stdout)
        self.assertAlmostEqual(out["bpm"], 128, delta=1)
        beat = 60 / 128
        errs = [(b - 0.37) - round((b - 0.37) / beat) * beat for b in out["beats"]]
        self.assertLess(max(abs(e) for e in errs), 0.03, "every beat within ~a frame")
        self.assertTrue(all(round((b - 0.37) / beat) % 4 == 0 for b in out["downbeats"]), "bars start on the kicks")


class SpeedRamp(unittest.TestCase):
    def test_every_beat_averages_normal_speed(self):
        from saytheline import ramp
        for st in ramp.STRENGTH:
            for sh in ramp.SHAPES:
                for d in (True, False):
                    vs = [ramp.speed((i + 0.5) / 500, st, sh, d) for i in range(500)]
                    self.assertAlmostEqual(sum(vs) / len(vs), 1, places=2)
        self.assertGreater(ramp.speed(0, "strong"), 1.5, "fast on the beat")
        self.assertLess(ramp.speed(0.5, "strong"), 0.5, "slow between")

    def test_source_times_cover_the_clip_in_step_with_the_beat(self):
        from saytheline import ramp
        beats = [0.5 * k for k in range(12)]
        t = ramp.source_times(4.0, 4.0, beats, dict(strength="strong", shape="smooth", peak="on"))
        self.assertEqual(len(t), 120)
        self.assertTrue(all(b > a for a, b in zip(t, t[1:])))
        self.assertAlmostEqual(t[-1], 4.0, delta=0.1)          # the middle of a fast last frame
        for b in beats[1:8]:              # on each beat the picture is back in step with the song
            self.assertAlmostEqual(t[int(b * 30)], b, delta=0.07)     # half a 3.5x frame past it
        self.assertAlmostEqual(ramp.map_time(t[45], t), 45.5 / 30, places=3)

    def test_peak_lands_just_before_the_beat(self):
        from saytheline import ramp
        beats = [0.5 * k for k in range(12)]
        for peak, lead in ramp.PEAK_LEAD.items():
            t = ramp.source_times(4.0, 4.0, beats, dict(strength="strong", peak=peak))
            for b in beats[2:7]:          # the fastest frame around each beat
                f = max(range(int((b - 0.25) * 30), int((b + 0.25) * 30)), key=lambda f: t[f + 1] - t[f])
                self.assertAlmostEqual((f + 1) / 30, b - lead, delta=0.04, msg=peak)

    def test_effect_stretches_split_across_clips(self):
        from saytheline import ramp
        fx = ramp.fx_spans([[9, 14], [3, 5], [4.5, 6], [20, 20.1]], 16)
        self.assertEqual(fx, [(3.0, 6.0), (9.0, 14.0)])          # merged, sorted, slivers dropped
        self.assertEqual(ramp.fx_spans(None, 8), [(0.0, 8.0)])     # none given: the whole cut
        self.assertEqual(ramp.fx_part(fx, 4.0, 6.0), [(0.0, 2.0), (5.0, 6.0)])   # a clip from 4 to 10 s
        self.assertEqual(ramp.fx_part(fx, 6.0, 3.0), [])                         # 6-9 s: outside it
        self.assertEqual(ramp.with_outside([(2.5, 3.0)], [(0.0, 2.0), (5.0, 6.0)], 6.0), [(2.0, 5.0)])
        # outside the effect a clip plays at normal speed, frame for frame
        beats = [0.5 * k for k in range(14)]
        t = ramp.source_times(6.0, 6.0, beats, dict(strength="extreme"), keep=ramp.with_outside([], [(0.0, 3.0)], 6.0))
        for f in range(91, 180):
            self.assertAlmostEqual(t[f], (f + 0.5) / 30, places=6)
        self.assertGreater(max(b - a for a, b in zip(t[:80], t[1:81])) * 30, 3, "ramped inside it")

    def test_strengths_are_far_apart(self):
        from saytheline import ramp
        peak = {st: ramp.speed(0, st) for st in ramp.STRENGTH}
        dip = {st: ramp.speed(0.5, st) for st in ramp.STRENGTH}
        self.assertLess(peak["subtle"] * 1.5, peak["strong"])
        self.assertLess(peak["strong"] * 1.5, peak["extreme"])
        self.assertGreaterEqual(peak["extreme"], 5.9)
        self.assertLessEqual(dip["extreme"], 0.16)

    def test_kept_lines_play_at_normal_speed_in_step(self):
        from saytheline import ramp
        cues = [{"start_ms": 11000, "end_ms": 12000}, {"start_ms": 12300, "end_ms": 12800}, {"start_ms": 14500, "end_ms": 14900}]
        keep = ramp.keep_spans(cues, 10000, 16000)
        self.assertEqual(keep, [(0.92, 2.95), (4.42, 5.05)])   # the near two joined
        beats = [0.5 * k for k in range(14)]
        t = ramp.source_times(6.0, 6.0, beats, dict(strength="extreme"), keep=keep)
        self.assertEqual(len(t), 180)
        self.assertTrue(all(b > a for a, b in zip(t, t[1:])))
        for f in range(29, 88):                     # inside the kept line: shown as it was
            self.assertAlmostEqual(t[f], (f + 0.5) / 30, places=6)
        self.assertGreater(max(b - a for a, b in zip(t[:27], t[1:28])) * 30, 2, "ramped before the line")
        self.assertAlmostEqual(t[-1], 6.0, delta=0.11)          # the middle of a 6x last frame


class BluraySubtitles(unittest.TestCase):
    def test_picture_lines_decode_with_their_times(self):
        import struct
        from saytheline import pgs
        if pgs.Image is None:
            self.skipTest("needs pillow")

        def seg(kind, pts_ms, data):
            return b"PG" + struct.pack(">II", pts_ms * 90, 0) + bytes([kind]) + struct.pack(">H", len(data)) + data
        # 4x2 picture: row 1 = two clear pixels then two of colour 1; row 2 = four of colour 1
        rle = bytes([0, 0x02, 1, 1, 0, 0]) + bytes([0, 0x84, 1, 0, 0])
        pcs = lambda n: struct.pack(">HHBHBBBB", 1920, 1080, 0x10, 0, 0x80, 0, 0, n) + (struct.pack(">HBBHH", 0, 0, 0, 100, 900) if n else b"")
        pds = bytes([0, 0]) + bytes([1, 235, 128, 128, 255])          # colour 1: white, opaque
        ods = struct.pack(">HBB", 0, 0, 0xC0) + (len(rle) + 4).to_bytes(3, "big") + struct.pack(">HH", 4, 2) + rle
        sup = (seg(0x16, 1000, pcs(1)) + seg(0x14, 1000, pds) + seg(0x15, 1000, ods) + seg(0x80, 1000, b"")
               + seg(0x16, 2500, pcs(0)) + seg(0x80, 2500, b""))
        with tempfile.TemporaryDirectory() as d:          # not NamedTemporaryFile: Windows can't reopen it
            path = os.path.join(d, "t.sup")
            with open(path, "wb") as f:
                f.write(sup)
            lines = pgs.read_sup(path)
        self.assertEqual(len(lines), 1)
        a, b, pics = lines[0]
        self.assertEqual((a, b), (1000, 2500))
        x, y, im = pics[0]
        self.assertEqual((x, y, im.size), (100, 900, (4, 2)))
        self.assertEqual(list(im.getdata()), [255, 255, 20, 20, 20, 20, 20, 20])   # white letters drawn black
        self.assertEqual(pgs._ts(3723456), "01:02:03,456")

    def test_dvd_picture_decodes_with_its_times(self):
        import struct
        from saytheline import pgs, vobsub
        if pgs.Image is None:
            self.skipTest("needs pillow")
        # 4x2 picture, fields apart: top row = two clear then two of colour 1 (runs 2<<2|0, 2<<2|1),
        # bottom row = four of colour 1 (a two-nibble run, 4<<2|1)
        fields = bytes([0x89, 0x11])
        x1, x2, y1, y2 = 100, 103, 900, 901
        area = bytes([x1 >> 4, (x1 & 15) << 4 | x2 >> 8, x2 & 255, y1 >> 4, (y1 & 15) << 4 | y2 >> 8, y2 & 255])
        ctrl = 4 + len(fields)
        cmds = (bytes([0x03, 0x32, 0x10, 0x04, 0xFF, 0xF0, 0x05]) + area + bytes([0x06]) + struct.pack(">HH", 4, 5)
                + bytes([0x01, 0xFF]))
        hide_at = ctrl + 4 + len(cmds)
        spu = (struct.pack(">HH", 0, ctrl) + fields + struct.pack(">HH", 0, hide_at) + cmds
               + struct.pack(">HH", 90, hide_at) + bytes([0x02, 0xFF]))            # hidden 90 x 1024/90000 s on
        got = vobsub._spu(spu)
        self.assertEqual(got[:6], (0, 1024, 100, 900, 4, 2))
        im = vobsub._picture(spu, got, [0x000000, 0xFFFFFF])
        self.assertEqual(list(im.getdata()), [255, 255, 0, 0, 0, 0, 0, 0])   # white letters drawn black


class TimingLog(unittest.TestCase):
    def test_summary_finds_slow_routes_repeats_and_cache_misses(self):
        import tempfile
        from saytheline import perf
        with tempfile.TemporaryDirectory() as d:
            old = perf.PATH, perf.OLD, perf.MAX_BYTES
            perf.PATH, perf.OLD = os.path.join(d, "perf.jsonl"), os.path.join(d, "perf.1.jsonl")
            try:
                for ms, hit in ((900, False), (12, True), (15, True)):
                    perf.begin(); perf.cache("preview", hit)
                    perf.write(dict(ev="req", m="GET", r=perf.route("/preview/5/100-200.mp4"), sig=perf.sig("/preview/5/100-200.mp4"),
                                    code=200, ms=ms, bytes=2048, cache=perf.end()))
                perf.write(dict(ev="req", m="GET", r=perf.route("/api/search"), sig=perf.sig("/api/search?q=a"), code=200, ms=40, bytes=10))
                for _ in range(2):
                    perf.write(dict(ev="job", kind="snap", key="snap", sig=perf.sig({"item": 1}), units=3, state="done",
                                    secs=4.0, wait=1.0, phases={"listening": 3.5}))
                perf.cache("shots", False)                       # outside a request: its own line
                s = perf.summary()
                top = s["routes"][0]
                self.assertEqual((top["route"], top["n"], top["repeats"], top["max_ms"]), ("GET /preview/#/#-#.mp4", 3, 2, 900))
                self.assertEqual(top["cache"]["preview"], {"hit": 2, "miss": 1})
                self.assertEqual(s["caches"]["shots"], {"hit": 0, "miss": 1, "rate": 0.0})
                self.assertEqual(s["jobs"][0]["repeats"], 1)
                self.assertEqual(s["jobs"][0]["steps"], {"listening": 3.5})
                perf.MAX_BYTES = 100                              # rolls over instead of growing
                for _ in range(5):
                    perf.write(dict(ev="cache", cache="x", hit=True))
                self.assertTrue(os.path.exists(perf.OLD))
                self.assertLess(os.path.getsize(perf.PATH), 300)
            finally:
                perf.PATH, perf.OLD, perf.MAX_BYTES = old


class TimeLeft(unittest.TestCase):
    def test_counters_and_history_give_time_left(self):
        from saytheline import web
        from unittest import mock
        job = web.Job(id="x", kind="studio", state="queued", _key="studio:t", _units=10.0,
                      _phases=web.PHASES["studio:ramp"])
        self.assertNotIn("pct", web.eta_view(job))
        job.update(state="rendering", done=0, total=2, frames=50, frames_total=100)
        t0 = job["_t0"]
        v = web.eta_view(job, now=t0 + 10)            # 5% + 93% x 25% done after 10 s
        self.assertEqual(v["pct"], 28)
        self.assertAlmostEqual(v["eta_s"], 10 / 0.2825 - 10, delta=1)
        self.assertFalse(any(k.startswith("_") for k in v))
        with mock.patch.dict(web.RATES, {"studio:t": 3.0}):      # 30 s expected, early on: mostly history
            j2 = web.Job(id="y", kind="studio", state="timing", done=0, total=2, _key="studio:t", _units=10.0,
                         _phases=web.PHASES["studio:ramp"])
            self.assertEqual(web.eta_view(j2, now=j2["_t0"] + 3)["eta_s"], 27)

    def test_queued_waits_for_whats_ahead(self):
        from saytheline import web
        from unittest import mock
        run = web.Job(id="a", kind="render", state="rendering", _key="render", _units=1, _phases=[])
        run.update(_eta=40.0, _eta_t=0)
        q = web.Job(id="b", kind="render", state="queued", _key="render", _units=1, _phases=[])
        with mock.patch.dict(web.JOBS, {"a": run, "b": q}, clear=True), mock.patch.dict(web.RATES, {"render": 5.0}):
            self.assertEqual(web.eta_view(q)["eta_s"], 45)


class AudioTracks(unittest.TestCase):
    def tracks(self, *tags):
        from unittest import mock
        return mock.patch.object(media, "audio_tracks", lambda path: tags)

    def test_english_track_wins_over_a_dub_ahead_of_it(self):
        with self.tracks("por", "eng"):
            self.assertEqual(media.audio_track("x"), 1)
            self.assertIsNone(media.foreign_audio("x"))

    def test_foreign_only_when_tagged(self):
        with self.tracks("jpn"):
            self.assertEqual((media.audio_track("x"), media.foreign_audio("x")), (0, "jpn"))
        for tags in [("und",), ("",), (), ("eng", "kor")]:
            with self.tracks(*tags):
                self.assertIsNone(media.foreign_audio("x"), tags)


if __name__ == "__main__":
    unittest.main()


STT_PY = config.stt_python()
WORKER_CHECK = r"""
import json, sys
import numpy as np
sys.path.insert(0, sys.argv[1])
import stt_worker as w
# 10 ms frames: a loud line 0-1 s, a faint noise 1.4-1.8 s, silence, then speech 3.0-4.0 s
db = np.full(500, 30.0); db[0:100] = 65; db[140:180] = 45; db[300:400] = 65
words = [{"w": "Say", "s": 0, "e": 500}, {"w": "it.", "s": 500, "e": 1000},
         {"w": "I", "s": 1400, "e": 1860}, {"w": "didn't", "s": 1860, "e": 3500},
         {"w": "do", "s": 3500, "e": 3700}, {"w": "it.", "s": 3700, "e": 4000}]
print(json.dumps(w.loudness(w.place_strays(w.refine_starts(words, db), db), db)))
"""


@unittest.skipUnless(os.path.exists(STT_PY), "the speech Python is not installed")
class WordOnsets(unittest.TestCase):
    def test_a_stretched_word_and_a_word_on_a_noise_move_to_the_speech(self):
        import json
        import subprocess
        r = subprocess.run([STT_PY, "-c", WORKER_CHECK, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "saytheline")],
                           capture_output=True, text=True, check=True)
        ws = {(x["w"], i): x for i, x in enumerate(json.loads(r.stdout))}
        didnt, i = ws[("didn't", 3)], ws[("I", 2)]
        self.assertEqual(didnt["s"], 3000)          # 1.1 s into the word: past the old 0.6 s look
        self.assertEqual((i["s"], i["e"]), (2900, 3000))
        self.assertEqual(ws[("Say", 0)]["s"], 0)    # already loud: left alone


class IndexOrder(unittest.TestCase):
    def test_subtitle_files_then_embedded_each_newest_first(self):
        from unittest import mock
        from saytheline import index
        srt = "1\n00:00:01,000 --> 00:00:02,000\nWalter, you're the only one I can trust.\n"
        with tempfile.TemporaryDirectory() as d:
            def item(name, added, embedded):
                path = os.path.join(d, name + ".mkv")
                open(path, "w").close()
                if not embedded:
                    with open(os.path.join(d, name + ".en.srt"), "w") as f:
                        f.write(srt)
                return dict(jf_id=name, kind="movie", series=None, season=None, episode=None, title=name,
                            year=None, path=path, runtime_s=None, added=added,
                            embedded=[(2, "subrip")] if embedded else [])
            items = [item("old-file", "2020", False), item("new-track", "2026", True), item("new-file", "2026", False)]
            items.sort(key=lambda i: i["added"], reverse=True)       # as jellyfin.list_items returns them
            seen = []
            with mock.patch.object(index.jellyfin, "list_items", return_value=items), \
                    mock.patch.object(index.subs, "extract_embedded", return_value=srt), \
                    mock.patch.object(index.subs, "is_dialogue", return_value=True), \
                    mock.patch.object(index.db, "path_map", return_value=[]):
                index.index(os.path.join(d, "cues.db"), movies=True, log=seen.append)
        self.assertEqual([s.split(":")[0].strip() for s in seen], ["new-file", "old-file", "new-track"])


class JellyfinSubtitleCache(unittest.TestCase):
    def test_uses_a_cached_track_only_when_newer_than_the_video(self):
        with tempfile.TemporaryDirectory() as d:
            jf = "a811bbbbb08fb74e2045c18d4bf8c446"
            folder = os.path.join(d, "a8", "a811bbbb-b08f-b74e-2045-c18d4bf8c446")
            os.makedirs(folder)
            video, srt = os.path.join(d, "ep.mkv"), os.path.join(folder, "3.srt")
            for p in (video, srt):
                with open(p, "w") as f:
                    f.write("x")
            os.utime(video, (1000, 1000))
            os.utime(srt, (2000, 2000))
            self.assertEqual(jellyfin.cached_subtitle(d, jf, 3, "subrip", video), srt)
            self.assertIsNone(jellyfin.cached_subtitle(d, jf, 4, "subrip", video), "other stream")
            self.assertIsNone(jellyfin.cached_subtitle(d, jf, 3, "ass", video), "ASS is cached as .ass")
            os.utime(video, (3000, 3000))           # the file was replaced after Jellyfin extracted
            self.assertIsNone(jellyfin.cached_subtitle(d, jf, 3, "subrip", video))


class SayAnything(unittest.TestCase):
    def facts(self, text, start=1000, gap=300):
        """A line heard word by word, 250 ms a word, with silence either side."""
        ws, t = [], start
        for w in text.split():
            ws.append(dict(w=w, s=t, e=t + 230))
            t += 250
        return dict(found=True, line_s=start, line_e=t, words=ws)

    def test_hyphens_split_and_punctuation_is_ignored(self):
        from saytheline import sayany
        self.assertEqual([n for _, n in sayany.words('say-the-line has a "new" feature.')],
                         ["say", "the", "line", "has", "a", "new", "feature"])

    def test_longest_heard_phrase_wins_and_unknown_words_say_why(self):
        from saytheline import sayany
        lines = {"walter": ["Walter."], "you're": ["You're the danger."], "you're the danger": ["You're the danger."],
                 "the danger": ["You're the danger."], "the": ["The."], "danger": ["Danger."]}

        def find(q):
            return [dict(item_id=1, start_ms=sorted(lines).index(q) * 10000 + k, text=t) for k, t in enumerate(lines.get(q, []))]
        heard = []

        def listen(h, text):
            heard.append(text)
            return self.facts(h["text"])
        pieces = sayany.plan("Walter, you're the danger, zorp", find, listen)
        self.assertEqual([p["text"] for p in pieces], ["Walter,", "you're the danger,", "zorp"])
        self.assertIsNone(pieces[-1]["take"])
        self.assertEqual(pieces[-1]["why"], "none")

    def test_a_long_sentence_still_tries_every_word(self):
        from saytheline import sayany
        words = " ".join(f"w{k}" for k in range(60))

        def find(q):              # a different line for every phrase (the planner reuses a line it has heard)
            return [dict(item_id=1, start_ms=sum(map(ord, q)) * 1000 + len(q) * 10 + k, text=q) for k in range(8)]
        calls = []

        def listen(h, text):
            calls.append(text)
            return dict(found=False)          # nothing is ever heard: the worst case for the budget
        pieces = sayany.plan(words, find, listen)
        self.assertEqual(len(pieces), 60)
        self.assertLessEqual(len(calls), sayany.MAX_LISTENS + sayany.TRIES + 60)
        self.assertEqual(pieces[-1]["why"], "rushed")
        self.assertTrue(pieces[-1]["alts"], "the untried lines stay, for the › button")

    def test_candidates_are_the_exact_words_and_not_one_chant(self):
        from saytheline import sayany
        # search matches stems: "chocolates" finds "chocolate" lines, which can never be the take
        pool = [dict(item_id=1, text="Chocolate!"), dict(item_id=2, text="a box of chocolates."),
                dict(item_id=3, text="Chocolates."), dict(item_id=4, text="chocolate. I like chocolate.")]
        self.assertEqual([h["item_id"] for h in sayany.pick(pool, ["chocolates"])], [3, 2])
        # one scene's "Eat! Eat! Eat!" four times over, then lines that say it once
        pool = [dict(item_id=7, idx=k, text="Eat! Eat! Eat!") for k in range(4)] + [
            dict(item_id=8, text="♪ eat ♪"), dict(item_id=9, text="You should eat something."), dict(item_id=10, text="Eat.")]
        got = sayany.pick(pool, ["eat"])
        self.assertEqual([h["item_id"] for h in got[:2]], [10, 9])
        self.assertEqual(sum(h["item_id"] == 7 for h in got), sayany.PER_TITLE)


class Settings(unittest.TestCase):
    def test_toml_with_a_bom_loads(self):
        # Windows editors (and PowerShell 5's Set-Content -Encoding utf8) start the file with a BOM
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "saytheline.toml")
            with open(p, "w", encoding="utf-8-sig") as f:
                f.write('[server]\nport = 9876\n[paths]\ntmp = "t"\n')
            with mock.patch.dict(os.environ, {"SAYTHELINE_CONFIG": p}), mock.patch.object(config.tempfile, "tempdir"):
                for k in ("SAYTHELINE_PORT", "SAYTHELINE_TMP"):
                    os.environ.pop(k, None)
                config.load()
                self.assertEqual(os.environ["SAYTHELINE_PORT"], "9876")
                self.assertEqual(os.environ["SAYTHELINE_TMP"], os.path.join(d, "t"), "relative to the file")
                self.assertEqual(config.tempfile.tempdir, os.path.join(d, "t"))

    def test_gpu_settings_default_off(self):
        from unittest import mock
        with mock.patch.dict(os.environ):
            for k in ("SAYTHELINE_DEMUCS_DEVICE", "SAYTHELINE_ENCODER"):
                os.environ.pop(k, None)
            self.assertEqual(config.demucs_device(), "cpu")
            self.assertEqual(config.encoder(), "")
        cpu = ["-c:v", "libx264", "-crf", "20"]
        with mock.patch.object(media, "ENCODER", ""):
            media.nvenc_check.cache_clear()
            self.assertEqual(media.h264(20, cpu), cpu)
        media.nvenc_check.cache_clear()


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


class Login(unittest.TestCase):
    def test_token_goes_under_jellyfin_and_nothing_else_moves(self):
        from saytheline import login
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "saytheline.toml")
            with open(p, "w", encoding="utf-8") as f:
                f.write('[jellyfin]\nurl = "http://jf:8096"\napi_key = ""   # paste\n\n[server]\nport = 8765\n')
            login.save_key(p, "http://jf:8096", "abc123")
            self.assertEqual(_read(p),
                             '[jellyfin]\nurl = "http://jf:8096"\napi_key = "abc123"\n\n[server]\nport = 8765\n')
            os.remove(p)
            login.save_key(p, "http://jf:8096", "abc123")       # no file yet: a minimal one
            self.assertEqual(_read(p), '[jellyfin]\nurl = "http://jf:8096"\napi_key = "abc123"\n\n')


class LaserEyes(unittest.TestCase):
    def test_part_is_kept_inside_the_cut(self):
        from saytheline import laser
        self.assertIsNone(laser.part_of({}, 1000, 5000))
        self.assertIsNone(laser.part_of({"laser": {"style": ""}}, 1000, 5000))
        self.assertEqual(laser.part_of({"laser": {"style": "flare", "a_ms": 0, "b_ms": 9000}}, 1000, 5000), (0, 4000, "flare", None))
        a, b, style, pick = laser.part_of({"laser": {"style": "beams", "a_ms": 2000, "b_ms": 3000, "pick": {"x": 1.4, "y": 0.25}}}, 1000, 5000)
        self.assertEqual((a, b, style, pick), (1000, 2000, "beams", (1.0, 0.25)))
        with self.assertRaises(ValueError):
            laser.part_of({"laser": {"style": "flare", "a_ms": 4900, "b_ms": 5000}}, 1000, 5000)
        with self.assertRaises(ValueError):
            laser.part_of({"laser": {"style": "rainbow"}}, 1000, 5000)

    def test_tapped_face_lands_on_the_same_spot_in_the_output(self):
        from saytheline import laser
        pt = lambda opts, w, h, p: [round(v, 1) for v in laser.to_output(1920, 1080, opts, w, h, p)]
        self.assertIsNone(laser.to_output(1920, 1080, {}, 1280, 720, None))
        self.assertEqual(pt({}, 1280, 720, (0.25, 0.5)), [320, 360])
        # 9:16, centred crop: the middle of the picture is the middle of the output
        w, h, _ = media.geometry(1920, 1080, "9:16", "crop", 720)
        self.assertEqual(pt({"aspect": "9:16"}, w, h, (0.5, 0.5)), [w / 2, h / 2])
        self.assertEqual(pt({"aspect": "9:16"}, w, h, (0.5 - 303.75 / 1920, 0.0)), [0, 0])     # the crop's left edge
        # bars: 4:3 picture in a 16:9 frame keeps its middle, and its left edge sits right of the bar
        self.assertEqual(laser.to_output(1440, 1080, {"aspect": "16:9", "fit": "pad"}, 1280, 720, (0, 0.5)), [160, 360])
        # a crop window moved by hand
        crop = {"x": 0.25, "y": 0.5, "s": 0.5}
        w, h, _ = media.geometry(1920, 1080, "1:1", "crop", 720, crop=crop)
        cw, ch, cx, cy = media.crop_box(1920, 1080, 1.0, crop)
        self.assertEqual(pt({"aspect": "1:1", "crop": crop}, w, h, ((cx + cw / 2) / 1920, (cy + ch / 2) / 1080)), [w / 2, h / 2])


class SoundEffects(unittest.TestCase):
    def test_hits_follow_the_lines_inside_the_cut(self):
        from saytheline import sfx
        cues = [dict(start_ms=900, end_ms=1500, text="a"),            # ends before the cut: no hit
                dict(start_ms=2000, end_ms=3000, text="b", words=[dict(w="b", s=2000, e=2600)]),
                dict(start_ms=2700, end_ms=2900, text="c"),            # ends within the gap of "b"'s hit: shares it
                dict(start_ms=4000, end_ms=5000, text="d"),
                dict(start_ms=7000, end_ms=9500, text="e")]            # still being said at the out point
        spec = lambda when, given=None, mark=None: ("boom", given, when, 200, 0.6, mark)
        self.assertEqual(sfx.times(spec("lines"), cues, 2000, 8000), [2800, 5200])   # "b" ends at its last word
        self.assertEqual(sfx.times(spec("lines"), [dict(cues[1], words=None), cues[2]], 2000, 8000), [3100])
        self.assertEqual(sfx.times(spec("last"), cues, 2000, 8000), [5200])
        self.assertEqual(sfx.times(spec("laser"), cues, 2000, 8000, laser_a=2500), [2700])
        self.assertEqual(sfx.times(spec("laser"), cues, 2000, 8000), [])
        self.assertEqual(sfx.times(spec("mark", mark=6100), cues, 2000, 8000), [6100])
        self.assertEqual(sfx.times(spec("lines", given=[9000, 2100, 2300, 7999]), cues, 2000, 8000), [2100, 7999])


class Aesthetics(unittest.TestCase):
    def test_grades_scale_with_the_output(self):
        from saytheline import aesthetic
        self.assertEqual(aesthetic.filters("none", 1280, 720), [])
        self.assertEqual(aesthetic.of({}), "none")
        with self.assertRaises(ValueError):
            aesthetic.of({"aesthetic": "sepia"})
        small, big = aesthetic.filters("punchy", 854, 480), aesthetic.filters("punchy", 1920, 1080)
        self.assertNotEqual(small, big)                     # the clarity radius follows the size
        self.assertTrue(all("unsharp=lx=" not in f or int(f.split("lx=")[1].split(":")[0]) % 2 for f in small + big))
        self.assertIn("rgbashift=rh=-3:bh=3", aesthetic.filters("grunge", 1920, 1080))


class Wasted(unittest.TestCase):
    def test_the_ending_starts_inside_the_cut(self):
        from saytheline import wasted
        self.assertIsNone(wasted.spec_of({}, 1000, 9000))
        self.assertEqual(wasted.spec_of({"wasted": {}}, 1000, 9000), (6.5, 1.5, "WASTED", True))
        self.assertEqual(wasted.spec_of({"wasted": {"text": "  so   busted ", "at_ms": 3000, "hold_ms": 99999, "sound": False}}, 1000, 9000),
                         (2.0, 4.0, "so busted", False))
        self.assertIsNone(wasted.spec_of({"wasted": {"at_ms": 500}}, 1000, 9000))       # a preview elsewhere
        self.assertEqual(wasted.spec_of({"wasted": {}}, 1000, 1800)[0], 0)              # short clip: from the start
        with tempfile.TemporaryDirectory() as tmp:
            fs = wasted.filters((2.0, 1.5, "busted", True), 1280, 720, tmp, 5.0)
            with open(os.path.join(tmp, "wasted.txt")) as f:
                self.assertEqual(f.read(), "BUSTED")
        self.assertTrue(fs[-1].startswith("tpad=") and "crop=1280:720" in fs)
        self.assertEqual(wasted.audio_filters((2.0, 1.5, "x", True), 5.0)[0], "apad=whole_dur=6.500")
