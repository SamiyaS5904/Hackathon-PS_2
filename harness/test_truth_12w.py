"""
Tests for the 12-week hidden-truth referee (harness/truth.py simulate_12_weeks) and discount training
(data/habits.py). The referee judges every approach, so it must itself behave as documented.

Proves: sending nothing changes nothing; the simulation is repeatable (seeded); coupons train a customer to wait
for deals (lower full-price share than a non-cash offer with the same pull); an offer can win back a lapsed
customer for longer than one order (habit revival); and the habit model's maths (step, cap, half-life).
Run: pytest harness/test_truth_12w.py -v
"""

from __future__ import annotations

from datetime import timedelta

from data.habits import DealHabit, training_config
from harness.conftest import NOON_IST, make_profile, offer
from harness.truth import Traits, simulate_12_weeks, true_value_12_weeks

STRONG_PULL = {t: 0.6 for t in ("pct_off", "flat_off", "cashback_to_wallet", "free_delivery", "points_multiplier",
                                "early_access")}


def traits(**overrides) -> Traits:
    values = dict(persona="loyal_regular", gap=15.0, regularity=0.5, deal_affinity=1.0, habit_break_date=None,
                  true_uplift=STRONG_PULL, wait_for_deal=0.0)
    values.update(overrides)
    return Traits(**values)


def test_no_offer_is_worth_exactly_nothing(catalog):
    value, run, base = true_value_12_weeks(traits(), make_profile(), None, catalog, NOON_IST, paths=50)
    assert value == 0.0 and run == base


def test_simulation_is_repeatable(catalog):
    o = offer(offer_type="pct_off", amount=10, category="fashion", min_order_rupees=499, expiry_hours=48)
    a = simulate_12_weeks(traits(), make_profile(), o, catalog, NOON_IST, paths=50)
    b = simulate_12_weeks(traits(), make_profile(), o, catalog, NOON_IST, paths=50)
    assert a == b


def test_coupons_lower_full_price_share_more_than_a_non_cash_offer(catalog):
    profile = make_profile(days_since_last_order=15.0)
    coupon = simulate_12_weeks(traits(), profile, offer(offer_type="pct_off", amount=10, category="fashion",
                                                        min_order_rupees=499, expiry_hours=48), catalog, NOON_IST)
    perk = simulate_12_weeks(traits(), profile, offer(offer_type="early_access", category="fashion",
                                                      expiry_hours=48), catalog, NOON_IST)
    assert coupon.full_price_orders / coupon.orders < perk.full_price_orders / perk.orders


def test_win_back_pays_off_beyond_one_order_only_through_revival(catalog, monkeypatch):
    """A lapsed customer: the same offer is worth more over 12 weeks when a won-back habit can revive."""
    import harness.truth as truth
    lapsed = traits(habit_break_date=(NOON_IST - timedelta(days=30)).date())
    profile = make_profile(days_since_last_order=60.0)
    o = offer(offer_type="pct_off", amount=10, category="fashion", min_order_rupees=499, expiry_hours=48)
    with_revival, run, base = true_value_12_weeks(lapsed, profile, o, catalog, NOON_IST, paths=300)
    monkeypatch.setattr(truth, "revival_chance", lambda: 0.0)
    no_revival, run0, _ = true_value_12_weeks(lapsed, profile, o, catalog, NOON_IST, paths=300)
    assert run0.orders - base.orders <= 1.0           # without revival: at most the one offer order
    assert run.orders > run0.orders and with_revival > no_revival


def test_habit_model_step_cap_and_half_life():
    cfg = training_config()
    h = DealHabit()
    h.coupon_order()
    assert h.wait_for_deal == cfg["step_per_discounted_order"]
    for _ in range(100):
        h.coupon_order()
    assert h.wait_for_deal == cfg["max_wait_for_deal"]
    h.pass_days(cfg["half_life_days"])
    assert abs(h.wait_for_deal - cfg["max_wait_for_deal"] / 2) < 1e-9
    assert h.order_factor(sale_day=False) < 1 < h.order_factor(sale_day=True)
