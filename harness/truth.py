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

12 weeks, not just 48h (simulate_12_weeks): a day-by-day Monte Carlo of the next 84 days with the same formulas,
the offer's effect inside its window, and discount training (data/habits.py): every coupon-discounted order makes
the customer wait a little more for deals, so a coupon can win today's order and cost full-price orders later.
An offer-driven order from a lapsed customer revives their habit with the generator's revival chance, so winning
someone back can pay off for weeks (config/habits.yaml; the engine never sees this number).
It reports the 12-week margin minus offer cost versus sending nothing, and the share of orders bought at full price.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from data.calendar_gen import festival_name_on, is_payday, is_weekend
from data.habits import DealHabit, revival_chance
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
    wait_for_deal: float = 0.0


def load_traits(conn: sqlite3.Connection) -> dict[int, Traits]:
    out = {}
    for r in conn.execute("SELECT * FROM customer_traits"):
        out[r["customer_id"]] = Traits(
            r["persona"], r["base_order_gap_days"], r["habit_regularity"], r["deal_affinity"],
            date.fromisoformat(r["habit_break_date"]) if r["habit_break_date"] else None,
            json.loads(r["true_uplift"]), r["wait_for_deal"] if "wait_for_deal" in r.keys() else 0.0)
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
    p *= DealHabit(t.wait_for_deal).order_factor(festival_name_on(today) is not None)  # coupon-trained habit
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


# ---------------------------------------------------------------------------------------------------
# 12 weeks: long-term value and habit effects
# ---------------------------------------------------------------------------------------------------
CASH_TYPES = {"pct_off", "flat_off", "cashback_to_wallet"}   # offers that are a coupon (train deal-waiting)
HORIZON_DAYS = 84


@dataclass
class LongRun:
    """Average over the simulated paths, for one customer and one offer (or none)."""

    margin_rupees: float          # margin of every order in 12 weeks, minus the offer cost
    orders: float
    full_price_orders: float


def _true_uplift(t: Traits, offer_type: str) -> float:
    uplift = t.true_uplift.get(offer_type)
    return 0.5 * t.true_uplift["early_access"] if uplift is None else uplift


def _one_path(t: Traits, profile: CustomerProfile, offer: OfferProposal | None, usual: tuple[int, float],
              offer_order: tuple[int, float, int] | None, now: datetime, rng: random.Random) -> tuple[float, int, int]:
    spec = PERSONAS_BY_NAME[t.persona]
    habit = DealHabit(t.wait_for_deal)
    days_since = max(profile.days_since_last_order or 400, 0.5)
    opened = rng.random() < 0.3 + 0.4 * t.deal_affinity   # drawn on every path, so with/without stay comparable
    window = max(1, math.ceil(offer.expiry_hours / 24)) if offer else 0
    used = False
    broken_since = t.habit_break_date
    margin = 0.0
    orders = full_price = 0
    start = to_ist(now).date()
    for d in range(HORIZON_DAYS):
        day = start + timedelta(days=d)
        sale = festival_name_on(day) is not None
        x = days_since / t.gap
        p = min(x ** (1 + 4 * t.regularity), 4.0) / t.gap
        if sale:
            p *= spec.festival_lift * (0.8 + 0.4 * t.deal_affinity)
        elif t.persona == "festival_only":
            p *= 0.15
        if is_payday(day):
            p *= spec.payday_lift
        if is_weekend(day):
            p *= spec.weekend_lift
        if broken_since and day >= broken_since:
            p *= 0.15 * math.exp(-(day - broken_since).days / 20)
        p *= habit.order_factor(sale)
        live = offer is not None and opened and not used and d < window
        if live:
            p += _true_uplift(t, offer.offer_type) / 2   # same per-day effect as the generator's 48h campaigns
        if rng.random() < min(p, 0.95):
            orders += 1
            coupon = False
            if live:
                value, margin_pct, base = offer_order
                margin += value * margin_pct / 100 - offer_cost_paise(offer, value, base)
                used, coupon = True, offer.offer_type in CASH_TYPES
                if broken_since and day >= broken_since and rng.random() < revival_chance():
                    broken_since = None  # won back: the habit revives
            else:
                margin += usual[0] * usual[1] / 100
            if coupon:
                habit.coupon_order()
            full_price += not coupon and not sale
            days_since = 0.5
        else:
            days_since += 1
        habit.pass_days(1)
    return margin / 100, orders, full_price


def simulate_12_weeks(t: Traits, profile: CustomerProfile, offer: OfferProposal | None, catalog: Catalog,
                      now: datetime, paths: int = 200, seed: str = "") -> LongRun:
    """Average of `paths` seeded 12-week futures. The same seeds are used with and without an offer
    (common random numbers), so the difference shows the offer's effect, not luck."""
    usual_value, usual_margin, _ = expected_order(OfferProposal(offer_type="reminder"), profile, catalog)
    offer_order = expected_order(offer, profile, catalog) if offer else None
    total = [0.0, 0, 0]
    for k in range(paths):
        rng = random.Random(f"{seed}-{profile.customer_id}-{k}")
        m, o, f = _one_path(t, profile, offer, (usual_value, usual_margin), offer_order, now, rng)
        total[0] += m
        total[1] += o
        total[2] += f
    return LongRun(total[0] / paths, total[1] / paths, total[2] / paths)


def true_value_12_weeks(t: Traits, profile: CustomerProfile, offer: OfferProposal | None, catalog: Catalog,
                        now: datetime, paths: int = 200) -> tuple[float, LongRun, LongRun]:
    """(12-week profit of the offer vs sending nothing, run with the offer, run without)."""
    base = simulate_12_weeks(t, profile, None, catalog, now, paths)
    if offer is None:
        return 0.0, base, base
    run = simulate_12_weeks(t, profile, offer, catalog, now, paths)
    return run.margin_rupees - base.margin_rupees, run, base
