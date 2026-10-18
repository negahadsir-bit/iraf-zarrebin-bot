import tempfile
import unittest
from pathlib import Path
from unittest import mock

import finalize
from models import Clip, Sentence
from retry import QuotaError

CLIP = Clip(1, 100.0, 170.0, "قصه‌ی کتاب", text="x", top=True, shaky=True)
W0 = 100.0 - finalize.PAD  # the listening window starts here: heard sentences are window-local
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
        # heard: 0 ببین, 1 شروع, 2 ادامه, 3 اوج, 4 اضافه -> keep 1..3
        out = self.one(Sc([{"from": 1, "to": 3, "ok": True}]))
        self.assertAlmostEqual(out.start, W0 + 2.0 - finalize.LEAD)
        self.assertAlmostEqual(out.end, W0 + 52.0 + finalize.TAIL)
        self.assertEqual(out.text, "شروع مستقل است. ادامه‌ی ماجرا. اوج احساسی همین است.")
        self.assertFalse(out.shaky)
        self.assertTrue(out.top)

    def test_surroundings_and_expected_topic_are_offered(self):
        sc = Sc([{"from": 1, "to": 3, "ok": True}])
        self.one(sc)
        self.assertIn("ببین مثلا", sc.prompts[0])
        self.assertIn("قصه‌ی کتاب", sc.prompts[0])

    def test_not_ok_drops_clip(self):
        self.assertIsNone(self.one(Sc([{"ok": False, "note": "بی‌معنی"}])))

    def test_too_short_result_keeps_clip(self):
        out = self.one(Sc([{"from": 0, "to": 0, "ok": True}]))
        self.assertEqual(out, CLIP)

    def test_bad_indices_keep_clip(self):
        self.assertEqual(self.one(Sc([{"from": 2, "to": 9, "ok": True}])), CLIP)
        self.assertEqual(self.one(Sc([{"from": "x", "to": 1, "ok": True}])), CLIP)
        self.assertEqual(self.one(Sc([])), CLIP)

    def test_almost_no_speech_drops_clip(self):
        self.assertIsNone(self.one(Sc([{"from": 0, "to": 1, "ok": True}]), tc=Tc(HEARD[:2])))

    def test_nothing_heard_keeps_clip_flagged(self):
        out = self.one(Sc([]), tc=Tc([]))
        self.assertTrue(out.shaky)
        self.assertEqual((out.start, out.end), (CLIP.start, CLIP.end))

    def test_unfinished_last_sentence_is_stepped_back(self):
        heard = [Sentence("ببین مثلا.", 0.0, 1.0), Sentence("شروع مستقل است.", 2.0, 12.0),
                 Sentence("ادامه‌ی ماجرا.", 12.0, 30.0), Sentence("اوج احساسی همین است.", 30.0, 52.0),
                 Sentence("گفتند که", 52.0, 60.0)]
        out = self.one(Sc([{"from": 1, "to": 4, "ok": True}]), tc=Tc(heard))
        self.assertEqual(out.text.split()[-1], "است.")
        out = self.one(Sc([{"from": 1, "to": 4, "ok": True}]), tc=Tc(heard[:4] + [Sentence("گفتند که", 52.0, 60.0)]))
        self.assertAlmostEqual(out.end, W0 + 52.0 + finalize.TAIL)

    def test_careful_listening_is_preferred_when_available(self):
        class Careful(Tc):
            def __init__(self):
                super().__init__()
                self.used = False

            def transcribe_careful(self, path):
                self.used = True
                return HEARD

            def transcribe_sentences(self, path):
                raise AssertionError("should use the careful listener")
        tc = Careful()
        self.one(Sc([{"from": 1, "to": 3, "ok": True}]), tc=tc)
        self.assertTrue(tc.used)


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
        answers = iter([[{"ok": False}], [{"from": 1, "to": 3, "ok": True}]])
        sc = Sc()
        sc.generate_json = lambda p: next(answers)
        out = self.run_it([a, b], sc)
        self.assertEqual([c.rank for c in out], [1])
        self.assertAlmostEqual(out[0].start, (300.0 - finalize.PAD) + 2.0 - finalize.LEAD)

    def test_all_dropped_returns_originals(self):
        out = self.run_it([CLIP], Sc([{"ok": False}]))
        self.assertEqual(out, [CLIP])


if __name__ == "__main__":
    unittest.main()
