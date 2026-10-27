"""
Settings loader: reads secrets and switches from the `.env` file.

Where it sits: used by every part of the pipeline that needs a setting (database path, API key, bot
token, model name, daily AI limit). Nothing else should read environment variables directly; import
`settings` from here instead, so there is one place to look.

Key idea: the system must still run with NO Claude API key. In that case `settings.agent_enabled`
is False and every decision goes to the free fallback rules (see CLAUDE.md section 3).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Project root = the folder that contains CLAUDE.md. All relative paths are resolved from here,
# so commands work no matter which folder they are started from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    """All runtime settings in one object. Frozen so nothing changes them by accident."""

    anthropic_api_key: str
    telegram_bot_token: str
    telegram_bot_username: str  # public, used for tester links t.me/<username>?start=<token>
    claude_model: str
    max_agent_runs_per_day: int  # hard ceiling; the dashboard Day Plan can only go lower
    db_path: Path
    telegram_manager_username: str = ""   # manager mode: every offer goes to this Telegram user (bot/manager.py)
    api_url: str = "http://127.0.0.1:8010"  # the dashboard API, which the bot asks to run a decision
    telegram_group_chat_id: str = ""        # public showcase group (bot/manager.py group_chat); empty = auto-detect
    dashboard_password: str = ""            # set it whenever the dashboard is reachable from the internet

    @property
    def agent_enabled(self) -> bool:
        """True only when an API key is present. Without it the fallback handles everything."""
        return bool(self.anthropic_api_key)


def load_settings() -> Settings:
    """Build Settings from environment variables (filled from .env), with safe defaults."""
    db_path = Path(os.getenv("DB_PATH", "data/offerpilot.db"))
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    return Settings(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_bot_username=os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@"),
        claude_model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5").strip(),
        max_agent_runs_per_day=int(os.getenv("MAX_AGENT_RUNS_PER_DAY", "50")),
        db_path=db_path,
        telegram_manager_username=os.getenv("TELEGRAM_MANAGER_USERNAME", "").strip().lstrip("@"),
        api_url=os.getenv("OFFERPILOT_API_URL", "http://127.0.0.1:8010").strip().rstrip("/"),
        telegram_group_chat_id=os.getenv("TELEGRAM_GROUP_CHAT_ID", "").strip(),
        dashboard_password=os.getenv("DASHBOARD_PASSWORD", "").strip(),
    )


settings = load_settings()
