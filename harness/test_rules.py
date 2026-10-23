"""
Harness test 1: every rule in CLAUDE.md section 4 has pass / fail / edge-case tests.

Each test builds one clear situation (a profile, an offer, what has been sent/spent) and checks the rules
checker answers correctly: approved, repaired (with the right fix), or rejected (with the right rule named).
Run: pytest harness/test_rules.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone

from engine.clock import to_ist
from engine.rules import RuleState, check_offer
from harness.conftest import make_profile, offer

GOOD_MESSAGE = "Hi Riya! Get 15% off on fashion above ₹999."


def run(o, profile, catalog, rules, state, now, **kw):
    return check_offer(o, profile, catalog, rules, state, now, **kw)


def result_of(res, rule: str) -> str | None:
    """The outcome ('passed' / 'repaired' / 'failed') of one rule in a check result."""
    for c in res.checks:
        if c.rule == rule:
            return c.result
    return None


def good_offer(**changes):
    fields = dict(offer_type="pct_off", amount=15, category="fashion", min_order_rupees=999, expiry_hours=24,
                  message=GOOD_MESSAGE)
    fields.update(changes)
    return offer(**fields)


# --- a normal good offer -------------------------------------------------------------------------
def test_good_offer_is_approved(profile, catalog, rules, state, now):
    res = run(good_offer(), profile, catalog, rules, state, now)
    assert res.status == "approved", res.failures
    assert res.profit.expected_profit_paise >= rules.profit["min_expected_profit_rupees"] * 100
    assert res.rules_version == 1


# --- 4.4 integrity -------------------------------------------------------------------------------
def test_unknown_product_rejected(profile, catalog, rules, state, now):
    res = run(good_offer(product_id=999, category=None), profile, catalog, rules, state, now)
    assert res.status == "rejected" and result_of(res, "integrity.product") == "failed"


def test_unknown_category_rejected(profile, catalog, rules, state, now):
    res = run(good_offer(category="toys"), profile, catalog, rules, state, now)
    assert res.status == "rejected" and result_of(res, "integrity.category") == "failed"


def test_discount_without_target_rejected(profile, catalog, rules, state, now):
    res = run(good_offer(category=None), profile, catalog, rules, state, now)
    assert result_of(res, "integrity.target") == "failed"


def test_zero_amount_rejected(profile, catalog, rules, state, now):
    res = run(good_offer(amount=0), profile, catalog, rules, state, now)
    assert result_of(res, "integrity.amount") == "failed"


def test_product_offer_takes_product_category(profile, catalog, rules, state, now):
    o = good_offer(product_id=2, category="electronics", amount=10,
                   message="Hi Riya! Get 10% off your Kurta above ₹999.")
    res = run(o, profile, catalog, rules, state, now)
    assert res.ok, res.failures
    assert res.offer.category == "fashion"


def test_expiry_too_long_repaired(profile, catalog, rules, state, now):
    res = run(good_offer(expiry_hours=100), profile, catalog, rules, state, now)
    assert result_of(res, "integrity.expiry") == "repaired" and res.offer.expiry_hours == 48


def test_expiry_too_short_repaired(profile, catalog, rules, state, now):
    res = run(good_offer(expiry_hours=1), profile, catalog, rules, state, now)
    assert res.offer.expiry_hours == 2


def test_stacking_rejected(profile, catalog, rules, now):
    res = run(good_offer(), profile, catalog, rules, RuleState(active_offers=1), now)
    assert result_of(res, "integrity.no_stacking") == "failed"


# --- 4.3 anti-spam -------------------------------------------------------------------------------
def test_no_consent_rejected(catalog, rules, state, now):
    res = run(good_offer(), make_profile(marketing_consent=False), catalog, rules, state, now)
    assert result_of(res, "anti_spam.consent") == "failed"


def test_weekly_limit_rejected(profile, catalog, rules, now):
    res = run(good_offer(), profile, catalog, rules, RuleState(offers_last_7d=2), now)
    assert result_of(res, "anti_spam.max_per_week") == "failed"


def test_one_offer_this_week_is_fine(profile, catalog, rules, now):
    res = run(good_offer(), profile, catalog, rules, RuleState(offers_last_7d=1, hours_since_last_offer=30), now)
    assert result_of(res, "anti_spam.max_per_week") == "passed"


def test_gap_too_short_rejected(profile, catalog, rules, now):
    res = run(good_offer(), profile, catalog, rules, RuleState(hours_since_last_offer=5), now)
    assert result_of(res, "anti_spam.gap") == "failed"


def test_gap_exactly_24h_is_fine(profile, catalog, rules, now):
    res = run(good_offer(), profile, catalog, rules, RuleState(hours_since_last_offer=24), now)
    assert result_of(res, "anti_spam.gap") == "passed"


def test_quiet_hours_late_night_moved_to_8am_next_day(profile, catalog, rules, state, now):
    late = datetime(2026, 10, 23, 17, 30, tzinfo=timezone.utc)  # 23:00 IST
    res = run(good_offer(send_at=late), profile, catalog, rules, state, now)
    assert result_of(res, "anti_spam.quiet_hours") == "repaired"
    local = to_ist(res.offer.send_at)
    assert (local.day, local.hour, local.minute) == (24, 8, 0)


def test_quiet_hours_early_morning_moved_to_8am_same_day(profile, catalog, rules, state, now):
    early = datetime(2026, 10, 23, 1, 30, tzinfo=timezone.utc)  # 07:00 IST
    res = run(good_offer(send_at=early), profile, catalog, rules, state, now)
    local = to_ist(res.offer.send_at)
    assert (local.day, local.hour) == (23, 8)


def test_daytime_send_not_moved(profile, catalog, rules, state, now):
    res = run(good_offer(), profile, catalog, rules, state, now)
    assert result_of(res, "anti_spam.quiet_hours") == "passed"
    assert res.offer.send_at == now


# --- 4.1 profit protection -----------------------------------------------------------------------
def test_category_cap_clamps_discount(profile, catalog, rules, state, now):
    res = run(good_offer(amount=25, category="electronics"), profile, catalog, rules, state, now)
    assert result_of(res, "profit.category_cap") == "repaired"
    assert res.status == "rejected"  # agent's text still says 25% -> must be rewritten
    assert result_of(res, "message.matches_offer") == "failed"


def test_category_cap_with_renderer_is_repaired_and_sent(profile, catalog, rules, state, now):
    o = good_offer(amount=25, category="electronics", min_order_rupees=1500)
    res = run(o, profile, catalog, rules, state, now,
              message_renderer=lambda x: f"Hi Riya! Get {x.amount:g}% off on electronics above ₹{x.min_order_rupees}.")
    # The cap fix is applied and the re-rendered message matches it (profit may still decide separately)
    assert result_of(res, "profit.category_cap") == "repaired"
    assert result_of(res, "message.matches_offer") == "passed"
    assert any("8% off" in c.detail or "lowered to 8%" in c.detail for c in res.checks)


def test_discount_at_cap_passes(profile, catalog, rules, state, now):
    res = run(good_offer(amount=30, message="Hi Riya! Get 30% off on fashion above ₹999, valid for 24 hours."),
              profile, catalog, rules, state, now)
    assert result_of(res, "profit.category_cap") == "passed"


def test_flat_off_without_min_order_gets_one(profile, catalog, rules, state, now):
    o = offer(offer_type="flat_off", amount=150, category="fashion", message="Hi Riya, ₹150 off on fashion!")
    res = run(o, profile, catalog, rules, state, now)
    assert result_of(res, "profit.flat_vs_min_order") == "repaired"
    assert res.offer.min_order_rupees == 500  # 150 / 30%


def test_flat_off_too_big_for_min_order_is_lowered(profile, catalog, rules, state, now):
    o = offer(offer_type="flat_off", amount=200, category="fashion", min_order_rupees=500,
              message="Hi Riya, ₹200 off on fashion above ₹500!")
    res = run(o, profile, catalog, rules, state, now)
    assert result_of(res, "profit.flat_vs_min_order") == "repaired"
    assert res.status == "rejected"  # message still says Rs 200, but the offer is now Rs 150


def test_cashback_capped_at_max(profile, catalog, rules, state, now):
    o = offer(offer_type="cashback_to_wallet", amount=500, category="fashion", min_order_rupees=3000,
              message="x")
    res = run(o, profile, catalog, rules, state, now)
    assert result_of(res, "profit.cashback_max") == "repaired"


def test_free_delivery_min_order_raised(profile, catalog, rules, state, now):
    o = offer(offer_type="free_delivery", min_order_rupees=100, message="Hi Riya, free delivery above ₹299!")
    res = run(o, profile, catalog, rules, state, now)
    assert result_of(res, "profit.free_delivery_min_order") == "repaired"
    assert res.offer.min_order_rupees == 299


def test_points_multiplier_capped(profile, catalog, rules, state, now):
    o = offer(offer_type="points_multiplier", amount=5, message="Hi Riya, earn 3x points on your next order!")
    res = run(o, profile, catalog, rules, state, now)
    assert result_of(res, "profit.points_max") == "repaired" and res.offer.amount == 3


def test_points_multiplier_of_one_is_not_a_bonus(profile, catalog, rules, state, now):
    res = run(offer(offer_type="points_multiplier", amount=1, message="1x"), profile, catalog, rules, state, now)
    assert result_of(res, "profit.points_max") == "failed"


def test_buys_anyway_blocks_discount(catalog, rules, state, now):
    res = run(good_offer(), make_profile(p_buy_48h_no_offer=0.8), catalog, rules, state, now)
    assert result_of(res, "profit.buys_anyway") == "failed"


def test_buys_anyway_allows_points_perk(catalog, rules, state, now):
    o = offer(offer_type="points_multiplier", amount=2, message="Hi Riya, earn 2x points on your next order!")
    res = run(o, make_profile(p_buy_48h_no_offer=0.8), catalog, rules, state, now)
    assert result_of(res, "profit.buys_anyway") == "passed"


def test_margin_floor_repairs_by_lowering_discount(catalog, rules, state, now):
    # Rice: 18% margin. 10% off would leave 8%, below the 10% floor -> lowered to 8%
    o = offer(offer_type="pct_off", amount=10, product_id=3, min_order_rupees=500,
              message="Hi Riya! 10% off your Basmati Rice above ₹500.")
    res = run(o, make_profile(), catalog, rules, state, now)
    assert result_of(res, "profit.margin_floor") == "repaired"
    assert res.offer is None or res.offer.amount <= 8
    assert res.profit.margin_after_offer_pct >= rules.profit["margin_floor_pct"]


def test_low_expected_profit_rejected(catalog, rules, state, now):
    # Likely to buy anyway (60%): a discount mostly gives margin away
    res = run(good_offer(), make_profile(p_buy_48h_no_offer=0.6), catalog, rules, state, now)
    assert result_of(res, "profit.expected_profit") == "failed"
    assert "Expected profit" in next(c.detail for c in res.checks if c.rule == "profit.expected_profit")


# --- 4.5 fairness --------------------------------------------------------------------------------
def test_no_discount_right_after_full_price_purchase(profile, catalog, rules, now):
    o = good_offer(product_id=2, category=None, amount=10, message="Hi Riya! 10% off your Kurta above ₹999.")
    res = run(o, profile, catalog, rules, RuleState(full_price_products_7d={2}), now)
    assert result_of(res, "fairness.recent_full_price") == "failed"


def test_loyal_customer_raised_to_match_new_customer(catalog, rules, now):
    state = RuleState(new_customer_best={("pct_off", "fashion"): 20})
    res = run(good_offer(), make_profile(total_orders=12), catalog, rules, state, now)
    assert result_of(res, "fairness.loyal_vs_new") == "repaired"


def test_fairness_does_not_apply_to_non_loyal(catalog, rules, now):
    state = RuleState(new_customer_best={("pct_off", "fashion"): 20})
    res = run(good_offer(), make_profile(total_orders=2), catalog, rules, state, now)
    assert result_of(res, "fairness.loyal_vs_new") is None


# --- 4.2 budget ----------------------------------------------------------------------------------
def test_daily_budget_exceeded_rejected(profile, catalog, rules, now):
    res = run(good_offer(), profile, catalog, rules, RuleState(spent_today_paise=4990_00), now)
    assert result_of(res, "budget.daily") == "failed"


def test_monthly_budget_exceeded_rejected(profile, catalog, rules, now):
    res = run(good_offer(), profile, catalog, rules, RuleState(spent_month_paise=99_990_00), now)
    assert result_of(res, "budget.monthly") == "failed"


def test_per_customer_cap_exceeded_rejected(profile, catalog, rules, now):
    res = run(good_offer(), profile, catalog, rules, RuleState(customer_spent_month_paise=450_00), now)
    assert result_of(res, "budget.per_customer") == "failed"


# --- 4.6 message safety --------------------------------------------------------------------------
def test_empty_message_rejected(profile, catalog, rules, state, now):
    assert result_of(run(good_offer(message=""), profile, catalog, rules, state, now), "message.present") == "failed"


def test_long_message_rejected(profile, catalog, rules, state, now):
    res = run(good_offer(message=GOOD_MESSAGE + " Thanks!" * 50), profile, catalog, rules, state, now)
    assert result_of(res, "message.length") == "failed"


def test_link_rejected(profile, catalog, rules, state, now):
    res = run(good_offer(message=GOOD_MESSAGE + " Shop at www.deals.com"), profile, catalog, rules, state, now)
    assert result_of(res, "message.no_links") == "failed"


def test_fake_urgency_rejected(profile, catalog, rules, state, now):
    res = run(good_offer(message="Hurry! " + GOOD_MESSAGE), profile, catalog, rules, state, now)
    assert result_of(res, "message.no_fake_urgency") == "failed"


def test_unbacked_claim_rejected(profile, catalog, rules, state, now):
    res = run(good_offer(message=GOOD_MESSAGE + " Lowest price guaranteed."), profile, catalog, rules, state, now)
    assert result_of(res, "message.no_unbacked_claims") == "failed"


def test_message_with_wrong_percent_rejected(profile, catalog, rules, state, now):
    res = run(good_offer(message="Hi Riya! Get 50% off on fashion above ₹999."), profile, catalog, rules, state, now)
    assert result_of(res, "message.matches_offer") == "failed"


def test_message_with_wrong_rupee_amount_rejected(profile, catalog, rules, state, now):
    res = run(good_offer(message="Hi Riya! Get 15% off on fashion above ₹499."), profile, catalog, rules, state, now)
    assert result_of(res, "message.matches_offer") == "failed"


def test_message_must_name_product(profile, catalog, rules, state, now):
    o = good_offer(product_id=2, category=None, amount=10, message="Hi Riya! 10% off above ₹999.")
    assert result_of(run(o, profile, catalog, rules, state, now), "message.matches_offer") == "failed"


def test_message_hours_must_match_expiry(profile, catalog, rules, state, now):
    res = run(good_offer(message=GOOD_MESSAGE + " Valid for 6 hours."), profile, catalog, rules, state, now)
    assert result_of(res, "message.matches_offer") == "failed"


def test_message_mentioning_wallet_balance_is_allowed(catalog, rules, state, now):
    o = offer(offer_type="reminder", message="Hi Riya, you have ₹350 in your wallet. Use it on your next order!")
    res = run(o, make_profile(wallet_balance_rupees=350), catalog, rules, state, now)
    assert result_of(res, "message.matches_offer") == "passed"
