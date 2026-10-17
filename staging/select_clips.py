import json
import sys
from dataclasses import replace
from categories import focus_note
from models import Clip, Sentence
from timeline import _TERMINATORS
from retry import BusyError, QuotaError, call_with_fallback, models_from_env

MAX_CLIPS = 12
TOP_MAX = 3
EXTRA = 4  # ask for spare candidates: some get dropped (overlap, length)
WORDS_PER_SEC = 2.6
MIN_DUR, TARGET_MIN, MAX_DUR = 20.0, 35.0, 75.0


class SelectionError(Exception):
    pass


def _task(want: int, strict: bool = False) -> str:
    fill = ("اگر برش قوی کمتر از این تعداد است همان‌ها را بده و برای پر کردن تعداد برش متوسط یا بی‌ربط اضافه نکن؛ "
            if strict else f"تا جای ممکن در همان {want} مورد پر کن؛ ")
    return (
        f"تو دستیار انتخاب کلیپ از مصاحبه‌های بلند هستی. بر اساس قوانین بالا، حداکثر {want} بازه‌ی "
        "مناسب برای کلیپ حدود یک‌دقیقه‌ای پیشنهاد بده، از بهترین به ضعیف‌تر، پخش‌شده در کل مصاحبه "
        "(نه فقط ابتدا)؛ " + fill + "اگر چیز خوبی نیست "
        "فهرست خالی برگردان. هر جمله با شماره‌ی [n] آمده. فقط JSON برگردان: "
        '[{"rank": 1, "from": شماره‌ی جمله‌ی شروع, "to": شماره‌ی جمله‌ی پایان, "reason": "دلیل کوتاه به فارسی", "top": false}]. '
        "from و to شماره‌ی جمله‌اند (نه زمان) و to ≥ from. بین همه‌ی برش‌ها حداکثر ۳ مورد را که "
        "به نظرت وایرال‌ترین و قوی‌ترین‌اند (اولویت انتشار) با \"top\": true علامت بزن و بقیه false. "
        "top نبودن یعنی فقط اولویت پایین‌تر؛ آن برش‌ها را از فهرست حذف نکن. "
        "توجه: وسط صوت ممکن است ویدیوی آرشیوی، موسیقی یا صدای غیرگفتاری پخش شود؛ در فهرست با "
        "«⏸ …ث بدون گفتار» مشخص شده. از آن‌ها برای حدس زدن ساختار استفاده کن و سر و ته هر برش "
        "را درست بچین: هر برش باید با یک جمله‌ی کامل و مستقل شروع شود (نه ادامه‌ی جمله‌ی قبل و "
        "نه با «و/که/ولی» ناقص) و با یک جمله‌ی تمام‌شده پایان یابد، نه وسط حرف. "
        "کنار هر جمله مدتش آمده، مثل (۸ث). مدت هر برش، یعنی از شروع جمله‌ی from تا پایان جمله‌ی to، "
        "باید بین ۳۵ و ۷۰ ثانیه باشد و هرگز از ۷۵ ثانیه بیشتر نشود؛ پایان برش را همان‌جا که "
        "فکر کامل می‌شود بگذار، نه آنجا که از حد می‌گذرد.")


def _dur(sentences: list[Sentence], a: int, b: int) -> float:
    """Timestamps are approximate, so never trust them below what the words need."""
    words = sum(len(s.text.split()) for s in sentences[a:b + 1])
    return max(sentences[b].end - sentences[a].start, words / WORDS_PER_SEC)


GAP_MARK = 8.0  # seconds without speech worth telling the model about


def _numbered(sentences: list[Sentence]) -> str:
    lines = []
    for i, s in enumerate(sentences):
        sec = int(s.start)
        gap = s.start - sentences[i - 1].end if i else 0.0
        mark = f"⏸{round(gap)}ث بدون گفتار " if gap >= GAP_MARK else ""
        lines.append(f"[{i}] {sec // 60}:{sec % 60:02d} ({round(s.end - s.start)}ث) {mark}{s.text}")
    return "\n".join(lines)


def _done(s: Sentence) -> bool:
    return s.text.rstrip().endswith(_TERMINATORS)


def _punctuated(sentences: list[Sentence]) -> bool:
    """Edge snapping only makes sense when the transcript really has sentence endings."""
    return bool(sentences) and sum(_done(s) for s in sentences) * 2 >= len(sentences)


_CONNECTORS = {"و", "که", "ولی", "اما", "پس", "یعنی", "چون", "بنابراین", "همچنین", "لذا"}


def _dangling(s: Sentence) -> bool:
    first = s.text.split()[0].strip("،,.") if s.text.split() else ""
    return first in _CONNECTORS


def _snap_start(sentences: list[Sentence], a: int, b: int) -> int:
    """Begin at the start of a sentence: back up over an unfinished one, or skip a leading
    sentence that only continues the previous thought (while enough clip remains)."""
    for _ in range(2):
        if a > 0 and not _done(sentences[a - 1]):
            a -= 1
    for _ in range(2):
        if a < b and _dangling(sentences[a]) and _dur(sentences, a + 1, b) >= TARGET_MIN:
            a += 1
    return a


def _end_score(sentences: list[Sentence], b: int) -> float:
    gap = sentences[b + 1].start - sentences[b].end if b + 1 < len(sentences) else 0.0
    return (1.0 if _done(sentences[b]) else 0.0) + min(max(gap, 0.0), 1.5) / 1.5 * 0.5


