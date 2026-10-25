"""Slow motion: the time map and caption timing (no media needed), and the whole render with a
stand-in for RIFE that repeats frames (needs ffmpeg, not a GPU).

  python -m unittest discover -s tests
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SAYTHELINE_CONFIG", os.devnull)       # never pick up a real saytheline.toml

from saytheline import media, slowmo  # noqa: E402

# rife-ncnn-vulkan -i IN -o OUT -m MODEL -n N -f jpg: N frames, each input frame repeated
FAKE_RIFE = """#!/bin/sh
while [ $# -gt 0 ]; do case "$1" in -i) i=$2;; -o) o=$2;; -n) n=$2;; esac; shift; done
k=$(( n / $(ls "$i" | wc -l) ))
exec ffmpeg -v error -nostdin -y -framerate 1 -i "$i/%06d.png" -vf fps=$k -frames:v "$n" "$o/%08d.jpg"
"""


class TimeMap(unittest.TestCase):
    def test_times_before_inside_and_after_the_slow_part(self):
        # 1 s to 2 s at 4x: before it nothing moves, inside it stretches, after it shifts by 3 s
        self.assertEqual([slowmo.timemap(t, 1000, 2000, 4) for t in (0, 1000, 1500, 2000, 2500)],
                         [0, 1000, 3000, 5000, 5500])

    def test_captions_follow_the_picture(self):
        cues = [dict(start_ms=10500, end_ms=12500, text="No!", words=[dict(w="No!", s=11000, e=11400)])]
        out = slowmo.remap_cues(cues, 10000, 1000, 2000, 2)        # cut from 10 s, 1-2 s of it at 2x
        self.assertEqual((out[0]["start_ms"], out[0]["end_ms"]), (500, 3500))
        self.assertEqual((out[0]["words"][0]["s"], out[0]["words"][0]["e"]), (1000, 1800))
        self.assertEqual(cues[0]["start_ms"], 10500, "the cut's own cues are left alone")

    def test_request_checks(self):
        self.assertIsNone(slowmo.part_of({}, 0, 4000))
        self.assertIsNone(slowmo.part_of({"slow": {"factor": 1}}, 0, 4000))
        self.assertEqual(slowmo.part_of({"slow": {"factor": 4, "a_ms": 1000, "b_ms": 2500}}, 500, 4000),
                         (500, 2000, 4, "deep"))
        # a part reaching past the cut is held to it
        self.assertEqual(slowmo.part_of({"slow": {"factor": 2, "a_ms": 0, "b_ms": 9000, "sound": "mute"}}, 500, 4000)[:2],
                         (0, 3500))
        for bad in ({"factor": 3}, {"factor": 2, "a_ms": 1000, "b_ms": 1100},
                    {"factor": 2, "a_ms": 0, "b_ms": slowmo.MAX_PART_MS + 1000}):
            with self.assertRaises(ValueError, msg=bad):
                slowmo.part_of({"slow": bad}, 0, 60000)


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class Render(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="saytheline-slowtest-")
        rife = os.path.join(cls.tmp, "rife-ncnn-vulkan")
        with open(rife, "w") as f:
            f.write(FAKE_RIFE)
        os.chmod(rife, os.stat(rife).st_mode | stat.S_IEXEC)
        os.makedirs(os.path.join(cls.tmp, "rife-v4.6"))
        cls.env = mock.patch.dict(os.environ, {"SAYTHELINE_RIFE": rife})
        cls.env.start()
        cls.src = os.path.join(cls.tmp, "src.mp4")
        cls.mute = os.path.join(cls.tmp, "mute.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=4",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-shortest", "-pix_fmt", "yuv420p", cls.src],
                       check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", cls.src, "-an", "-c", "copy", cls.mute], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def render(self, src, req, in_ms=500, out_ms=2500, fmt="mp4"):
        out = os.path.join(self.tmp, f"out.{fmt}")
        seen = []
        info = media.render(src, in_ms, out_ms, {"format": fmt, "res": 240}, [], out,
                            slow=slowmo.part_of({"slow": req}, in_ms, out_ms), progress=lambda d, t: seen.append(t))
        return info, media.probe(out), seen

    def test_part_slowed_in_the_middle(self):
        info, got, seen = self.render(self.src, {"factor": 4, "a_ms": 1000, "b_ms": 1500})
        self.assertAlmostEqual(got["duration"], 3.5, delta=0.12)     # 2 s cut + 0.5 s x 3 more
        self.assertAlmostEqual(info["seconds"], 3.5, delta=0.01)
        self.assertTrue(got["has_audio"])
        self.assertEqual(seen[-1], 48, "12 frames of the slow part, 4 each")

    def test_whole_cut_same_pitch(self):
        _, got, _ = self.render(self.src, {"factor": 2, "a_ms": 500, "b_ms": 2500, "sound": "pitch"})
        self.assertAlmostEqual(got["duration"], 4.0, delta=0.12)

    def test_no_audio_and_a_gif(self):
        _, got, _ = self.render(self.mute, {"factor": 2, "a_ms": 2000, "b_ms": 2500})
        self.assertAlmostEqual(got["duration"], 2.5, delta=0.12)
        self.assertFalse(got["has_audio"])
        _, gif, _ = self.render(self.src, {"factor": 2, "a_ms": 500, "b_ms": 1000}, fmt="gif")
        self.assertAlmostEqual(gif["duration"], 2.5, delta=0.15)


if __name__ == "__main__":
    unittest.main()
