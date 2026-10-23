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
            self.assertEqual(search.facets(p, "danger"), [{"key": "s:Show", "kind": "episode", "label": "Show", "n": 2}])
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
