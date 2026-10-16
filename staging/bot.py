import os
import sys
import tempfile
import time
from pathlib import Path

from buttons import ASK, handle_callback, keyboard
from config import ConfigError, load_config
from feedback import handle_reason
from media import InputError, resolve_input
from music import add_post, log_seen
from netguard import NetGuard
from pipeline import Deps, process
import studio
from wordsmode import run_words, words_text
from telegram_api import Telegram, TelegramError

BUDGET_SECONDS = 5 * 60
MEMORY = Path(__file__).parent / "memory" / "labels.jsonl"
FEEDBACK = MEMORY.parent / "feedback.jsonl"
MUSIC = MEMORY.parent / "music" / "catalog.json"
PENDING = MEMORY.parent / "pending.json"  # the clip whose «تولید ویدیو» button was pressed, waiting for its video


def handle_update(update: dict, cfg, tg, process_fn, commit=None, feedback_path=None,
                  words=None, music_path=None, make=None, pending_path=None) -> int:
    """Handle one update; returns next offset. A file/link is first answered with the
    category question (nothing slow happens yet). Pressing «شروع» runs the work; the
    offset is committed right before that, so a crashed run never repeats the same file."""
    nxt = update["update_id"] + 1
    if update.get("callback_query"):
        if studio.handle_callback(update["callback_query"], cfg, tg):
            return nxt
        if pending_path is not None and studio.handle_make(update["callback_query"], cfg, tg, pending_path):
            return nxt
        handle_callback(update["callback_query"], cfg, tg, process_fn,
                        commit=(lambda: commit(nxt)) if commit else None,
                        feedback_path=feedback_path)
        return nxt
    post = update.get("channel_post") or update.get("edited_channel_post")
    if post:  # music channel: keep a catalog of its tracks and their hashtags
        if music_path is not None:
            log_seen(music_path.parent / "seen.jsonl", update)
            add_post(music_path, post)
        return nxt
    msg = update.get("message")
    if not msg or msg.get("chat", {}).get("id") not in cfg.allowed_chat_ids:
        return nxt
    chat_id = msg["chat"]["id"]
    if make is not None and studio.handle_reply(msg, cfg, tg, make):  # name / title of a render
        return nxt
    if handle_reason(msg, cfg, tg, feedback_path):
        return nxt
    rtext = studio.render_text(msg)
    if rtext is not None:  # caption «رندر»: ask which template
        studio.start(msg, rtext, tg)
        return nxt
    waiting = studio.take_pending(pending_path, msg) if pending_path is not None else None
    if waiting:  # the video of the clip we asked for: go on with template, name and title
        studio.start(msg, waiting, tg)
        return nxt
    text = words_text(msg)
    if text is not None and words is not None:  # caption «words»: word times for subtitles
        words(msg, text)
        return nxt
    try:
        resolve_input(msg)
    except InputError as e:
        tg.send_message(chat_id, str(e))
        return nxt
    tg.send_message(chat_id, ASK, reply_markup=keyboard(()), reply_to=msg.get("message_id"))
    return nxt


def main() -> int:
    try:
        cfg = load_config(os.environ)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 1
    from select_clips import make_select_client
    from transcribe import GeminiClient
    tg = Telegram(cfg.bot_token)
    rules = (Path(__file__).parent / "rules.md").read_text(encoding="utf-8")
    t_client, s_client = GeminiClient(cfg.gemini_key), make_select_client(os.environ, cfg.gemini_key)
    offset = None
    deadline = time.time() + BUDGET_SECONDS
    guard = NetGuard()
    while time.time() < deadline:
        try:
            updates = tg.get_updates(offset, timeout=25)
        except TelegramError as e:
            print(f"telegram error: {e}", file=sys.stderr)
            if guard.failed():
                print("telegram unreachable from this runner; ending the run so the next one reconnects",
                      file=sys.stderr)
                return 0
            time.sleep(guard.pause())
            continue
        guard.ok()
        for u in updates:
            with tempfile.TemporaryDirectory() as d:
                deps = Deps(tg, t_client, s_client, Path(d), rules, memory=MEMORY, feedback=FEEDBACK)

                def commit(o):
                    nonlocal offset
                    offset = o
                    try:
                        tg.get_updates(o, timeout=0)  # acknowledge
                    except TelegramError:
                        pass

                offset = handle_update(u, cfg, tg, lambda m, cats: process(m, deps, cats), commit=commit,
                                       feedback_path=FEEDBACK, music_path=MUSIC, pending_path=PENDING,
                                       make=lambda cid, st: studio.make_video(cid, st, tg, t_client, s_client,
                                                                              Path(d), MUSIC),
                                       words=lambda m, t: run_words(m, t, tg, t_client, Path(d), MEMORY.parent))
    return 0


if __name__ == "__main__":
    sys.exit(main())
