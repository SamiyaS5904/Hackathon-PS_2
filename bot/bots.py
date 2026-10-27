"""
Category bots: which Telegram bot sends an offer (config/bots.yaml).

Where it sits: bot/delivery.py tags each due offer with its bot key; bot/telegram_bot.py sends it with that bot.
Only the main bot (TELEGRAM_BOT_TOKEN) listens for /start; the category bots only SEND, so they need no polling.
In a private chat the chat id is the person's Telegram id for every bot, so once the manager has pressed Start in a
category bot, that bot can message the same chat. A bot without a token (or not started yet) falls back to the main
bot, so an offer is never lost.
"""

from __future__ import annotations

import os
from functools import lru_cache

import yaml

from config.settings import CONFIG_DIR


@lru_cache(maxsize=1)
def bot_config() -> dict:
    return yaml.safe_load((CONFIG_DIR / "bots.yaml").read_text(encoding="utf-8"))["bots"]


def bot_key_for(category: str | None) -> str:
    """Offer category -> bot key. Offers without a category (points, wallet, whole-order) go to Rewards."""
    return category if category in bot_config() else "rewards"


def bot_token(key: str) -> str:
    return os.getenv(bot_config()[key]["token_env"], "").strip()


def bots_status() -> list[dict]:
    """For the dashboard: each bot's name and whether its own token is set (otherwise the main bot is used)."""
    return [{"key": k, "name": v["name"], "connected": bool(bot_token(k)), "username": v.get("username"),
             "link": f"https://t.me/{v['username']}" if v.get("username") else None} for k, v in bot_config().items()]
