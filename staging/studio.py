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
MOODS = ("غمگین", "احساسی", "حماسی", "ریتمیک", "هیجانی", "سوزناک", "آرام", "عاشقانه", "نوستالژیک", "انگیزشی", "امیدوار")
DEFAULT_MOOD = "احساسی"
NEED_TEXT = ("متن گفتار این برش پیدا نشد.\nویدیو را روی پیام پیشنهادیِ همان برش ریپلای کن و در کپشن بنویس «رندر»؛ "
             "یا متن را بعد از «رندر» بیاور.")
FAILED = "ساخت ویدیو انجام نشد. لطفاً دوباره امتحان کن."
ASK_TEMPLATE = "این برش در کدام قالب ساخته شود؟"
ASK_NAME = "نام مهمان را بنویس"
ASK_TITLE = "عنوان یا سمت مهمان را بنویس"
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


def handle_callback(cb: dict, cfg, tg, ask_path=None) -> bool:
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
        remember_ask(ask_path, chat_id, st)
        tg.answer_callback(cb["id"])
    except Exception:  # noqa: BLE001 - a stale button must never crash the bot
        pass
    return True


def remember_ask(path, chat_id, st: dict) -> None:
    """The question we are waiting an answer to (name or title), so a plain message without
    «reply» is still understood as the answer."""
    if path is None:
        return
    data = _load_pending(path)
    data[str(chat_id)] = {"st": st, "at": time.time()}
    _save_pending(path, data)


def forget_ask(path, chat_id) -> None:
    if path is None:
        return
    data = _load_pending(path)
    if data.pop(str(chat_id), None) is not None:
        _save_pending(path, data)


def _advance(chat_id, st: dict, answer: str, reply_to, tg, make, ask_path) -> None:
    """One answer moves the render forward: first the name, then the title (which renders)."""
    if not st.get("name"):
        st["name"] = " ".join(answer.split())
        body, ents = build(ASK_TITLE, st)
        remember_ask(ask_path, chat_id, st)
        tg.send_message(chat_id, body, reply_to=reply_to, entities=ents)
        return
    st["title"] = " ".join(answer.split())
    forget_ask(ask_path, chat_id)
    make(chat_id, st)


def handle_reply(message: dict, cfg, tg, make, ask_path=None) -> bool:
    """A reply to one of our questions: first the name, then the title (which renders)."""
    prev = message.get("reply_to_message") or {}
    st = parse(prev)
    answer = (message.get("text") or "").strip()
    if st is None or not st.get("template") or not answer:
        return False
    _advance(message["chat"]["id"], st, answer, message.get("message_id"), tg, make, ask_path)
    return True


_MEDIA = ("video", "document", "audio", "voice", "photo", "video_note", "animation")


def handle_loose(message: dict, cfg, tg, make, ask_path) -> bool:
    """A plain text (no reply needed) answers the question we asked last, for up to PENDING_TTL."""
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()
    if ask_path is None or not text or any(k in message for k in _MEDIA) or _LINK.search(text) \
            or "http" in text or _TRIGGER.match(text):
        return False
    item = _load_pending(ask_path).get(str(chat_id))
    if not item or time.time() - item.get("at", 0) > PENDING_TTL or not isinstance(item.get("st"), dict):
        return False
    st = item["st"]
    if not st.get("template") or not st.get("ref") or not st.get("text"):
        return False
    _advance(chat_id, dict(st), text, message.get("message_id"), tg, make, ask_path)
    return True


def choose_mood(client, text: str) -> str:
    prompt = ("حال‌وهوای این گفتار را برای انتخاب موسیقی زمینه تعیین کن. موسیقی ویدیوها باید پرانرژی و "
              "احساسی باشد؛ «آرام» را فقط وقتی انتخاب کن که گفتار واقعاً آرام و بی‌هیجان است. فقط یکی از این‌ها: "
              + "، ".join(MOODS) + f"\nگفتار:\n{text}\n"
              'فقط JSON برگردان: [{"mood": "..."}]')
    try:
        rows = client.generate_json(prompt)
        mood = str(rows[0].get("mood", "")).strip()
    except Exception as e:  # noqa: BLE001 - music is never worth failing the video
        print(f"mood fallback: {type(e).__name__}", file=sys.stderr, flush=True)
        return DEFAULT_MOOD
    return mood if mood in MOODS else DEFAULT_MOOD


