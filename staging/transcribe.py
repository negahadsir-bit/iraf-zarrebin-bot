import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from models import Sentence, Word
from retry import BusyError, QuotaError, call_with_fallback, models_from_env
from timeline import fix_word_times, parse_seconds, retime, words_to_sentences


class TranscriptionError(Exception):
    pass


def _split(audio: Path, workdir: Path, chunk_seconds: int) -> list[Path]:
    out = workdir / "chunks"
    out.mkdir(parents=True, exist_ok=True)
    pattern = out / "c%03d.mp3"
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(audio), "-f", "segment",
         "-segment_time", str(chunk_seconds), "-c", "copy", "-reset_timestamps", "1",
         str(pattern)], capture_output=True)
    if r.returncode != 0:
        raise TranscriptionError("ffmpeg split failed")
    return sorted(out.glob("c*.mp3"))


def audio_seconds(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
         "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        raise TranscriptionError("ffprobe failed") from None


def _one(client, chunk: Path) -> list[Sentence]:
    try:
        words = client.transcribe_words(chunk)
        if words:
            return words_to_sentences(fix_word_times(words))
        return client.transcribe_sentences(chunk) or []
    except BusyError:
        raise
    except Exception as e:  # noqa: BLE001
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            raise QuotaError(str(e)) from e
        raise


def transcribe(audio: Path, client, workdir: Path = None, chunk_seconds: int = 300,
               progress=None, on_skip=None, sleep=time.sleep,
               retry_wait: float = 30.0, workers: int = 3) -> list[Sentence]:
    """Transcribe in chunks. A chunk that stays busy is retried once at the end
    and then skipped (reported via on_skip) so one bad chunk never loses the rest."""
    workdir = Path(workdir or tempfile.mkdtemp())
    chunks = _split(audio, workdir, chunk_seconds)
    done: dict[int, list[Sentence]] = {}
    busy: list[int] = []
    last_busy = None
    finished = 0
    lock = threading.Lock()

    def finish():
        nonlocal finished
        with lock:
            finished += 1
            n = finished
        if progress:
            progress(n, len(chunks))

    def work(i):
        try:
            done[i] = _one(client, chunks[i])
            finish()
        except QuotaError:
            raise
        except Exception as e:  # noqa: BLE001 - one bad chunk must not lose the rest
            print(f"chunk {i + 1} failed: {type(e).__name__}: {str(e)[:200]}",
                  file=sys.stderr, flush=True)
            with lock:
                busy.append(i)
                nonlocal last_busy
                last_busy = e if isinstance(e, BusyError) else BusyError(str(e))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for f in [pool.submit(work, i) for i in range(len(chunks))]:
            f.result()  # re-raises QuotaError and other hard failures
    busy.sort()
    if busy:
        sleep(retry_wait)
    for i in busy:
        try:
            done[i] = _one(client, chunks[i])
        except QuotaError:
            raise
        except Exception as e:  # noqa: BLE001
            last_busy = e if isinstance(e, BusyError) else BusyError(str(e))
            if on_skip:
                on_skip(i + 1, len(chunks))
        finish()
    if busy and not any(done.values()):
        raise last_busy
    result = [Sentence(s.text, s.start + i * chunk_seconds, s.end + i * chunk_seconds)
              for i in sorted(done) for s in done[i]]
    if not result:
        raise TranscriptionError("empty transcript")
    return result


_DARI_NOTE = (
    "گویندگان معمولاً افغانستانی‌اند و به فارسی دری (لهجه‌ی کابلی، هزارگی، مزاری، هراتی) صحبت می‌کنند. "
    "گویش را درست بفهم (مثلاً «استم/استی» یعنی «هستم/هستی»، «خو»، «بلی»، «کدام»، «مه»، «اینجه/آنجه»، "
    "«چی‌رو») و متن را به فارسی معیار و روان بنویس؛ لازم نیست املای دری را حفظ کنی، فقط معنا باید "
    "دقیقاً همان چیزی باشد که گفته شده. واژه را حدسی و بی‌ربط عوض نکن؛ اگر کلمه‌ای را مطمئن نشنیدی، "
    "نزدیک‌ترین کلمه‌ی معنادار با توجه به جمله را بنویس. نام‌های افغانستانی و ایرانی (شهرها، قوم‌ها، "
    "افراد) را درست بنویس. هیچ بخش گفتاری را جا نینداز و حرف‌های پرکننده‌ی بی‌معنا («اِ…»، «ام…») را ننویس. ")

_PROMPT_WORDS = (
    "این فایل صوتی فارسی را کلمه به کلمه بنویس. " + _DARI_NOTE + "فقط JSON برگردان: "
    '[{"text": "...", "start": ثانیه, "end": ثانیه}, ...] با زمان واقعی هر کلمه از ابتدای فایل. '
    "علائم نقطه، ؟ و ! را در پایان جمله‌ها بگذار.")
_PROMPT_SENT = (
    "این فایل صوتی گفتگوی فارسی است. " + _DARI_NOTE +
    "آن را جمله به جمله بنویس: هر جمله باید یک فکر کامل باشد که در گفتار واقعاً تمام شده "
    "(با نقطه، علامت سؤال یا تعجب پایان یابد)، نه وسط حرف و نه با ادامه‌ی جمله‌ی بعد؛ جمله‌ی "
    "طولانی را در مکث یا مرز طبیعی فکر به چند جمله بشکن (حداکثر حدود ۱۵ کلمه) اما هرگز یک فکر "
    "را وسط یک عبارت قطع نکن. هر جمله را فقط یک بار و با ترتیب شنیده‌شده بنویس. فقط JSON برگردان: "
    '[{"text": "...", "start": ثانیه, "end": ثانیه}, ...] با زمان دقیق شروع و پایان هر جمله از ابتدای فایل، '
    "به‌صورت عدد ثانیه‌ی اعشاری (مثلاً 83.5) و نه دقیقه:ثانیه.")


_PROMPT_ALIGN = (
    "این فایل صوتی فارسی (دری/افغانستانی) یک برش کوتاه است. متن زیر همان چیزی است که در آن گفته می‌شود "
    "(ممکن است چند غلط داشته باشد):\n«%s»\n"
    "برای هر کلمه‌ی این متن، به ترتیب، زمان دقیق شروع و پایان گفتن آن کلمه را از ابتدای فایل بده، "
    "به‌صورت عدد ثانیه‌ی اعشاری (مثلاً 3.42). فقط JSON برگردان: "
    '[{"text": "کلمه از متن", "heard": "آنچه واقعاً شنیده می‌شود", "start": ثانیه, "end": ثانیه}, ...]. '
    "هیچ کلمه‌ای را جا نینداز و کلمه‌ی اضافه نیاور. زمان‌ها باید دقیقاً با صدا هماهنگ باشند و "
    "در مکث‌ها و سکوت‌ها هیچ کلمه‌ای قرار نگیرد.")
_PROMPT_FREE_WORDS = (
    "این فایل صوتی فارسی را کلمه به کلمه بنویس. " + _DARI_NOTE + "فقط JSON برگردان: "
    '[{"text": "...", "heard": "...", "start": ثانیه, "end": ثانیه}, ...] با زمان دقیق هر کلمه از '
    "ابتدای فایل به‌صورت عدد ثانیه‌ی اعشاری. در مکث‌ها کلمه‌ای قرار نده.")


_PROMPT_CORRECT = (
    "این فایل صوتی فارسی (گاهی با لهجه‌ی دری/افغانستانی) یک برش کوتاه است. متن زیر پیش‌نویسی است که از روی "
    "همین صدا نوشته شده و چند کلمه‌اش احتمالاً غلط شنیده یا غلط املایی است:\n«%s»\n"
    "با گوش دادن دقیق به صدا، متن را اصلاح کن تا دقیقاً همان چیزی باشد که گفته می‌شود. " + _DARI_NOTE +
    "املای فارسی معیار را رعایت کن (ی و ک فارسی، نیم‌فاصله در «می‌خواهم»، «کتاب‌ها»، «خانه‌ام»)، نام‌ها و "
    "اصطلاحات را درست بنویس و نقطه، ویرگول «،» و علامت سؤال «؟» را درست بگذار. ساختار و ترتیب جمله‌ها را "
    "عوض نکن و چیزی از خودت اضافه یا حذف نکن. اگر عبارتی در پیش‌نویس بی‌معنی یا ترکیب کلمه‌های بی‌ربط است، "
    "یعنی غلط شنیده شده: همان قسمت را دوباره از روی صدا بنویس تا معنادار شود. فقط اگر واقعاً نمی‌شنوی، "
    "کلمه‌ی پیش‌نویس را نگه دار. "
    'فقط JSON برگردان: [{"text": "متن اصلاح‌شده‌ی کامل"}]')


class GeminiClient:
    """Real client; google-genai imported lazily (not unit-tested)."""

    def __init__(self, api_key: str, model: str = None):
        from google import genai
        self._genai = genai
        from google.genai import types
        self._client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=180_000))
        self._models = [model] if model else models_from_env()
        self._careful_models = None

    def _careful(self) -> list[str]:
        """Strongest available models first (for short, final listening), then the usual ones."""
        if self._careful_models is None:
            import re
            found = []
            try:
                for m in self._client.models.list():
                    name = (getattr(m, "name", "") or "").split("/")[-1]
                    acts = getattr(m, "supported_actions", None) or []
                    bad = ("image", "tts", "live", "audio", "embedding", "vision", "robotics", "computer",
                           "research", "agent", "customtools", "-exp")
                    if (name.startswith("gemini-") and "-pro" in name and "generateContent" in acts
                            and not any(b in name for b in bad)):
                        found.append(name)
            except Exception as e:  # noqa: BLE001 - fall back to the usual models
                print(f"model list failed: {type(e).__name__}", file=sys.stderr, flush=True)

            def version(n):
                nums = re.findall(r"\d+(?:\.\d+)?", n)
                return float(nums[0]) if nums else 0.0
            found.sort(key=version, reverse=True)
            top = found[:2]
            self._careful_models = top + [m for m in self._models if m not in top]
            print(f"careful models: {self._careful_models}", file=sys.stderr, flush=True)
        return self._careful_models

    def _ask(self, audio: Path, prompt: str, careful: bool = False) -> list[dict]:
        f = self._client.files.upload(file=str(audio))

        def call(models, attempts):
            return call_with_fallback(
                lambda m: self._client.models.generate_content(
                    model=m, contents=[prompt, f],
                    config={"response_mime_type": "application/json"}),
                models, attempts=attempts)
        if careful:
            try:
                resp = call(self._careful(), 2)
            except QuotaError:
                raise
            except Exception as e:  # noqa: BLE001 - a strong model that fails is never fatal
                print(f"careful listening failed ({type(e).__name__}); using the usual models",
                      file=sys.stderr, flush=True)
                resp = call(self._models, 5)
        else:
            resp = call(self._models, 5)
        try:
            data = json.loads(resp.text)
        except (ValueError, TypeError):
            return []
        return [d for d in data if isinstance(d, dict) and "text" in d] if isinstance(data, list) else []

    def correct_text(self, audio: Path, draft: str) -> str:
        """The draft text of a short clip, corrected by listening to it again."""
        rows = self._ask(audio, _PROMPT_CORRECT.replace("%s", draft), careful=True)
        return str(rows[0].get("text", "")) if rows else ""

    def align_words(self, audio: Path, text: str = "") -> list[dict]:
        """Per-word times of a short clip; with `text`, one row per word of that text."""
        prompt = _PROMPT_ALIGN.replace("%s", text) if text else _PROMPT_FREE_WORDS
        rows = self._ask(audio, prompt) or self._ask(audio, prompt)  # one retry on an empty answer
        out = []
        for r in rows:
            try:
                out.append({"text": str(r["text"]), "heard": str(r.get("heard", r["text"])),
                            "start": parse_seconds(r["start"]), "end": parse_seconds(r["end"])})
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def transcribe_words(self, audio: Path):
        # Word-level output is too long for the API (connections drop), so the
        # real client uses sentence-level times only; see transcribe_sentences.
        return None

    def transcribe_careful(self, audio: Path):
        """Sentences of a short clip, written by the strongest available model."""
        return self.transcribe_sentences(audio, careful=True)

    def transcribe_sentences(self, audio: Path, careful: bool = False):
        rows = self._ask(audio, _PROMPT_SENT, careful=careful)
        out = []
        for r in rows:
            try:
                out.append(Sentence(str(r["text"]), parse_seconds(r["start"]), parse_seconds(r["end"])))
            except (KeyError, TypeError, ValueError):
                continue
        return retime(out, audio_seconds(audio))