def _fit_end(sentences: list[Sentence], a: int, b: int, punctuated: bool = True) -> int:
    """Choose where the clip ends so its length lands in [TARGET_MIN, MAX_DUR], preferring a
    finished sentence followed by a pause over simply the last one that fits."""
    n = len(sentences)
    if TARGET_MIN <= _dur(sentences, a, b) <= MAX_DUR and (not punctuated or _done(sentences[b])):
        return b  # the model's own ending is already fine
    if _dur(sentences, a, b) > MAX_DUR:
        order = range(b, a - 1, -1)   # too long: shorten, longest fit first
    else:
        order = range(b, n)           # too short or fine: lengthen only as far as needed
    best, best_score = None, -1.0
    for k in order:
        d = _dur(sentences, a, k)
        if d > MAX_DUR:
            if order.step > 0:
                break
            continue
        if d < TARGET_MIN:
            if order.step < 0:
                break
            continue
        score = _end_score(sentences, k)
        if score > best_score:
            best, best_score = k, score
    if best is not None:
        return best
    # no ideal end exists (e.g. episode ends first): old behaviour
    while _dur(sentences, a, b) < TARGET_MIN and b + 1 < n:
        b += 1
    while _dur(sentences, a, b) > MAX_DUR and b > a:
        b -= 1
    return b


def select_clips(sentences: list[Sentence], rules: str, client,
                 want: int = 10, focus=(), examples: str = "") -> list[Clip]:
    note = focus_note(focus)
    prompt = "\n\n".join(part for part in (
        rules, note, examples, _task(want + EXTRA, strict=bool(note)),
        f"جمله‌ها (شماره، زمان، متن):\n{_numbered(sentences)}") if part)
    try:
        raw = client.generate_json(prompt)
    except (BusyError, QuotaError):
        raise
    except Exception as e:  # noqa: BLE001
        raise SelectionError(str(e)) from e
    if not isinstance(raw, list):
        raise SelectionError("not a list")
    n = len(sentences)
    punctuated = _punctuated(sentences)
    clips = []
    drops = {"bad_index": 0, "bad_length": 0, "overlap": 0}
    for d in sorted(raw, key=lambda d: d.get("rank", 0) if isinstance(d, dict) else 0):
        try:
            rank, a, b = int(d["rank"]), int(d["from"]), int(d["to"])
            reason = str(d.get("reason", ""))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SelectionError("malformed item")
        if not (0 <= a <= b < n):
            drops["bad_index"] += 1
            continue
        if punctuated:
            a = _snap_start(sentences, a, b)
        b = _fit_end(sentences, a, b, punctuated)
        c = Clip(rank, sentences[a].start, sentences[b].end, reason,
                 " ".join(s.text for s in sentences[a:b + 1]), d.get("top") is True)
        if not (MIN_DUR <= _dur(sentences, a, b) <= MAX_DUR):
            drops["bad_length"] += 1
            continue
        if any(c.start < k.end and k.start < c.end for k in clips):
            drops["overlap"] += 1
            continue
        clips.append(c)
    print(f"selection: raw={len(raw)} kept={len(clips)} "
          + " ".join(f"{k}={v}" for k, v in drops.items())
          + f" sentences={n}", file=sys.stderr, flush=True)
    print(f"selection raw json: {json.dumps(raw, ensure_ascii=False)[:1500]}",
          file=sys.stderr, flush=True)
    final = clips[:min(want, MAX_CLIPS)]
    flagged = [c for c in final if c.top][:TOP_MAX] or final[:TOP_MAX]  # no flags: best-ranked win
    return [replace(c, rank=i, top=c in flagged) for i, c in enumerate(final, 1)]


class GeminiTextClient:
    def __init__(self, api_key: str, model: str = None):
        from google import genai
        from google.genai import types
        self._client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=180_000))
        self._models = [model] if model else models_from_env()

    def generate_json(self, prompt: str) -> list[dict]:
        resp = call_with_fallback(
            lambda m: self._client.models.generate_content(
                model=m, contents=prompt,
                config={"response_mime_type": "application/json"}),
            self._models)
        return json.loads(resp.text)


class FallbackClient:
    """Try the primary client; if it fails for any reason, use the secondary."""

    def __init__(self, primary, secondary):
        self.primary, self.secondary = primary, secondary

    def generate_json(self, prompt: str):
        try:
            return self.primary.generate_json(prompt)
        except Exception as e:  # noqa: BLE001
            print(f"primary selection client failed ({type(e).__name__}); using fallback",
                  file=sys.stderr, flush=True)
            return self.secondary.generate_json(prompt)


def make_select_client(env, gemini_key: str):
    """DeepSeek (falling back to Gemini) when DEEPSEEK_API_KEY is set, otherwise Gemini."""
    key = (env.get("DEEPSEEK_API_KEY") or "").strip()
    if key:
        from deepseek import FALLBACK_MODEL, MODEL, DeepSeekTextClient
        model = (env.get("DEEPSEEK_MODEL") or "").strip() or MODEL
        deepseek = FallbackClient(DeepSeekTextClient(key, model),
                                  DeepSeekTextClient(key, FALLBACK_MODEL))
        return FallbackClient(deepseek, GeminiTextClient(gemini_key))
    return GeminiTextClient(gemini_key)
