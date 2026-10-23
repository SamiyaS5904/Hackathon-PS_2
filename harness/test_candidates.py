"""
Tests for the pre-priced candidate offers (one-call design, engine/candidates.py).

Proves: the menu is personalised (favourite item, cart item, top category), allowed candidates are sorted by
expected profit, every candidate has been through the rules (blocked ones say why), discounts never exceed the
category cap, and customers who'd buy anyway get no allowed discount candidates.
Run: pytest harness/test_candidates.py -v
"""

from __future__ import annotations

from engine.candidates import build_candidates
from engine.rules import RuleState
from harness.conftest import make_profile

FAV = [{"product_id": 2, "name": "Urban Thread Kurta Classic", "category": "fashion", "price_rupees": 1500,
        "times_ordered": 6, "last_bought": "2026-08-01"}]
CART = {"left_hours_ago": 5, "cart_value_rupees": 1800,
        "items": [{"product_id": 5, "name": "Casa Nova Table Lamp Max", "category": "home", "price_rupees": 1800}]}


def test_menu_is_personalised(catalog, rules, state, now):
    profile = make_profile(favourite_items=FAV, open_cart=CART, wallet_balance_rupees=500)
    ideas = " | ".join(c["idea"] for c in build_candidates(profile, catalog, rules, state, now))
    assert "Kurta" in ideas or "favourite item" in ideas
    assert "Table Lamp" in ideas or "cart item" in ideas


def test_allowed_candidates_sorted_by_profit(catalog, rules, state, now):
    menu = build_candidates(make_profile(favourite_items=FAV, deal_seeker_score=0.6), catalog, rules, state, now)
    allowed = [c["expected_profit_rupees"] for c in menu if c["allowed_by_rules"]]
    assert allowed == sorted(allowed, reverse=True) and allowed
    assert all(p >= rules.profit["min_expected_profit_rupees"] for p in allowed)


def test_blocked_candidates_explain_why(catalog, rules, now):
    menu = build_candidates(make_profile(favourite_items=FAV), catalog, rules, RuleState(offers_last_7d=2), now)
    assert not any(c["allowed_by_rules"] for c in menu)
    assert all(c["blocked_because"] for c in menu)


def test_discounts_never_exceed_category_cap(catalog, rules, state, now):
    profile = make_profile(favourite_categories=["electronics"], category_spend_share={"electronics": 1.0},
                           deal_seeker_score=0.9)
    for c in build_candidates(profile, catalog, rules, state, now):
        if c["allowed_by_rules"] and c["offer"]["offer_type"] == "pct_off":
            assert c["offer"]["amount"] <= rules.profit["category_cap_pct"]["electronics"]


def test_buys_anyway_customer_gets_no_allowed_discounts(catalog, rules, state, now):
    menu = build_candidates(make_profile(p_buy_48h_no_offer=0.85), catalog, rules, state, now)
    allowed_types = {c["offer"]["offer_type"] for c in menu if c["allowed_by_rules"]}
    assert not allowed_types & {"pct_off", "flat_off", "cashback_to_wallet", "free_delivery"}
