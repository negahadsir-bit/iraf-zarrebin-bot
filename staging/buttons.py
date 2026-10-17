from categories import CATS
from feedback import handle_feedback

GO = "go"
MARK = "✅ "
ASK = "این مصاحبه بیشتر در کدام دسته است؟ (می‌توانی چند تا انتخاب کنی؛ اگر نمی‌دانی مستقیم شروع را بزن)"
NO_ORIGINAL = "پیام اصلی پیدا نشد؛ فایل یا لینک را دوباره بفرست."


def keyboard(selected) -> dict:
    sel = set(selected)
    btn = [{"text": (MARK if i in sel else "") + name, "callback_data": f"t{i}"}
           for i, name in enumerate(CATS)]
    return {"inline_keyboard": [btn[:2], btn[2:4], btn[4:], [{"text": "▶️ شروع بررسی", "callback_data": GO}]]}


def selected_from(markup: dict | None) -> set[int]:
    """The chat message itself is the state: read which buttons are ticked."""
    out = set()
    for row in (markup or {}).get("inline_keyboard", []):
        for b in row:
            data = b.get("callback_data", "")
            if data.startswith("t") and b.get("text", "").startswith(MARK.strip()):
                out.add(int(data[1:]))
    return out


def handle_callback(cb: dict, cfg, tg, process_fn, commit=None, feedback_path=None) -> None:
    """Toggle a category, or on «شروع» run the pending file (the question message is a
    reply to it, so Telegram hands the original message back to us: no stored state)."""
    if handle_feedback(cb, cfg, tg, feedback_path):
        return
    msg = cb.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    if chat_id not in cfg.allowed_chat_ids:
        return
    sel = selected_from(msg.get("reply_markup"))
    data = cb.get("data", "")
    if data.startswith("t") and data[1:].isdigit() and int(data[1:]) < len(CATS):
        sel ^= {int(data[1:])}
        try:
            tg.edit_markup(chat_id, msg["message_id"], keyboard(sel))
            tg.answer_callback(cb["id"])
        except Exception:  # noqa: BLE001 - a stale button must never crash the bot
            pass
        return
    if data != GO:
        try:
            tg.answer_callback(cb["id"])  # e.g. the already-done marker: just stop the spinner
        except Exception:  # noqa: BLE001
            pass
        return
    if commit:
        commit()
    try:
        tg.answer_callback(cb["id"])
    except Exception:  # noqa: BLE001 - too-old queries are refused by Telegram
        pass
    original = msg.get("reply_to_message")
    if not original:
        tg.send_message(chat_id, NO_ORIGINAL)
        return
    picked = tuple(sorted(sel))
    label = "، ".join(CATS[i] for i in picked) or "بدون تعیین دسته"
    try:
        tg.edit_message(chat_id, msg["message_id"], f"🏷 دسته: {label}\n⏳ دریافت شد؛ در حال بررسی… (چند دقیقه طول می‌کشد)",
                        reply_markup={"inline_keyboard": []})
    except Exception:  # noqa: BLE001
        pass
    for text in process_fn(original, picked):
        tg.send_message(chat_id, text)
