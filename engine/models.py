"""
Shared data shapes used across the engine: an offer proposal, one rule check, and the checker's answer.

Where it sits: the agent (Session 3) and the fallback both produce an `OfferProposal`; rules.py checks it and
returns a `CheckResult`; decide.py stores everything in the `decisions` table.

Key idea: pydantic validates every proposal. If the AI returns something malformed (negative amount, expiry of
500 hours, unknown fields), it fails validation here instead of slipping through.
Money in proposals is in RUPEES (easy for the agent and humans to read); storage converts to paise.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OfferType = Literal["pct_off", "flat_off", "free_delivery", "cashback_to_wallet",
                    "points_multiplier", "early_access", "reminder"]


class OfferProposal(BaseModel):
    """One offer built from the building blocks in config/offers.yaml."""

    model_config = ConfigDict(extra="forbid")

    offer_type: OfferType
    amount: float = Field(default=0, ge=0, description="% for pct_off, rupees for flat_off/cashback, x for points")
    product_id: int | None = None
    category: str | None = None
    min_order_rupees: int = Field(default=0, ge=0)
    expiry_hours: int = Field(default=24, ge=0, le=720)
    send_at: datetime | None = None  # UTC; None = send now
    message: str = ""

    def describe(self) -> str:
        """Short human description, e.g. '15% off on fashion (min order Rs 999)'."""
        target = f"product #{self.product_id}" if self.product_id else (self.category or "any order")
        amount = {
            "pct_off": f"{self.amount:g}% off on {target}",
            "flat_off": f"Rs {self.amount:g} off on {target}",
            "cashback_to_wallet": f"Rs {self.amount:g} wallet cashback on {target}",
            "points_multiplier": f"{self.amount:g}x reward points",
            "free_delivery": "free delivery",
            "early_access": f"early access to {target}",
            "reminder": "reminder message (no discount)",
        }[self.offer_type]
        if self.min_order_rupees:
            amount += f" (min order Rs {self.min_order_rupees})"
        return amount


RuleResult = Literal["passed", "repaired", "failed"]


class RuleCheck(BaseModel):
    """One line of the checker's report, shown on the dashboard: rule name, outcome, explanation."""

    rule: str
    result: RuleResult
    detail: str


class ProfitEstimate(BaseModel):
    """The numbers behind 'is this offer worth it?' (all money in paise)."""

    p_buy_no_offer: float
    p_buy_with_offer: float
    expected_order_value_paise: int
    normal_margin_paise: int
    offer_cost_paise: int
    margin_after_offer_paise: int
    margin_after_offer_pct: float
    expected_profit_paise: int
    explanation: str
    # v2: what a won-back customer is worth over the coming weeks (only for late customers; engine/profit.py)
    retention_value_paise: int = 0
    total_value_paise: int = 0          # expected_profit + retention_value
    is_late_customer: bool = False      # the retention term applies (overdue and enough past orders)


class CheckResult(BaseModel):
    """The checker's answer: approved / repaired (approved after safe fixes) / rejected."""

    status: Literal["approved", "repaired", "rejected"]
    offer: OfferProposal | None  # the final offer (after repairs); None when rejected
    checks: list[RuleCheck]
    profit: ProfitEstimate | None = None
    rules_version: int
    offer_kind: Literal["normal", "winback"] = "normal"   # win-back = relies on retention / habit exception

    @property
    def ok(self) -> bool:
        return self.status in ("approved", "repaired")

    @property
    def failures(self) -> list[str]:
        """Plain-language reasons for a rejection; these are fed back to the agent so it can try again."""
        return [f"{c.rule}: {c.detail}" for c in self.checks if c.result == "failed"]
