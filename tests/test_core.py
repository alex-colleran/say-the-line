"""Core logic tests: no media, Jellyfin, ffmpeg or speech model needed.

  python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SAYTHELINE_CONFIG", os.devnull)       # never pick up a real saytheline.toml

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
        self.assertEqual(media._fpath("C:\\x\\it's.ass"), "C\\:/x/it\\'s.ass")


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

    def loud_cards(self, said):
        from unittest import mock
        from saytheline import kinetic, poster
        words = [dict(w=w, s=i * 300, e=i * 300 + 250, loud=l) for i, (w, l) in enumerate(said)]
        with mock.patch.object(poster, "tiers", lambda ws: [2] * len(ws)):
            return kinetic.layout(words, 1280, 720, dict(size="m", font="Inter Black", position="middle"))

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
