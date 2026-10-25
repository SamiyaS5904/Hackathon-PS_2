"""
The hidden truth: how much profit an offer REALLY makes for a customer, using their hidden personality.

Only the harness may use this (it reads `customer_traits`). It reuses the generator's own behaviour formulas
(data/generate.py: order chance from the customer's rhythm, festivals, payday, weekend, a breaking habit, and the
true effect of an opened offer), so it is the referee the agent can never see.

For one customer at one moment:
  p0 = true chance they buy in the next 48h with NO offer
  p1 = true chance with the offer = the offer only works if they open it (open chance rises with deal affinity);
       when opened, it adds their TRUE uplift for that offer type, spread over the 48h window (as in the generator)
  true profit = p1 x (order margin - offer cost) - p0 x order margin       (the same formula as engine/profit.py,
                                                                            but with the true chances)
'reminder' never appeared in past campaigns, so its true effect is taken as half of the customer's true
early-access effect (both are zero-cost nudges); this is a documented assumption of the referee.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from data.calendar_gen import festival_name_on, is_payday, is_weekend
from data.personas import PERSONAS_BY_NAME
from engine.catalog import Catalog
from engine.clock import to_ist
from engine.features import CustomerProfile
from engine.models import OfferProposal
from engine.profit import expected_order, offer_cost_paise


@dataclass
class Traits:
    persona: str
    gap: float
    regularity: float
    deal_affinity: float
    habit_break_date: date | None
    true_uplift: dict


def load_traits(conn: sqlite3.Connection) -> dict[int, Traits]:
    out = {}
    for r in conn.execute("SELECT * FROM customer_traits"):
        out[r["customer_id"]] = Traits(
            r["persona"], r["base_order_gap_days"], r["habit_regularity"], r["deal_affinity"],
            date.fromisoformat(r["habit_break_date"]) if r["habit_break_date"] else None,
            json.loads(r["true_uplift"]))
    return out


def true_daily_order_chance(t: Traits, profile: CustomerProfile, now: datetime) -> float:
    """The generator's order chance for today (data/generate.py CustomerSimulator._order_probability)."""
    spec = PERSONAS_BY_NAME[t.persona]
    days_since = max(profile.days_since_last_order or 400, 0.5)
    x = days_since / t.gap
    p = min(x ** (1 + 4 * t.regularity), 4.0) / t.gap
    today = to_ist(now).date()
    if festival_name_on(today):
        p *= spec.festival_lift * (0.8 + 0.4 * t.deal_affinity)
    elif t.persona == "festival_only":
        p *= 0.15
    if is_payday(today):
        p *= spec.payday_lift
    if is_weekend(today):
        p *= spec.weekend_lift
    if t.habit_break_date and today >= t.habit_break_date:
        p *= 0.15 * math.exp(-(today - t.habit_break_date).days / 20)
    return min(p, 0.95)


def true_chances(t: Traits, profile: CustomerProfile, offer_type: str | None, now: datetime) -> tuple[float, float]:
    """(p0, p1): true chance to buy within 48h without and with this kind of offer."""
    daily = true_daily_order_chance(t, profile, now)
    p0 = 1 - (1 - daily) ** 2
    if offer_type is None:
        return p0, p0
    uplift = t.true_uplift.get(offer_type)
    if uplift is None:  # 'reminder'
        uplift = 0.5 * t.true_uplift["early_access"]
    opened = 0.3 + 0.4 * t.deal_affinity
    p_if_opened = 1 - (1 - min(daily + uplift / 2, 0.95)) ** 2
    return p0, opened * p_if_opened + (1 - opened) * p0


def true_profit_rupees(t: Traits, profile: CustomerProfile, offer: OfferProposal | None, catalog: Catalog,
                       now: datetime) -> float:
    """Expected profit of an offer vs sending nothing, judged with the hidden truth. No offer = 0."""
    if offer is None:
        return 0.0
    value, margin_pct, discount_base = expected_order(offer, profile, catalog)
    margin = value * margin_pct / 100
    cost = offer_cost_paise(offer, value, discount_base)
    p0, p1 = true_chances(t, profile, offer.offer_type, now)
    return (p1 * (margin - cost) - p0 * margin) / 100
