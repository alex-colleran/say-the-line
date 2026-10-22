"""Camera-cut tests. The detector and cache tests use made-up frames; the last two need ffmpeg
(skipped without it) and check real frame times and a frame-exact render on a generated video.

  python -m unittest discover -s tests
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SAYTHELINE_CONFIG", os.devnull)
os.environ["SAYTHELINE_VAAPI"] = ""                          # CPU decode: the same everywhere

from saytheline import media, shots  # noqa: E402

N = shots.W * shots.H
F = 1000 / 24


def picture(seed, shift=0):
    """A textured 32x18 frame; `shift` moves it sideways (a pan or a shake)."""
    rnd = random.Random(seed)
    base = [rnd.randrange(30, 220) for _ in range((shots.W + 8) * shots.H)]
    return bytes(base[y * (shots.W + 8) + x + 4 - shift] for y in range(shots.H) for x in range(shots.W))


def clip(*parts):
    """[(ms, frame)] at 24 fps from (frame, count) parts."""
    frames = []
    for f, n in parts:
        frames += [f] * n
    return [(1000 + i * F, f) for i, f in enumerate(frames)]


class Detect(unittest.TestCase):
    def test_hard_cut_boundary_is_half_a_frame_early(self):
        frames = clip((picture(1), 20), (picture(2), 20))
        f, anchor, cuts = shots.find_cuts(frames)
        self.assertAlmostEqual(f, F, places=6)
        self.assertEqual(anchor, 1000)
        self.assertEqual([ms for ms, _ in cuts], [round(1000 + 20 * F - F / 2)])

    def test_dark_cut_counts(self):
        dark = lambda s: bytes(p // 8 for p in picture(s))       # noqa: E731  (a scene lit very dark)
        self.assertEqual(len(shots.find_cuts(clip((dark(1), 15), (dark(2), 15)))[2]), 1)

    def test_local_change_is_not_a_cut(self):
        a = bytearray(picture(1))
        b = bytearray(a)
        for y in range(10, 16):                                 # a mouth opens: 1/8 of the frame changes
            for x in range(12, 20):
                b[y * shots.W + x] = 255 - b[y * shots.W + x]
        self.assertEqual(shots.find_cuts(clip((bytes(a), 10), (bytes(b), 3), (bytes(a), 10)))[2], [])

    def test_shake_and_pan_are_not_cuts(self):
        shake = clip((picture(1), 10), (picture(1, 2), 1), (picture(1), 10))
        self.assertEqual(shots.find_cuts(shake)[2], [])
        pan = [(1000 + i * F, picture(1, i % 5 - 2)) for i in range(30)]
        self.assertEqual(shots.find_cuts(pan)[2], [])

    def test_flash_is_not_a_cut(self):
        white = bytes([250] * N)
        self.assertEqual(shots.find_cuts(clip((picture(1), 10), (white, 2), (picture(1), 10)))[2], [])


class Fit(unittest.TestCase):
    def test_trims_padding_only(self):
        # line 1000-2000, padding 1500 / 1000, cuts in both paddings and one inside the line
        self.assertEqual(shots.fit(-500, 3000, 1000, 2000, [-1000, 200, 600, 1500, 2400, 2800]), (600, 2400))
        self.assertEqual(shots.fit(-500, 3000, 1000, 2000, [1500]), (-500, 3000))

    def test_a_cut_just_inside_the_words_counts(self):
        self.assertEqual(shots.fit(0, 3000, 1000, 2000, [1000 + shots.SPEECH_TOL_MS, 2000 - shots.SPEECH_TOL_MS]),
                         (1000 + shots.SPEECH_TOL_MS, 2000 - shots.SPEECH_TOL_MS))
        self.assertEqual(shots.fit(0, 3000, 1000, 2000, [1000 + shots.SPEECH_TOL_MS + 1]), (0, 3000))

    def test_margin_moves_each_end_inward(self):
        self.assertEqual(shots.fit(0, 3000, 1000, 2000, [500, 2500], margin=40), (540, 2460))

    def test_never_leaves_a_sliver(self):
        self.assertEqual(shots.fit(0, 1000, 450, 550, [560, 440]), (0, 1000))

    def test_whole_shot(self):
        self.assertEqual(shots.whole_shot(1000, 2000, [-3000, 400, 1500, 2600, 5000]), (400, 2600))
        self.assertEqual(shots.whole_shot(1000, 2000, [2600]), (None, 2600))


class CacheScans(unittest.TestCase):
    def test_scans_each_stretch_once(self):
        calls = []

        def fake_decode(src, a, b):
            calls.append((a, b))
            out, t = [], a
            while t < b:
                out.append((t, picture(1 if t < 5000 else 2)))
                t += F
            return out

        real = shots.decode
        shots.decode = fake_decode
        try:
            with tempfile.TemporaryDirectory() as d:
                src = os.path.join(d, "v.mkv")
                open(src, "wb").close()
                c = shots.Cache(os.path.join(d, "shots"))
                shots.loose_timing = lambda s: False            # no ffprobe for the fake file
                r1 = c.cuts(7, src, 3000, 6000)
                r2 = c.cuts(7, src, 4000, 5500)
                r3 = c.cuts(7, src, 2000, 8000)
            self.assertEqual(len(r1["cuts"]), 1)
            self.assertEqual(r1["cuts"], r2["cuts"])
            self.assertEqual(len(calls), 2)                      # the second request was all cached
            self.assertEqual(calls[1], (2000 - shots.LEAD_MS, 8000 + shots.TAIL_MS))
            self.assertEqual([x["ms"] for x in r3["cuts"]], [x["ms"] for x in r1["cuts"]])
            self.assertEqual(r3["margin_ms"], 0)
        finally:
            shots.decode = real

    def test_one_frame_apart_is_one_cut(self):
        self.assertEqual(list(shots._dedupe({"1000": 0.5, "1033": 0.6, "3000": 0.4}, 33.4)), ["1000", "3000"])

    def test_gaps(self):
        self.assertEqual(shots._gaps([[0, 100, 0, 40], [200, 300, 0, 40]], 50, 400), [(100, 200), (300, 400)])
        self.assertEqual(shots._gaps([[0, 500, 0, 40]], 50, 400), [])


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class RealVideo(unittest.TestCase):
    """Colour bars cut to a test pattern at 2.5025 s (frame 60 at 23.976 fps), B-frames on.
    (Plain colours won't do: every frame is normalised to its own contrast, so a flat frame is blank.)"""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.src = os.path.join(cls.dir, "cut.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "smptebars=s=320x180:r=24000/1001:d=2.5025",
                        "-f", "lavfi", "-i", "rgbtestsrc=s=320x180:r=24000/1001:d=2.5", "-filter_complex",
                        "[0]format=yuv420p[a];[1]format=yuv420p[b];[a][b]concat=n=2:v=1", "-c:v", "libx264", "-preset", "veryfast",
                        "-bf", "3", "-g", "48", cls.src], check=True)

    def frame(self, path, last=False):
        """The first (or last) frame of a video as 16x9 grey."""
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", "scale=16:9,format=gray", "-f", "rawvideo", "-"],
                           capture_output=True, check=True).stdout
        return r[-144:] if last else r[:144]

    def nearer(self, f, a, b):
        d = lambda x, y: sum(abs(p - q) for p, q in zip(x, y))   # noqa: E731
        return "A" if d(f, a) < d(f, b) else "B"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_cut_found_wherever_the_scan_starts(self):
        for start in (0, 900, 1700):
            f, _, cuts = shots.find_cuts(shots.decode(self.src, start, 4500))
            self.assertEqual([ms for ms, _ in cuts], [round(60 * 1001 / 24 - f / 2)], f"scan from {start}")

    def test_render_ends_on_the_last_frame_before_the_cut(self):
        _, _, cuts = shots.find_cuts(shots.decode(self.src, 0, 4500))
        cut = cuts[0][0]
        a, b = self.frame(self.src), self.frame(self.src, last=True)
        for fmt in ("mp4", "gif"):
            out = os.path.join(self.dir, "out." + fmt)
            for lead in range(1000, 1042, 7):       # starts at every phase between two frames
                media.render(self.src, cut - lead, cut, {"format": fmt, "res": 180}, [], out)
                self.assertEqual(self.nearer(self.frame(out, last=True), a, b), "A", f"{fmt} from -{lead} ms: the next shot leaked in")
            media.render(self.src, cut, cut + 1000, {"format": fmt, "res": 180}, [], out)
            self.assertEqual(self.nearer(self.frame(out), a, b), "B", f"{fmt}: opens on the previous shot")


if __name__ == "__main__":
    unittest.main()
