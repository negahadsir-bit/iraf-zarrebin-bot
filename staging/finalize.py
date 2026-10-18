"""Final editor: listen to every clip once more, keep only its best self-contained stretch
(clean start, strongest ending, nothing after the peak) and drop clips that make no sense."""
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from models import Clip
from refine import _cut
from select_clips import _done
from retry import BusyError, QuotaError

PAD = 10.0  # absorbs small timing drift of the first transcript
LEAD, TAIL = 0.15, 0.35
MIN_LEN, MAX_LEN = 20.0, 70.0
MIN_SENTENCES = 3


def _prompt(sents, reason: str = "") -> str:
    lines = [f"[{i}] ({round(s.end - s.start)}ث) {s.text}" for i, s in enumerate(sents)]
    return (
        "تو ویراستار نهایی یک برش ویدیویی کوتاه از مصاحبه‌ی فارسی افغانستانی (دری) هستی. متن زیر از روی "
        "صدا نوشته شده و ممکن است کلمه‌های غلط یا بی‌معنی داشته باشد؛ معنا را با لهجه‌ی دری بفهم. "
        + (f"موضوع مورد انتظار این برش: «{reason}». بازه را از همان بخش انتخاب کن؛ جمله‌های اطراف "
           "فقط برای پیدا کردن مرز درست‌اند.\n" if reason else "") +
        "یک بازه‌ی پیوسته از جمله‌ها انتخاب کن که به‌تنهایی یک ویدیوی کامل و تأثیرگذار باشد:\n"
        "- شروع باید فکر یا روایت را از اول و مستقل باز کند؛ نه ادامه‌ی جمله‌ی قبل، نه وابسته به «این/آن» "
        "بدون مرجع، نه «ببین/خب/مثلاً» بی‌محتوا، نه وسط یک داستان بدون مقدمه.\n"
        "- پایان باید قوی‌ترین جمله‌ی همان فکر باشد (نتیجه، پانچ یا اوج احساسی). هر چه بعد از اوج می‌آید "
        "و ضعیف‌تر یا تکراری یا اضافه است حذف شود؛ کوتاه‌تر و تأثیرگذارتر بهتر از طولانی است.\n"
        "- آخرین جمله‌ی بازه باید کامل باشد؛ جمله‌ی نیمه‌تمام مثل «گفتند که» یا «و بعد» را پایان قرار نده.\n"
        "- مدت بازه ترجیحاً ۳۰ تا ۶۰ ثانیه، هرگز کمتر از ۲۰ و حداکثر ۷۰ ثانیه باشد.\n"
        "- جمله‌ی نامفهوم، خراب یا بی‌معنی (مثلاً ترکیب کلمه‌های بی‌ربط) داخل بازه نباشد.\n"
        "- اگر هیچ بازه‌ی معنادار، مستقل و کامل در این متن نیست، ok را false بده.\n"
        'فقط JSON برگردان: [{"from": شماره‌ی جمله, "to": شماره‌ی جمله, "ok": true یا false, '
        '"note": "یک جمله دلیل"}]\n\nجمله‌ها:\n' + "\n".join(lines))


def finalize_one(clip: Clip, audio: Path, total: float, tclient, sclient, workdir: Path):
    """The clip trimmed to its best stretch, the clip itself when nothing can be judged,
    or None when the judge finds no meaningful, self-contained stretch in it."""
    w0, w1 = max(0.0, clip.start - PAD), min(total, clip.end + PAD)
    dest = workdir / f"fin_{clip.rank}.mp3"
    listen = getattr(tclient, "transcribe_careful", None) or tclient.transcribe_sentences
    heard = listen(_cut(audio, w0, w1 - w0, dest)) or []
    if not heard:  # nothing heard (a failed listen or no speech here): keep the clip, flagged
        return replace(clip, shaky=True)
    sents = list(heard)
    if len(sents) < MIN_SENTENCES:  # a stretch of music or silence, not an interview answer
        print(f"finalize clip {clip.rank}: only {len(sents)} spoken sentence(s), dropped",
              file=sys.stderr, flush=True)
        return None
    rows = sclient.generate_json(_prompt(sents, clip.reason))
    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    if row is None:
        return clip
    if row.get("ok") is False:
        print(f"finalize clip {clip.rank}: no meaningful stretch ({str(row.get('note', ''))[:80]}), "
              f"dropped", file=sys.stderr, flush=True)
        return None
    try:
        a, b = int(row["from"]), int(row["to"])
    except (KeyError, TypeError, ValueError):
        return clip
    if not (0 <= a <= b < len(sents)):
        return clip
    if sum(_done(s) for s in sents) * 2 >= len(sents):  # punctuated transcript: end on a finished sentence
        while b > a and not _done(sents[b]):
            b -= 1
    start = max(0.0, w0 + sents[a].start - LEAD)
    end = min(total, w0 + sents[b].end + TAIL)
    if not MIN_LEN <= end - start <= MAX_LEN:
        return clip
    print(f"finalize clip {clip.rank}: {clip.start:.1f}-{clip.end:.1f} -> {start:.1f}-{end:.1f} "
          f"(sentences {a}-{b} of {len(sents)})", file=sys.stderr, flush=True)
    return replace(clip, start=start, end=end, shaky=False,
                   text=" ".join(s.text for s in sents[a:b + 1]))


def finalize_clips(clips: list[Clip], audio: Path, total: float, tclient, sclient,
                   workdir: Path, workers: int = 3) -> list[Clip]:
    def work(c: Clip):
        try:
            return finalize_one(c, audio, total, tclient, sclient, workdir)
        except (QuotaError, BusyError):
            return c
        except Exception as e:  # noqa: BLE001 - a failed judgement keeps the clip as it was
            print(f"finalize clip {c.rank} skipped: {type(e).__name__}", file=sys.stderr, flush=True)
            return c

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        out = [f.result() for f in [pool.submit(work, c) for c in clips]]
    kept = [c for c in out if c is not None]
    if not kept:  # never answer with nothing because of the final editor
        return clips
    return [replace(c, rank=i) for i, c in enumerate(kept, 1)]
