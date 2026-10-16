"""Template studio: a cut clip sent with the caption «رندر» + its text is rendered in the
chosen template (گپ / یاران خراسانی) with the guest name and title, word-synced subtitles
and background music picked by the mood of the speech. All state lives in the bot's own
chat messages (button → reply with the name → reply with the title), nothing is stored."""
import base64
import json
import random
import re
import shutil
import time
import sys
import zlib
from pathlib import Path

from media import InputError, InputRef, extract_drive_id, fetch_input, resolve_input, to_audio

TEMPLATES = {"gap": "گپ", "yaran": "یاران خراسانی"}
HEAD = "🎬"
_LINK = re.compile(r"https?://\S*drive\.google\.com\S*")
_TRIGGER = re.compile(r"(?is)^\s*(?:رندر|render)(?=\s|$)\s*(.*)$")
MOODS = ("غمگین", "احساسی", "حماسی", "آرام", "عاشقانه", "نوستالژیک", "انگیزشی", "امیدوار")
DEFAULT_MOOD = "آرام"
NEED_TEXT = ("متن گفتار این برش پیدا نشد.\nویدیو را روی پیام پیشنهادیِ همان برش ریپلای کن و در کپشن بنویس «رندر»؛ "
             "یا متن را بعد از «رندر» بیاور.")
FAILED = "ساخت ویدیو انجام نشد. لطفاً دوباره امتحان کن."
ASK_TEMPLATE = "این برش در کدام قالب ساخته شود؟"
ASK_NAME = "نام مهمان را بنویس (روی همین پیام ریپلای کن)"
ASK_TITLE = "عنوان یا سمت مهمان را بنویس (روی همین پیام ریپلای کن)"
BAR_LEN = 10
_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def bar(pct: float) -> str:
    """«▰▰▰▱▱▱▱▱▱▱ ۳۰٪»: the progress line shown while a video is being made."""
    pct = max(0, min(100, int(pct)))
    full = round(BAR_LEN * pct / 100)
    return "▰" * full + "▱" * (BAR_LEN - full) + " " + f"{pct}٪".translate(_FA)


class Progress:
    """One message that fills up as the work advances (edits are throttled; a failed edit never matters)."""

    def __init__(self, tg, chat_id: int, title: str, clock=time.monotonic):
        self.tg, self.chat_id, self.title, self.clock = tg, chat_id, title, clock
        self.mid, self.last, self.shown = None, 0.0, ""
        self.set(0, "در صف شروع")

    def _text(self, pct, label):
        return f"⏳ {self.title}\n{bar(pct)}\n{label}"

    def set(self, pct: float, label: str, force: bool = False) -> None:
        text = self._text(pct, label)
        now = self.clock()
        if text == self.shown or (not force and self.mid is not None and now - self.last < 2.5):
            return
        try:
            if self.mid is None:
                self.mid = self.tg.send_message(self.chat_id, text)
            else:
                self.tg.edit_message(self.chat_id, self.mid, text)
            self.shown, self.last = text, now
        except Exception:  # noqa: BLE001
            pass

    def done(self, label: str = "آماده شد ✅") -> None:
        if self.mid is not None:
            try:
                self.tg.edit_message(self.chat_id, self.mid, f"✅ {self.title}\n{bar(100)}\n{label}")
            except Exception:  # noqa: BLE001
                pass
STATE_URL = "https://t.me/iraf_state?d="
MAX_URL = 1900


def render_text(message: dict) -> str | None:
    """The clip text when the caption starts with «رندر»; None for any other message."""
    m = _TRIGGER.match(message.get("caption") or message.get("text") or "")
    return _LINK.sub(" ", m.group(1)).strip() if m else None


def reply_dialogue(message: dict) -> str:
    """The clip's speech from the suggestion message this video replies to (the text inside «…»)."""
    m = re.search(r"«(.+)»", (message.get("reply_to_message") or {}).get("text") or "", re.S)
    return " ".join(m.group(1).split()) if m else ""


def keyboard() -> dict:
    return {"inline_keyboard": [[{"text": n, "callback_data": f"tp:{k}"} for k, n in TEMPLATES.items()]]}


def _pack(st: dict) -> str:
    raw = json.dumps(st, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=")


def _unpack(blob: str) -> dict | None:
    try:
        raw = zlib.decompress(base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4)))
        st = json.loads(raw.decode("utf-8"))
    except (ValueError, zlib.error):
        return None
    return st if isinstance(st, dict) and st.get("ref") and st.get("text") else None


def build(header: str, st: dict):
    """(text, entities): the visible message is just the question; the state hides in a link on 🎬."""
    st = {k: (" ".join(v.split()) if k in ("text", "name", "title") else v) for k, v in st.items() if v}
    url = STATE_URL + _pack(st)
    if len(url) <= MAX_URL:
        return f"{HEAD} {header}", [{"type": "text_link", "offset": 0, "length": len(HEAD.encode("utf-16-le")) // 2,
                                      "url": url}]
    lines = [f"{HEAD} {header}"]  # too long for a link: keep the state visible
    if st.get("template"):
        lines.append(f"قالب: {TEMPLATES[st['template']]}")
    if st.get("name"):
        lines.append(f"نام: {st['name']}")
    lines += ["متن: " + st["text"], f"ref: {st['ref']}"]
    return "\n".join(lines), None


