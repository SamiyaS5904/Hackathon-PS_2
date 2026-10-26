"""
Brand voice and message language (config/brand_voice.yaml), shared by the agent and the fallback.

Where it sits: engine/agent.py puts the voice into the AI's instructions; engine/fallback.py picks the Hinglish or
English template. Kept separate so the free fallback never has to import the AI module.
"""

from __future__ import annotations

from functools import lru_cache

import yaml

from config.settings import CONFIG_DIR
from engine.features import CustomerProfile


@lru_cache(maxsize=1)
def brand_voice() -> dict:
    with open(CONFIG_DIR / "brand_voice.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def message_language(profile: CustomerProfile) -> str:
    """'hinglish' for customers whose language is listed in languages.hinglish_for, otherwise the default (English)."""
    langs = brand_voice().get("languages", {})
    return "hinglish" if profile.language in langs.get("hinglish_for", []) else langs.get("default", "english")
