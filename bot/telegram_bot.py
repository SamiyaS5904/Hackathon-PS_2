"""
The OfferPilot Telegram bot: links testers' chats to customer profiles and delivers approved offers.

Where it sits: STEP 6 (delivery). It never decides anything: decisions come from engine/decide.py (AI or fallback,
always checked by the rules). The bot only:
  1. /start <token>  -> links the tester's chat to their customer profile (bot/testers.py checks the username)
  2. every 15 seconds -> sends offers that are due ('queued' now, or 'scheduled' whose time has come)
     to the linked chat, and records message id + sent time (bot/delivery.py)
It uses only TELEGRAM_BOT_TOKEN (no Claude API key), as CLAUDE.md section 3 requires.

Run (keep it running during the demo):
    python -m bot.telegram_bot
"""

from __future__ import annotations

import logging
import sys

from telegram import Bot, Update
from telegram.error import ChatMigrated, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, TypeHandler

from bot.bots import bot_config, bot_token
from bot.delivery import due_deliveries, mark_failed, mark_sent
from bot.manager import group_chat, remember_group
from bot.testers import link_chat
from config.settings import settings
from db.store import connect
from engine.clock import now_utc

DELIVERY_EVERY_SECONDS = 15
log = logging.getLogger("offerpilot.bot")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start <token>: link this chat to the tester's customer profile."""
    token = context.args[0] if context.args else None
    user = update.effective_user
    conn = connect()
    try:
        linked, text = link_chat(conn, token, user.username if user else None, update.effective_chat.id)
    finally:
        conn.close()
    log.info("start from @%s: %s", user.username if user else "?", "linked" if linked else "not linked")
    await update.message.reply_text(text)


async def log_every_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log every incoming message (who and what); if it comes from a group, remember it as the showcase group."""
    user = update.effective_user
    text = update.effective_message.text if update.effective_message else None
    chat = update.effective_chat
    log.info("update from @%s (chat %s): %r", user.username if user else "?", chat.id if chat else "?", text)
    if chat and chat.type in ("group", "supergroup"):
        _note_group(chat.id, chat.title)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any exception in a handler is logged with its cause instead of failing silently."""
    log.error("error while handling an update: %s", context.error, exc_info=context.error)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "This is the OfferPilot demo bot. Personalised offers chosen by our AI arrive here automatically. "
        "To connect, open the personal link the OfferPilot team shared with you.")


_category_bots: dict[str, Bot] = {}


def category_bot(key: str) -> Bot | None:
    """The send-only Bot for a category (config/bots.yaml), or None when its token isn't set."""
    token = bot_token(key)
    if not token:
        return None
    if key not in _category_bots:
        _category_bots[key] = Bot(token)
    return _category_bots[key]


async def deliver(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every few seconds: send every offer that's due through its category bot, then record it as sent."""
    conn = connect()
    try:
        for item in due_deliveries(conn, now_utc()):
            bot, via = category_bot(item["bot"]), item["bot"]
            try:
                if bot is None:
                    raise TelegramError("no token for this category bot")
                sent = await bot.send_message(chat_id=item["chat_id"], text=item["text"])
            except TelegramError as exc:  # no token, or the category bot wasn't started yet -> main bot
                if bot is not None:
                    log.warning("%s bot could not send decision #%s (%s); using the main bot", via,
                                item["decision_id"], exc)
                via = "main"
                try:
                    sent = await context.bot.send_message(chat_id=item["chat_id"], text=item["text"])
                except TelegramError as exc2:  # e.g. the person blocked the bot
                    mark_failed(conn, item["decision_id"], f"Telegram: {exc2}")
                    log.warning("could not send decision #%s: %s", item["decision_id"], exc2)
                    continue
            mark_sent(conn, item["decision_id"], sent.message_id, now_utc(), via)
            log.info("sent decision #%s to customer #%s via %s bot", item["decision_id"], item["customer_id"], via)
            await _post_to_group(context.bot if via == "main" else bot, conn, item["text"])
    finally:
        conn.close()


_offsets: dict[str, int] = {}


def _note_group(chat_id: int, title: str | None) -> None:
    """A bot was added to (or heard from) a group: that group becomes the public showcase group."""
    conn = connect()
    try:
        if remember_group(conn, chat_id):
            log.info("showcase group set: %s (chat %s); every sent offer will also be posted there", title, chat_id)
    finally:
        conn.close()


async def _post_to_group(bot: Bot, conn, text: str) -> None:
    """Post the same offer message in the showcase group, if there is one. A failure never blocks delivery."""
    group = group_chat(conn)
    if group is None:
        return
    try:
        await bot.send_message(chat_id=group, text=text)
    except ChatMigrated as exc:  # the group became a supergroup: new id, try once more
        remember_group(conn, exc.new_chat_id)
        try:
            await bot.send_message(chat_id=exc.new_chat_id, text=text)
        except TelegramError as exc2:
            log.warning("could not post to the group: %s", exc2)
    except TelegramError as exc:
        log.warning("could not post to the group (is this bot a member/admin there?): %s", exc)


async def greet_category_starts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Every few seconds: read new messages sent to the category bots (they have no handlers of their own) and answer
    each one, so pressing Start shows "connected" instead of silence. Also remembers that the bot is ready to message
    that person (meta 'bot_started_<key>'), which the dashboard shows. The Rewards bot is the main bot: its /start
    is handled by start() above.
    """
    conn = None
    for key, cfg in bot_config().items():
        bot = category_bot(key)
        if bot is None or bot_token(key) == settings.telegram_bot_token:
            continue
        try:
            updates = await bot.get_updates(offset=_offsets.get(key), timeout=0)
        except TelegramError as exc:
            log.warning("%s bot: could not read messages: %s", key, exc)
            continue
        for u in updates:
            _offsets[key] = u.update_id + 1
            if u.effective_chat and u.effective_chat.type in ("group", "supergroup"):
                _note_group(u.effective_chat.id, u.effective_chat.title)
                continue
            if not (u.effective_chat and u.effective_user):
                continue
            await bot.send_message(chat_id=u.effective_chat.id,
                                   text=f"✅ {cfg['name']} is connected. Personalised {key} offers will appear here.")
            conn = conn or connect()
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = "
                         "excluded.value", (f"bot_started_{key}", str(u.effective_chat.id)))
            conn.commit()
            log.info("%s bot: message from @%s (chat %s): connected", key, u.effective_user.username,
                     u.effective_chat.id)
    if conn:
        conn.close()


def build_app() -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(TypeHandler(Update, log_every_update), group=-1)  # runs first, for every update
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(deliver, interval=DELIVERY_EVERY_SECONDS, first=3)
    app.job_queue.run_repeating(greet_category_starts, interval=8, first=2)
    return app


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every polling request (URLs contain the token)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)  # the 15-second delivery job is too chatty
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing in .env")
    log.info("OfferPilot bot @%s running: delivering due offers every %ss", settings.telegram_bot_username,
             DELIVERY_EVERY_SECONDS)
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)   # ALL_TYPES includes "added to a group"


if __name__ == "__main__":
    main()
