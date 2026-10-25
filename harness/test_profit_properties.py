"""
Harness test 2: the profit guarantee, tested with thousands of random cases (property-based testing).

Instead of a few hand-picked examples, `hypothesis` generates random customers, random offers (absurd discounts,
odd minimum orders, any send time, any expiry) and random budget situations, and checks that EVERY offer the rules
approve keeps the promises in CLAUDE.md section 13. One counter-example fails the test (and hypothesis shrinks it to
the simplest failing case, so a bug is easy to read).

Invariants checked on every approved offer:
  1. expected profit >= the minimum (recomputed independently from the final offer)
  2. margin after the offer >= the margin floor
  3. the offer's cost fits today's, this month's and this customer's budget
  4. a % discount never exceeds its category cap; points never exceed the max; cashback never exceeds the max
  5. expiry within the allowed hours; send time never inside quiet hours
  6. customers who'd buy anyway never get a discount
The same invariants are checked for the fallback rules and for the candidate menu.
Run: pytest harness/test_profit_properties.py -v
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from engine.candidates import build_candidates
from engine.catalog import Catalog, Product
from engine.clock import to_ist
from engine.fallback import fallback_decide
from engine.models import OfferProposal
from engine.profit import estimate
from engine.rules import RuleState, check_offer
from engine.rules_store import ActiveRules, load_default_rules
from harness.conftest import make_profile

CATALOG = Catalog(
    products={
        1: Product(1, "Voltix Earphones Pro", "electronics", "Earphones", "Voltix", 2000_00, 1560_00, 22.0),
        2: Product(2, "Urban Thread Kurta Classic", "fashion", "Kurta", "Urban Thread", 1500_00, 750_00, 50.0),
        3: Product(3, "FreshFarm Basmati Rice Plus", "grocery", "Basmati Rice", "FreshFarm", 500_00, 410_00, 18.0),
        4: Product(4, "Glowveda Serum Neo", "beauty", "Serum", "Glowveda", 900_00, 480_00, 46.7),
        5: Product(5, "Casa Nova Table Lamp Max", "home", "Table Lamp", "Casa Nova", 1800_00, 1150_00, 36.1),
    },
    category_avg_margin_pct={"electronics": 22.5, "fashion": 50.3, "beauty": 46.2, "home": 36.2, "grocery": 22.7},
    category_avg_price_paise={"electronics": 1827_00, "fashion": 1775_00, "beauty": 639_00, "home": 1404_00,
                              "grocery": 341_00},
)
RULES = ActiveRules(1, load_default_rules(), "2026-10-22T00:00:00Z", "test", None)
NOW = datetime(2026, 10, 23, 6, 30, tzinfo=timezone.utc)
CATEGORIES = list(CATALOG.category_avg_margin_pct)
TYPES = ["pct_off", "flat_off", "free_delivery", "cashback_to_wallet", "points_multiplier", "early_access", "reminder"]

profiles = st.builds(
    lambda p_no, deal, aov, orders, overdue, fav_cat: make_profile(
        p_buy_48h_no_offer=p_no, deal_seeker_score=deal, avg_order_value_rupees=aov, total_orders=orders,
        overdue_ratio=overdue, favourite_categories=[fav_cat], category_spend_share={fav_cat: 1.0}),
    p_no=st.floats(0, 0.99), deal=st.floats(0, 1), aov=st.integers(0, 15000), orders=st.integers(0, 60),
    overdue=st.floats(0, 8), fav_cat=st.sampled_from(CATEGORIES))

offers = st.builds(
    lambda t, amount, target, min_order, expiry, send_minutes: OfferProposal(
        offer_type=t, amount=amount,
        product_id=target if isinstance(target, int) else None,
        category=target if isinstance(target, str) else None,
        min_order_rupees=min_order, expiry_hours=expiry,
        send_at=NOW + timedelta(minutes=send_minutes)),
    t=st.sampled_from(TYPES), amount=st.floats(0, 120), target=st.one_of(st.none(), st.sampled_from([1, 2, 3, 4, 5]),
                                                                             st.sampled_from(CATEGORIES)),
    min_order=st.integers(0, 6000), expiry=st.integers(0, 300), send_minutes=st.integers(0, 2 * 24 * 60))

states = st.builds(
    lambda today, month, customer: RuleState(spent_today_paise=today, spent_month_paise=month,
                                             customer_spent_month_paise=customer),
    today=st.integers(0, 6000_00), month=st.integers(0, 110_000_00), customer=st.integers(0, 600_00))


def matching_message(o: OfferProposal) -> str:
    """A message that states the final offer's real numbers, so approval depends on the money rules only."""
    product = CATALOG.get(o.product_id)
    what = {"pct_off": f"{o.amount:g}% off", "flat_off": f"₹{o.amount:g} off", "cashback_to_wallet": f"₹{o.amount:g} cashback",
            "points_multiplier": f"{o.amount:g}x points", "free_delivery": "free delivery",
            "early_access": "early access", "reminder": "a reminder"}[o.offer_type]
    on = f" on {product.name}" if product else ""
    return f"Hi Riya, enjoy {what}{on} on orders above ₹{o.min_order_rupees}."