def parse(message: dict) -> dict | None:
    """State of one of our messages, or None when it is not one."""
    for e in message.get("entities") or []:
        if e.get("type") == "text_link" and str(e.get("url", "")).startswith(STATE_URL):
            return _unpack(e["url"][len(STATE_URL):])
    text = message.get("text") or ""
    if not text.startswith(HEAD):
        return None
    st = {}
    for line in text.split("\n")[1:]:
        key, _, val = line.partition(": ")
        if key == "قالب":
            st["template"] = next((k for k, n in TEMPLATES.items() if n == val.strip()), None)
        elif key == "نام":
            st["name"] = val.strip()
        elif key == "متن":
            st["text"] = val.strip()
        elif key == "ref":
            st["ref"] = val.strip()
    return st if st.get("ref") and st.get("text") else None


_RANGE = re.compile(r"(\d+:\d{2}(?:\.\d)?)\s*[–-]\s*(\d+:\d{2}(?:\.\d)?)(?:\s*\((\d+)ث\))?")
PENDING_TTL = 3 * 3600
WAIT_VIDEO = ("🎞 ویدیوی همین برش را بفرست\n⏱ بازه: {a} تا {b}\n"
              "به محض رسیدنش، مرحله‌های بعد (قالب، نام و عنوان) شروع می‌شود.")


