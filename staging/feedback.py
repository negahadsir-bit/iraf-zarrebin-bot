import re
from pathlib import Path

from categories import CATS
from memory import CLIP_CHARS, save_feedback, save_reason, save_viral

_DATA = re.compile(r"f([+-])(\d{1,2})")
_VIRAL = re.compile(r"v([+-])(\d{1,2})([yns])")
_DIALOGUE = re.compile(r"«(.*)»", re.S)
DONE = "noop"
REASON_MARK = "✍️ دلیل ضعیف بودن"
_CATLINE = re.compile(r"دسته: (.*)")


def cats_mask(cats) -> int:
    return sum(1 << i for i in set(cats) if 0 <= i < len(CATS))


MAKE_BTN = {"text": "🎬 تولید ویدیو", "callback_data": "mk"}


def with_make(rows: list) -> dict:
    """Every clip keeps its «تولید ویدیو» button, whatever else is pressed."""
    return {"inline_keyboard": [*rows, [MAKE_BTN]]}


def clip_keyboard(cats) -> dict:
    """👍/👎 under a clip; the interview's categories ride along in the button data (stateless)."""
    m = cats_mask(cats)
    return with_make([[{"text": "👍 خوب بود", "callback_data": f"f+{m}"},
                       {"text": "👎 ضعیف بود", "callback_data": f"f-{m}"}]])


def viral_keyboard(good: bool, mask: int) -> dict:
    """Second step, shown after 👍/👎: does this clip have viral potential, by the user's experience?"""
    g = "+" if good else "-"
    return with_make([[{"text": "🚀 وایرال می‌شد", "callback_data": f"v{g}{mask}y"},
                       {"text": "🧊 وایرال نمی‌شد", "callback_data": f"v{g}{mask}n"}],
                      [{"text": "⏭ رد کردن", "callback_data": f"v{g}{mask}s"}]])


def reason_prompt(cats, dialogue: str) -> str:
    """The question sent after 👎. It carries the clip and its categories because replies
    only point back at this message (the bot keeps no state)."""
    names = "، ".join(CATS[i] for i in cats if 0 <= i < len(CATS))
    return (f"{REASON_MARK} این برش چی بود؟ همین‌جا جواب بنویس تا یاد بگیرم "
            f"(نخواستی جواب نده).\n\n🏷 دسته: {names or '—'}\n«{dialogue}»")


def handle_reason(msg: dict, cfg, tg, path: Path | None) -> bool:
    """A text reply to the «why was it weak» question is saved as the clip's reason.
    Returns True when the message belonged to that flow."""
    asked = (msg.get("reply_to_message") or {}).get("text", "")
    if not asked.startswith(REASON_MARK):
        return False
    chat_id = msg.get("chat", {}).get("id")
    reason = (msg.get("text") or "").strip()
    if chat_id not in cfg.allowed_chat_ids or not reason:
        return chat_id not in cfg.allowed_chat_ids
    line = _CATLINE.search(asked)
    names = [n.strip() for n in line.group(1).split("،")] if line else []
    cats = [i for i, name in enumerate(CATS) if name in names]
    d = _DIALOGUE.search(asked)
    dialogue = (d.group(1) if d else "").strip()[:CLIP_CHARS]
    if path is not None:
        try:
            save_reason(path, cats, dialogue, reason)
        except OSError:
            pass
    try:
        tg.send_message(chat_id, "✅ دلیلش ثبت شد؛ از این به بعد حواسم بهش هست.",
                        reply_to=msg.get("message_id"))
    except Exception:  # noqa: BLE001
        pass
    return True


def _dialogue(msg: dict) -> str:
    text = msg.get("text", "")
    d = _DIALOGUE.search(text)
    return (d.group(1) if d else text).strip()[:CLIP_CHARS]


def _edit(tg, chat_id, msg, markup) -> None:
    try:
        tg.edit_markup(chat_id, msg["message_id"], markup)
    except Exception:  # noqa: BLE001
        pass


def _answer(tg, cb, text="ثبت شد") -> None:
    try:
        tg.answer_callback(cb["id"], text)
    except Exception:  # noqa: BLE001
        pass


def handle_feedback(cb: dict, cfg, tg, path: Path | None) -> bool:
    """Record a 👍/👎 press, then the viral question that follows it.
    Returns True when the callback belonged to this feedback flow."""
    data = cb.get("data", "")
    v = _VIRAL.fullmatch(data)
    if v:
        return _handle_viral(cb, cfg, tg, path, v)
    m = _DATA.fullmatch(data)
    if not m:
        return False
    msg = cb.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    if chat_id not in cfg.allowed_chat_ids:
        return True
    good = m.group(1) == "+"
    mask = int(m.group(2))
    cats = [i for i in range(len(CATS)) if mask >> i & 1]
    dialogue = _dialogue(msg)
    if path is not None and dialogue:
        try:
            save_feedback(path, good, cats, dialogue)
        except OSError:
            pass
    if good:
        _edit(tg, chat_id, msg, viral_keyboard(good, mask))
        _answer(tg, cb, "ثبت شد؛ حالا وایرال بودنش؟")
    else:  # a weak clip is obviously not viral: no second question
        _edit(tg, chat_id, msg, with_make([[{"text": "✅ ثبت شد 👎", "callback_data": DONE}]]))
        _answer(tg, cb)
        try:  # ask why: the answer is optional and becomes part of what the bot learns from
            tg.send_message(chat_id, reason_prompt(cats, dialogue), reply_to=msg.get("message_id"),
                            reply_markup={"force_reply": True, "selective": True,
                                          "input_field_placeholder": "دلیلش چی بود؟ (اختیاری)"})
        except Exception:  # noqa: BLE001
            pass
    return True


def _handle_viral(cb: dict, cfg, tg, path: Path | None, v) -> bool:
    msg = cb.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    if chat_id not in cfg.allowed_chat_ids:
        return True
    good, mask, answer = v.group(1) == "+", int(v.group(2)), v.group(3)
    cats = [i for i in range(len(CATS)) if mask >> i & 1]
    dialogue = _dialogue(msg)
    if answer != "s" and path is not None and dialogue:
        try:
            save_viral(path, answer == "y", cats, dialogue)
        except OSError:
            pass
    mark = {"y": " 🚀", "n": " 🧊", "s": ""}[answer]
    _edit(tg, chat_id, msg, with_make([[{"text": "✅ ثبت شد " + ("👍" if good else "👎") + mark,
                                         "callback_data": DONE}]]))
    _answer(tg, cb)
    return True