_FIX_PROMPT = ("این متن گفتار فارسی محاوره‌ای (با لهجه) است که از روی صدا نوشته شده و ممکن است چند کلمه‌ی "
               "غلط یا بی‌معنی داشته باشد (مثلاً «خوفی» به جای «خوشی» یا «خود خو» به جای «خودتو»). فقط غلط‌های "
               "بدیهی را با نزدیک‌ترین کلمه‌ی درست و هم‌صدا اصلاح کن. جمله‌ها را بازنویسی نکن، کلمه‌ی اضافه نیاور و "
               "چیزی را حذف نکن؛ علائم نگارشی سر جایش بماند. اگر مطمئن نیستی، کلمه را همان‌طور بگذار.\n"
               "متن:\n%s\nفقط JSON برگردان: [{\"text\": \"متن اصلاح‌شده\"}]")
MAX_FIXED = 0.15  # more changed words than this share means the model rewrote the text: keep the original


def hear_again(client, audio: Path, text: str) -> str:
    """The caption text checked against the video's own voice; typography cleaned either way."""
    from persian import normalize_fa
    from proofread import accept
    text = normalize_fa(text)
    try:
        new = normalize_fa(client.correct_text(audio, text))
    except Exception as e:  # noqa: BLE001 - a text pass is never worth failing the video
        print(f"audio text check skipped: {type(e).__name__}", file=sys.stderr, flush=True)
        return text
    return new if new and accept(text, new) else text


def fix_text(client, text: str) -> str:
    """Obvious mis-heard words corrected; anything doubtful stays as it was (captions must match the voice)."""
    from difflib import SequenceMatcher
    words = text.split()
    try:
        rows = client.generate_json(_FIX_PROMPT % text)
        new = str(rows[0].get("text", "")).split()
    except Exception as e:  # noqa: BLE001 - a spelling pass is never worth failing the video
        print(f"text fix skipped: {type(e).__name__}", file=sys.stderr, flush=True)
        return text
    if not new:
        return text
    out, changed = [], 0
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, words, new, autojunk=False).get_opcodes():
        if tag == "equal":
            out += words[i1:i2]
        elif (tag == "replace" and i2 - i1 <= 2 and j2 - j1 <= 2
              and SequenceMatcher(None, "".join(words[i1:i2]), "".join(new[j1:j2])).ratio() >= 0.45):
            out += new[j1:j2]
            changed += i2 - i1
        else:
            out += words[i1:i2]
    if changed > max(3, len(words) * MAX_FIXED):
        return text
    from persian import normalize_fa
    return normalize_fa(" ".join(out))


def _fetch_url(url: str, dest: Path) -> None:
    import urllib.request
    with urllib.request.urlopen(url, timeout=120) as r:
        Path(dest).write_bytes(r.read())


def pick_music(mood: str, channel_rows: list, tg, workdir: Path, lufs_of, rng=random, fetch=_fetch_url):
    """(file, lufs) of a track from our own library (channel posts and bundled files): one tagged
    with the mood, else any other; (None, None) when nothing is usable. Music is never synthesized."""
    usable = [r for r in channel_rows if r.get("file_id") or r.get("url")]
    ordered = [r for r in usable if mood in r.get("tags", [])] or usable
    rows = list(ordered)
    rng.shuffle(rows)
    for row in rows:
        try:
            dest = Path(workdir) / "music_in.mp3"
            if row.get("url"):
                fetch(row["url"], dest)
            else:
                tg.download(tg.get_file_path(row["file_id"]), dest)
            return str(dest), lufs_of(str(dest))
        except Exception as e:  # noqa: BLE001
            print(f"music track failed: {type(e).__name__}", file=sys.stderr, flush=True)
    return None, None


def make_video(chat_id: int, st: dict, tg, align_client, select_client, workdir: Path, music_catalog: Path) -> None:
    """Fetch the clip, time its words, pick music, render and send."""
    import gap_render
    try:
        prog = Progress(tg, chat_id, f"ساخت ویدیو · قالب «{TEMPLATES[st['template']]}»")
        workdir = Path(workdir)
        prog.set(3, "دریافت ویدیو", force=True)
        kind, _, rid = st["ref"].partition(":") if st["ref"].startswith("drive:") else ("telegram", "", st["ref"])
        video = fetch_input(InputRef(kind, rid), tg, workdir)
        prog.set(12, "استخراج صدا")
        audio = to_audio(video, workdir / "studio_audio.mp3")
        prog.set(18, "زمان‌بندی کلمات")
        st = {**st, "text": fix_text(select_client, hear_again(align_client, audio, st["text"]))}
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
        import music_bundled
        music_file, music_lufs = pick_music(mood, [*channel, *music_bundled.ROWS], tg, workdir,
                                            gap_render.lufs_of)
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