def _load_pending(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_pending(path, data: dict) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def handle_make(cb: dict, cfg, tg, pending_path) -> bool:
    """«🎬 تولید ویدیو» under a clip suggestion: remember its text, ask for the cut video."""
    if cb.get("data") != "mk":
        return False
    msg = cb.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    if chat_id not in cfg.allowed_chat_ids:
        return True
    text = reply_dialogue({"reply_to_message": msg})
    try:
        if not text:
            tg.answer_callback(cb["id"], "متن این برش پیدا نشد")
            return True
        m = _RANGE.search(msg.get("text") or "")
        a, b, secs = (m.group(1), m.group(2), int(m.group(3) or 0)) if m else ("—", "—", 0)
        data = _load_pending(pending_path)
        data[str(chat_id)] = {"text": text, "secs": secs, "at": time.time()}
        _save_pending(pending_path, data)
        tg.answer_callback(cb["id"], "ویدیوی این برش را بفرست")
        tg.send_message(chat_id, WAIT_VIDEO.format(a=a, b=b), reply_to=msg.get("message_id"))
    except Exception:  # noqa: BLE001 - a stale button must never crash the bot
        pass
    return True


def take_pending(pending_path, message: dict) -> str | None:
    """The clip text waiting for this chat's next video (or Drive link); None when nothing waits.
    A video far longer than the asked clip is not that clip (probably a whole interview)."""
    chat_id = str(message.get("chat", {}).get("id"))
    media = message.get("video") or message.get("document") or {}
    is_video = "video" in message or str(media.get("mime_type", "")).startswith("video/")
    if not (is_video or extract_drive_id(f"{message.get('text', '')} {message.get('caption', '')}")):
        return None
    data = _load_pending(pending_path)
    item = data.get(chat_id)
    if not item or time.time() - item.get("at", 0) > PENDING_TTL:
        return None
    secs, dur = item.get("secs", 0), media.get("duration", 0)
    if secs and dur and dur > secs * 3 + 30:
        return None
    del data[chat_id]
    _save_pending(pending_path, data)
    return item.get("text") or None


def start(message: dict, text: str, tg) -> None:
    chat_id = message["chat"]["id"]
    text = text or reply_dialogue(message)
    if not text:
        tg.send_message(chat_id, NEED_TEXT)
        return
    try:
        ref = resolve_input(message)
    except InputError as e:
        tg.send_message(chat_id, str(e))
        return
    key = ref.ref if ref.kind == "telegram" else "drive:" + ref.ref
    body, ents = build(ASK_TEMPLATE, {"text": text, "ref": key})
    tg.send_message(chat_id, body, reply_markup=keyboard(), reply_to=message.get("message_id"), entities=ents)


def handle_callback(cb: dict, cfg, tg) -> bool:
    """«tp:<template>» buttons; True when the callback was ours."""
    data = cb.get("data", "")
    if not data.startswith("tp:"):
        return False
    msg = cb.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    key = data[3:]
    st = parse(msg)
    if chat_id not in cfg.allowed_chat_ids or st is None or key not in TEMPLATES:
        return True
    st["template"] = key
    try:
        body, ents = build(ASK_NAME, st)
        tg.edit_message(chat_id, msg["message_id"], body, reply_markup={"inline_keyboard": []}, entities=ents)
        tg.answer_callback(cb["id"])
    except Exception:  # noqa: BLE001 - a stale button must never crash the bot
        pass
    return True


def handle_reply(message: dict, cfg, tg, make) -> bool:
    """A reply to one of our questions: first the name, then the title (which renders)."""
    prev = message.get("reply_to_message") or {}
    st = parse(prev)
    answer = (message.get("text") or "").strip()
    if st is None or not st.get("template") or not answer:
        return False
    chat_id = message["chat"]["id"]
    if not st.get("name"):
        st["name"] = " ".join(answer.split())
        body, ents = build(ASK_TITLE, st)
        tg.send_message(chat_id, body, reply_to=message.get("message_id"), entities=ents)
        return True
    st["title"] = " ".join(answer.split())
    make(chat_id, st)
    return True


def choose_mood(client, text: str) -> str:
    prompt = ("حال‌وهوای این گفتار را برای انتخاب موسیقی زمینه تعیین کن. فقط یکی از این‌ها: "
              + "، ".join(MOODS) + f"\nگفتار:\n{text}\n"
              'فقط JSON برگردان: [{"mood": "..."}]')
    try:
        rows = client.generate_json(prompt)
        mood = str(rows[0].get("mood", "")).strip()
    except Exception as e:  # noqa: BLE001 - music is never worth failing the video
        print(f"mood fallback: {type(e).__name__}", file=sys.stderr, flush=True)
        return DEFAULT_MOOD
    return mood if mood in MOODS else DEFAULT_MOOD


def pick_music(mood: str, channel_rows: list, tg, workdir: Path, lufs_of, compose, presets, rng=random):
    """(file, lufs): a channel track tagged with the mood, else an original synthesized bed."""
    tagged = [r for r in channel_rows if r.get("file_id") and mood in r.get("tags", [])]
    if tagged:
        row = rng.choice(tagged)
        try:
            dest = Path(workdir) / "music_in.mp3"
            tg.download(tg.get_file_path(row["file_id"]), dest)
            return str(dest), lufs_of(str(dest))
        except Exception as e:  # noqa: BLE001
            print(f"channel track failed, composing: {type(e).__name__}", file=sys.stderr, flush=True)
    preset = presets.get(mood, "calm_ambient")
    row = compose(preset, str(workdir))
    path = str(Path(workdir) / row["file"])
    return path, lufs_of(path)


def make_video(chat_id: int, st: dict, tg, align_client, select_client, workdir: Path, music_catalog: Path) -> None:
    """Fetch the clip, time its words, pick music, render and send."""
    import gap_render
    import music_synth
    try:
        prog = Progress(tg, chat_id, f"ساخت ویدیو · قالب «{TEMPLATES[st['template']]}»")
        workdir = Path(workdir)
        prog.set(3, "دریافت ویدیو", force=True)
        kind, _, rid = st["ref"].partition(":") if st["ref"].startswith("drive:") else ("telegram", "", st["ref"])
        video = fetch_input(InputRef(kind, rid), tg, workdir)
        prog.set(12, "استخراج صدا")
        audio = to_audio(video, workdir / "studio_audio.mp3")
        prog.set(18, "زمان‌بندی کلمات")
        rows = align_client.align_words(audio, st["text"])
        if not rows:
            raise ValueError("no word times")
        prog.set(32, "انتخاب حال‌وهوا")
        mood = choose_mood(select_client, st["text"])
        prog.set(38, f"موسیقی زمینه · {mood}")
        try:
            channel = json.loads(Path(music_catalog).read_text(encoding="utf-8")) if Path(music_catalog).exists() else []
        except (OSError, ValueError):
            channel = []
        music_file, music_lufs = pick_music(mood, channel, tg, workdir, gap_render.lufs_of, music_synth.compose,
                                            music_synth.MOOD_TO_PRESET)
        out = str(workdir / "studio_out.mp4")
        prog.set(45, "طراحی زیرنویس‌ها", force=True)
        gap_render.main(str(video), st["text"], st["name"], st["title"], out, str(workdir / "render"), rows=rows,
                        music_file=music_file, music_lufs=music_lufs, template=st["template"],
                        progress=lambda f: prog.set(50 + 42 * f, "ساخت و ترکیب ویدیو"))
        try:  # keep the timing data of every render so a sync complaint can be checked against it
            keep = Path(music_catalog).parent.parent / "renders"
            keep.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^0-9A-Za-z_-]", "_", st["ref"])[:40]
            shutil.copyfile(workdir / "render" / "debug.json", keep / f"{safe}.json")
        except OSError:
            pass
        prog.set(94, "ارسال ویدیو", force=True)
        tg.send_video(chat_id, out, caption=f"{st['name']} — {st['title']}\n🎵 حال‌وهوا: {mood}")
        prog.done()
    except InputError as e:
        tg.send_message(chat_id, str(e))
    except Exception as e:  # noqa: BLE001 - always answer the user
        print(f"studio failed: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr, flush=True)
        tg.send_message(chat_id, FAILED)
