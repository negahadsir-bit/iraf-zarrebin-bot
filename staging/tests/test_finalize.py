import tempfile
import unittest
from pathlib import Path
from unittest import mock

import finalize
from models import Clip, Sentence
from retry import QuotaError

CLIP = Clip(1, 100.0, 170.0, "r", text="x", top=True, shaky=True)
# window starts at 98.5: sentences are window-local
HEARD = [Sentence("ببین مثلا.", 0.0, 1.0),                 # outside the clip (before it)
         Sentence("شروع مستقل است.", 2.0, 12.0),
         Sentence("ادامه‌ی ماجرا.", 12.0, 30.0),
         Sentence("اوج احساسی همین است.", 30.0, 52.0),
         Sentence("حرف اضافه و ضعیف.", 52.0, 70.0)]


class Tc:
    def __init__(self, heard=HEARD):
        self.heard = heard

    def transcribe_sentences(self, path):
        return self.heard


class Sc:
    def __init__(self, rows=None, boom=None):
        self.rows, self.boom, self.prompts = rows, boom, []

    def generate_json(self, prompt):
        self.prompts.append(prompt)
        if self.boom:
            raise self.boom
        return self.rows


class FinalizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(finalize, "_cut", side_effect=lambda a, s, l, d: d)
        p.start()
        self.addCleanup(p.stop)
        self.wd = Path(self.tmp.name)

    def one(self, sc, tc=None, clip=CLIP):
        return finalize.finalize_one(clip, Path("a.mp3"), 1000.0, tc or Tc(), sc, self.wd)

    def test_trims_tail_after_the_peak(self):
        # candidates (inside the clip): 0 شروع, 1 ادامه, 2 اوج, 3 اضافه -> keep 0..2
        out = self.one(Sc([{"from": 0, "to": 2, "ok": True}]))
        self.assertAlmostEqual(out.start, 98.5 + 2.0 - finalize.LEAD)
        self.assertAlmostEqual(out.end, 98.5 + 52.0 + finalize.TAIL)
        self.assertEqual(out.text, "شروع مستقل است. ادامه‌ی ماجرا. اوج احساسی همین است.")
        self.assertFalse(out.shaky)
        self.assertTrue(out.top)

    def test_sentence_before_the_clip_is_not_offered(self):
        sc = Sc([{"from": 0, "to": 2, "ok": True}])
        self.one(sc)
        self.assertNotIn("ببین مثلا", sc.prompts[0])

    def test_not_ok_drops_clip(self):
        self.assertIsNone(self.one(Sc([{"ok": False, "note": "بی‌معنی"}])))

    def test_too_short_result_keeps_clip(self):
        out = self.one(Sc([{"from": 0, "to": 0, "ok": True}]))
        self.assertEqual(out, CLIP)

    def test_bad_indices_keep_clip(self):
        self.assertEqual(self.one(Sc([{"from": 2, "to": 9, "ok": True}])), CLIP)
        self.assertEqual(self.one(Sc([{"from": "x", "to": 1, "ok": True}])), CLIP)
        self.assertEqual(self.one(Sc([])), CLIP)

    def test_too_little_speech_keeps_clip(self):
        out = self.one(Sc([{"from": 0, "to": 1, "ok": True}]), tc=Tc(HEARD[:2]))
        self.assertEqual(out, CLIP)


class FinalizeClipsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(finalize, "_cut", side_effect=lambda a, s, l, d: d)
        p.start()
        self.addCleanup(p.stop)
        self.wd = Path(self.tmp.name)

    def run_it(self, clips, sc):
        return finalize.finalize_clips(clips, Path("a.mp3"), 1000.0, Tc(), sc, self.wd, workers=1)

    def test_failure_keeps_clip(self):
        out = self.run_it([CLIP], Sc(boom=RuntimeError("x")))
        self.assertEqual(out, [CLIP])

    def test_quota_keeps_clip(self):
        self.assertEqual(self.run_it([CLIP], Sc(boom=QuotaError("q"))), [CLIP])

    def test_drops_and_renumbers(self):
        a = Clip(1, 100.0, 170.0, "r", text="x")
        b = Clip(2, 300.0, 370.0, "r", text="y")
        answers = iter([[{"ok": False}], [{"from": 0, "to": 2, "ok": True}]])
        sc = Sc()
        sc.generate_json = lambda p: next(answers)
        out = self.run_it([a, b], sc)
        self.assertEqual([c.rank for c in out], [1])
        self.assertAlmostEqual(out[0].start, 298.5 + 2.0 - finalize.LEAD)

    def test_all_dropped_returns_originals(self):
        out = self.run_it([CLIP], Sc([{"ok": False}]))
        self.assertEqual(out, [CLIP])


if __name__ == "__main__":
    unittest.main()
