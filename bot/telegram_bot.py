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

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

from bot.delivery import due_deliveries, mark_failed, mark_sent
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


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "This is the OfferPilot demo bot. Personalised offers chosen by our AI arrive here automatically. "
        "To connect, open the personal link the OfferPilot team shared with you.")


async def deliver(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every few seconds: send every offer that's due, then record it as sent."""
    conn = connect()
    try:
        for item in due_deliveries(conn, now_utc()):
            try:
                sent = await context.bot.send_message(chat_id=item["chat_id"], text=item["text"])
                mark_sent(conn, item["decision_id"], sent.message_id, now_utc())
                log.info("sent decision #%s to customer #%s", item["decision_id"], item["customer_id"])
            except TelegramError as exc:  # e.g. the tester blocked the bot
                mark_failed(conn, item["decision_id"], f"Telegram: {exc}")
                log.warning("could not send decision #%s: %s", item["decision_id"], exc)
    finally:
        conn.close()


def build_app() -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.job_queue.run_repeating(deliver, interval=DELIVERY_EVERY_SECONDS, first=3)
    return app


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every polling request (URLs contain the token)
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing in .env")
    log.info("OfferPilot bot @%s running: delivering due offers every %ss", settings.telegram_bot_username,
             DELIVERY_EVERY_SECONDS)
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
