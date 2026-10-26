"""
Discount training: the hidden habit cost of coupons, shared by the data generator and the hidden-truth referee.

Where it sits: used by data/generate.py (12 months of history) and harness/truth.py (12 weeks after an offer).
The engine never imports this: like a real shop, it has to see the effect in the data (full-price share).

The idea in one line: every coupon-discounted order teaches a customer to wait for the next deal.
  wait_for_deal (0..max) goes up by `step` after each coupon-discounted order and halves every `half_life_days`
  without one. On a normal day the order chance is multiplied by (1 - wait_for_deal); on a sale day by
  (1 + wait_for_deal): the same customer still buys, but more of their buying moves to discounts.
Revival: an offer-driven order from a customer whose habit has broken brings the habit back with a fixed chance
(revival_chance()). Constants live in config/habits.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import yaml

from config.settings import PROJECT_ROOT


@lru_cache(maxsize=1)
def _config() -> dict:
    return yaml.safe_load((PROJECT_ROOT / "config" / "habits.yaml").read_text(encoding="utf-8"))


def training_config() -> dict:
    return _config()["discount_training"]


def revival_chance() -> float:
    """Chance that an offer-driven order revives a broken habit."""
    return _config()["revival"]["chance_after_offer_order"]


@dataclass
class DealHabit:
    """One customer's learned habit of waiting for deals."""

    wait_for_deal: float = 0.0

    def pass_days(self, days: float = 1.0) -> None:
        """Time without a coupon slowly undoes the training."""
        self.wait_for_deal *= 0.5 ** (days / training_config()["half_life_days"])

    def coupon_order(self) -> None:
        """A coupon-discounted order: a little more trained to wait for the next one."""
        cfg = training_config()
        self.wait_for_deal = min(cfg["max_wait_for_deal"], self.wait_for_deal + cfg["step_per_discounted_order"])

    def order_factor(self, sale_day: bool) -> float:
        """Multiplier on today's order chance."""
        return 1 + self.wait_for_deal if sale_day else 1 - self.wait_for_deal
