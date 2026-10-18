import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path

from feedback import clip_keyboard
from formatting import format_clip
from categories import rules_for
from memory import examples_note, save_interview
from media import InputError, fetch_input, resolve_input, to_audio
from retry import BusyError, QuotaError
from select_clips import SelectionError, select_clips
from verify import verify_clips
from polish import polish_edges
from proofread import proofread_clips
from refine import refine_clips
from silence import detect_silences, drop_overlaps, snap_to_silence
from transcribe import TranscriptionError, audio_seconds, transcribe

QUOTA = "سهمیه‌ی رایگان Gemini پر شده؛ کمی بعد دوباره بفرست."
NO_SPEECH = "هیچ گفتاری در فایل پیدا نشد."
SELECTION_FAILED = "انتخاب کلیپ‌ها ناموفق بود؛ دوباره امتحان کن."
BUSY = "Gemini الان شلوغ است؛ چند دقیقه بعد دوباره بفرست."
GENERIC = "مشکلی پیش آمد؛ کمی بعد دوباره امتحان کن."


@dataclass
class Deps:
    tg: object
    transcribe_client: object
    select_client: object
    workdir: Path
    rules: str
    memory: Path | None = None
    feedback: Path | None = None


def _bar(done: int, total: int, width: int = 10) -> str:
    filled = round(width * done / total)
    return "▓" * filled + "░" * (width - filled)


class _Progress:
    """One status message in the chat, edited as work advances (best effort)."""

    def __init__(self, message: dict, deps: Deps):
        self._chat_id = message.get("chat", {}).get("id")
        self._tg = deps.tg
        self._msg_id = None
        self._lock = threading.Lock()

    def _show(self, text: str) -> None:
        if self._chat_id is None or self._tg is None:
            return
        with self._lock:  # chunks finish on several threads
            try:
                if self._msg_id is None:
                    self._msg_id = self._tg.send_message(self._chat_id, text)
                else:
                    self._tg.edit_message(self._chat_id, self._msg_id, text)
            except Exception:  # noqa: BLE001
                pass

    def __call__(self, done: int, total: int) -> None:
        print(f"transcribed chunk {done}/{total}", file=sys.stderr, flush=True)
        self._show(f"⏳ {_bar(done, total)} {round(100 * done / total)}%  (تکه {done} از {total})")

    def selecting(self) -> None:
        self._show("✍️ متن کامل شد؛ در حال انتخاب بهترین برش‌ها…")


def process(message: dict, deps: Deps, cats: tuple = ()) -> list[str]:
    chat_id = message.get("chat", {}).get("id")
    progress = _Progress(message, deps)
    unsent: list[str] = []
    skipped: list[int] = []
    try:
        ref = resolve_input(message)
        src = fetch_input(ref, deps.tg, deps.workdir)
        audio = to_audio(src, deps.workdir / "audio.mp3")
        sentences = transcribe(audio, deps.transcribe_client, workdir=deps.workdir,
                               progress=progress,
                               on_skip=lambda i, n: skipped.append(i))
        progress.selecting()
        try:
            examples = examples_note(deps.memory, cats, deps.feedback)
        except Exception:  # noqa: BLE001 - learning is a bonus, never a blocker
            examples = ""
        clips = select_clips(sentences, rules_for(deps.rules, cats), deps.select_client, focus=cats,
                             examples=examples)
        try:  # re-listen to each clip's surroundings: short audio gives accurate times
            clips = refine_clips(clips, audio, audio_seconds(audio), deps.transcribe_client,
                                 deps.workdir)
        except Exception as e:  # noqa: BLE001
            print(f"refine skipped: {type(e).__name__}", file=sys.stderr, flush=True)
        try:  # confirm both edges by listening to short windows around them
            clips = polish_edges(clips, audio, audio_seconds(audio), deps.transcribe_client,
                                 deps.workdir)
        except Exception as e:  # noqa: BLE001
            print(f"polish skipped: {type(e).__name__}", file=sys.stderr, flush=True)
        try:  # double-check clips whose place is doubtful (unconfirmed edges, end of the file)
            clips = verify_clips(clips, audio, audio_seconds(audio), deps.transcribe_client,
                                 deps.workdir)
        except Exception as e:  # noqa: BLE001
            print(f"verify skipped: {type(e).__name__}", file=sys.stderr, flush=True)
        try:  # then pull edges to real pauses in the audio
            clips = snap_to_silence(clips, detect_silences(audio))
        except Exception as e:  # noqa: BLE001 - never lose clips over a refinement step
            print(f"silence snapping skipped: {type(e).__name__}", file=sys.stderr, flush=True)
        clips = drop_overlaps(clips)  # re-timing can reveal two clips on the same speech
        try:  # last: fix mis-heard / misspelled words of each clip against its own audio
            clips = proofread_clips(clips, sentences, audio, audio_seconds(audio),
                                    deps.transcribe_client, deps.workdir)
        except Exception as e:  # noqa: BLE001
            print(f"proofread skipped: {type(e).__name__}", file=sys.stderr, flush=True)
        if cats and clips and deps.memory is not None:
            try:  # the user's own category choice teaches later runs
                save_interview(deps.memory, cats, clips)
            except Exception as e:  # noqa: BLE001
                print(f"memory not saved: {type(e).__name__}", file=sys.stderr, flush=True)
        for clip in clips:
            text = format_clip(clip, sentences)
            try:
                deps.tg.send_message(chat_id, text, reply_markup=clip_keyboard(cats))
            except Exception:  # noqa: BLE001 - keep the clip, return it at the end
                unsent.append(text)
        if not clips:
            return ["هیچ برش مناسبی پیدا نشد."]
        done = f"✅ تمام شد: {len(clips)} پیشنهاد برش"
        tops = [str(c.rank) for c in clips if c.top]
        if tops:
            done += f"\n🔥 اولویت پیشنهادی من (وایرال‌ترین‌ها): برش {'، '.join(tops)}"
        if skipped:
            nums = "، ".join(str(i) for i in sorted(skipped))
            done += f"\n⚠ تکه {nums} (هر کدام ۵ دقیقه) به‌خاطر شلوغی Gemini رد شد."
        return unsent + [done]
    except InputError as e:
        return [str(e)]
    except BusyError as e:
        print(f"busy: {e}", file=sys.stderr)
        return unsent + [BUSY]
    except QuotaError as e:
        print(f"quota: {e}", file=sys.stderr)
        return unsent + [QUOTA]
    except TranscriptionError:
        return unsent + [NO_SPEECH]
    except SelectionError:
        return unsent + [SELECTION_FAILED]
    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return unsent + [GENERIC]
