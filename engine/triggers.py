"""
Triggers: the moments that deserve an offer decision, spotted from a customer's behaviour.

Where it sits: STEP 3 of the pipeline. events/simulator.py (Session 4) creates live events and calls decide()
with these trigger types; `python -m engine.decide --customer N --auto-trigger` uses detect_triggers() to pick the
strongest moment for one customer right now.

Each trigger comes with details (which product, how long ago) that go into the AI's first message, so the offer
can talk about exactly that moment ("Still thinking about the Mira lipstick?"), plus a suggested delay: offers
work best a little AFTER the moment, not instantly (instant offers feel like being watched, and some people buy
on their own within the hour).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.features import CustomerProfile


@dataclass
class Trigger:
    type: str
    strength: float                 # higher = more worth acting on (used to pick and to order)
    detail: dict = field(default_factory=dict)
    suggested_delay_hours: float = 0.0

    def describe(self) -> str:
        return f"{self.type} ({', '.join(f'{k}: {v}' for k, v in self.detail.items())})"


# What each trigger means, for docs, the dashboard and the simulator
TRIGGER_TYPES = {
    "cart_abandoned": "Left items in the cart without ordering",
    "product_interest": "Searched for, viewed repeatedly or wishlisted a product",
    "habit_break": "Overdue compared to their usual gap between orders",
    "points_expiring": "Reward points expire within 7 days",
    "wallet_idle": "Wallet money unused for 30+ days",
    "followup": "The AI waited earlier; time to look again",
    "manual": "The marketing manager asked for a decision",
}


def detect_triggers(profile: CustomerProfile) -> list[Trigger]:
    """All triggers that apply to this customer right now, strongest first."""
    found: list[Trigger] = []

    if profile.open_cart and profile.open_cart["items"]:
        cart = profile.open_cart
        found.append(Trigger("cart_abandoned", 80 + min(cart["cart_value_rupees"] / 100, 20),
                             {"product": cart["items"][0]["name"], "cart_value_rupees": cart["cart_value_rupees"],
                              "left_hours_ago": cart["left_hours_ago"]},
                             suggested_delay_hours=0 if cart["left_hours_ago"] >= 2 else 2 - cart["left_hours_ago"]))

    interest = [i for i in profile.intent_products
                if not (profile.open_cart and any(c["product_id"] == i["product_id"]
                                                  for c in profile.open_cart["items"]))]
    if interest:
        top = interest[0]
        found.append(Trigger("product_interest", 60 + 5 * len(top["signals"]),
                             {"product": top["name"], "signals": top["signals"]}, suggested_delay_hours=3))

    if (profile.overdue_ratio or 0) >= 1.5 and profile.total_orders >= 3:
        found.append(Trigger("habit_break", 70 + min(profile.total_spend_rupees / 2000, 25),
                             {"usual_gap_days": profile.usual_gap_days,
                              "days_since_last_order": profile.days_since_last_order}))

    if profile.points_expiring_7d:
        found.append(Trigger("points_expiring", 40, {"points": profile.points_expiring_7d,
                                                     "expires": profile.points_next_expiry}))

    if profile.wallet_balance_rupees >= 200 and (profile.wallet_idle_days or 0) >= 30:
        found.append(Trigger("wallet_idle", 30, {"wallet_balance_rupees": profile.wallet_balance_rupees,
                                                 "idle_days": profile.wallet_idle_days}))
    return sorted(found, key=lambda t: -t.strength)
