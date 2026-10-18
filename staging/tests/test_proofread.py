import tempfile
import unittest
from pathlib import Path
from unittest import mock

import proofread
from models import Clip, Sentence
from persian import normalize_fa
from studio import hear_again


class NormalizeTests(unittest.TestCase):
    def test_arabic_letters_and_halfspaces(self):
        self.assertEqual(normalize_fa("مي خواهم كتاب ها"), "می‌خواهم کتاب‌ها")

    def test_punctuation(self):
        self.assertEqual(normalize_fa("چرا ? بله ,خوب ... است ."), "چرا؟ بله، خوب… است.")

    def test_numbers_and_words_untouched(self):
        self.assertEqual(normalize_fa("۳,۵۰۰ تومان و ها"), "۳,۵۰۰ تومان و ها")


class AcceptTests(unittest.TestCase):
    def test_small_fix_accepted(self):
        self.assertTrue(proofread.accept("من به خوفی رفتم و دیدم", "من به خوشی رفتم و دیدم"))

    def test_rewrite_rejected(self):
        self.assertFalse(proofread.accept("من به خانه رفتم و دیدم", "کاملاً متن دیگری است که ربطی ندارد اصلاً"))

    def test_shortened_rejected(self):
        self.assertFalse(proofread.accept("یک دو سه چهار پنج شش هفت هشت", "یک دو"))


class FakeClient:
    def __init__(self, reply=None, boom=False):
        self.reply, self.boom, self.calls = reply, boom, []

    def correct_text(self, audio, draft):
        self.calls.append(draft)
        if self.boom:
            raise RuntimeError("x")
        return self.reply


class ProofreadClipsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(proofread, "_cut", side_effect=lambda a, s, l, d: d)
        p.start()
        self.addCleanup(p.stop)

    def run_it(self, client, text="من به خوفی رفتم و دیدم كه"):
        clip = Clip(1, 10, 40, "r", text=text)
        return proofread.proofread_clips([clip], [], Path("a.mp3"), 100, client, Path(self.tmp.name))[0]

    def test_audio_correction_applied(self):
        c = self.run_it(FakeClient("من به خوشی رفتم و دیدم که"))
        self.assertEqual(c.text, "من به خوشی رفتم و دیدم که")

    def test_wild_rewrite_keeps_cleaned_draft(self):
        c = self.run_it(FakeClient("چیز دیگری"))
        self.assertEqual(c.text, "من به خوفی رفتم و دیدم که")

    def test_failure_keeps_cleaned_draft(self):
        c = self.run_it(FakeClient(boom=True))
        self.assertEqual(c.text, "من به خوفی رفتم و دیدم که")

    def test_client_without_correct_text(self):
        c = self.run_it(object())
        self.assertEqual(c.text, "من به خوفی رفتم و دیدم که")

    def test_empty_text_falls_back_to_sentences(self):
        clip = Clip(1, 10, 40, "r")
        sents = [Sentence("جمله ی اول.", 10, 20), Sentence("جمله دوم.", 20, 30)]
        out = proofread.proofread_clips([clip], sents, Path("a.mp3"), 100, None, Path(self.tmp.name))[0]
        self.assertEqual(out.text, "جمله ی اول. جمله دوم.")


class HearAgainTests(unittest.TestCase):
    def test_corrects_caption_from_audio(self):
        c = FakeClient("من به خوشی رفتم و دیدم")
        self.assertEqual(hear_again(c, Path("a.mp3"), "من به خوفی رفتم و دیدم"), "من به خوشی رفتم و دیدم")

    def test_failure_returns_cleaned_text(self):
        self.assertEqual(hear_again(FakeClient(boom=True), Path("a.mp3"), "مي خواهم"), "می‌خواهم")


if __name__ == "__main__":
    unittest.main()