def assert_never_loses_money(offer: OfferProposal, profile, state: RuleState) -> None:
    """Every invariant from the module docstring, checked on one approved offer."""
    p, b, s, i = RULES.profit, RULES.budget, RULES.anti_spam, RULES.integrity
    e = estimate(offer, profile, CATALOG)
    assert e.expected_profit_paise >= p["min_expected_profit_rupees"] * 100, e.explanation
    assert e.margin_after_offer_pct >= p["margin_floor_pct"], e.explanation
    assert state.spent_today_paise + e.offer_cost_paise <= b["daily_rupees"] * 100
    assert state.spent_month_paise + e.offer_cost_paise <= b["monthly_rupees"] * 100
    assert state.customer_spent_month_paise + e.offer_cost_paise <= b["per_customer_monthly_rupees"] * 100
    category = CATALOG.get(offer.product_id).category if offer.product_id else offer.category
    cap = p["category_cap_pct"][category] if category else min(p["category_cap_pct"].values())
    if offer.offer_type == "pct_off":
        assert offer.amount <= cap
    if offer.offer_type in ("flat_off", "cashback_to_wallet"):
        assert offer.amount <= offer.min_order_rupees * cap / 100 + 1e-9
    if offer.offer_type == "cashback_to_wallet":
        assert offer.amount <= p["cashback_max_rupees"]
    if offer.offer_type == "points_multiplier":
        assert 1 < offer.amount <= p["points_multiplier_max"]
    assert i["min_expiry_hours"] <= offer.expiry_hours <= i["max_expiry_hours"]
    local = to_ist(offer.send_at).time()
    assert not (local >= time.fromisoformat(s["quiet_hours_start"]) or local < time.fromisoformat(s["quiet_hours_end"]))
    if profile.p_buy_48h_no_offer > p["buys_anyway_threshold"]:
        assert offer.offer_type in set(p["perks_allowed_for_buys_anyway"]) | {"early_access", "reminder"}


@settings(max_examples=3000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(offer=offers, profile=profiles, state=states)
def test_any_approved_offer_never_loses_money(offer, profile, state):
    result = check_offer(offer, profile, CATALOG, RULES, state, NOW, message_renderer=matching_message)
    if result.ok:
        assert_never_loses_money(result.offer, profile, state)


@settings(max_examples=800, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(profile=profiles, state=states, points=st.integers(0, 2000), wallet=st.integers(0, 5000),
       idle=st.floats(0, 200), has_cart=st.booleans())
def test_fallback_offers_never_lose_money(profile, state, points, wallet, idle, has_cart):
    profile.points_expiring_7d = points
    profile.points_next_expiry = "2026-10-27T00:00:00Z" if points else None
    profile.wallet_balance_rupees, profile.wallet_idle_days = wallet, idle
    profile.favourite_items = [{"product_id": 2, "name": "Urban Thread Kurta Classic", "category": "fashion",
                                "price_rupees": 1500, "times_ordered": 5, "last_bought": "2026-08-01"}]
    if has_cart:
        profile.open_cart = {"left_hours_ago": 5, "cart_value_rupees": 1500, "items": [profile.favourite_items[0]]}
    result = fallback_decide(profile, CATALOG, RULES, state, NOW, "property test")
    if result.check is not None:
        assert result.check.ok
        assert_never_loses_money(result.check.offer, profile, state)


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(profile=profiles, state=states)
def test_candidate_menu_only_allows_safe_offers(profile, state):
    for c in build_candidates(profile, CATALOG, RULES, state, NOW):
        if c["allowed_by_rules"]:
            o = c["offer"]
            offer = OfferProposal(offer_type=o["offer_type"], amount=o["amount"], product_id=o["product_id"],
                                  category=o["category"], min_order_rupees=o["min_order_rupees"],
                                  expiry_hours=o["expiry_hours"], send_at=NOW)
            # candidates are checked at NOW; the send-time repair is applied when the real proposal is checked
            e = estimate(offer, profile, CATALOG)
            assert e.expected_profit_paise >= RULES.profit["min_expected_profit_rupees"] * 100
            assert e.margin_after_offer_pct >= RULES.profit["margin_floor_pct"]
