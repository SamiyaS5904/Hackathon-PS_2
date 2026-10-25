"""
Harness test 4 (Session 2 part): the fallback rule table and the pipeline without AI.

Proves: rules are tried in the documented order; the first one that passes the rules wins; template messages
always match the final offer; nothing is sent when nothing passes; the pipeline falls back cleanly when there is
no API key, and the gatekeeper skips customers who can't get an offer (without spending an AI call).
More fallback cases (API down, timeout, junk output, 2 rejections) are added with the agent in Session 3.
Run: pytest harness/test_fallback.py -v
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from engine.decide import decide
from harness.fake_agent import Response, ScriptedClient, offer_input, text_only, tool_call
from engine.fallback import fallback_decide
from engine.gatekeeper import get_day_plan, set_day_plan
from engine.rules import RuleState
from harness.conftest import NOON_IST, make_profile


def test_nothing_fits_sends_nothing(catalog, rules, state, now):
    res = fallback_decide(make_profile(), catalog, rules, state, now, "no API key")
    assert res.check is None
    assert res.reasoning["summary"].startswith("Send nothing")
    assert [t["rule"][0] for t in res.tried] == ["1", "2", "3", "4", "5"]  # all rules considered, in order


def test_points_expiring_gives_free_reminder(catalog, rules, state, now):
    profile = make_profile(points_expiring_7d=300, points_next_expiry="2026-10-27T00:00:00Z")
    res = fallback_decide(profile, catalog, rules, state, now, "no API key")
    assert res.check and res.check.offer.offer_type == "reminder"
    assert "300 reward points" in res.check.offer.message and "27 Oct" in res.check.offer.message
    assert res.check.profit.offer_cost_paise == 0


def test_open_cart_gets_free_delivery(catalog, rules, state, now):
    cart = {"left_hours_ago": 5, "cart_value_rupees": 900,
            "items": [{"product_id": 2, "name": "Urban Thread Kurta Classic", "category": "fashion", "price_rupees": 1500}]}
    res = fallback_decide(make_profile(open_cart=cart), catalog, rules, state, now, "no API key")
    assert res.check.offer.offer_type == "free_delivery"
    assert "Urban Thread Kurta Classic" in res.check.offer.message and "₹299" in res.check.offer.message
    assert "fallback" in res.reasoning["decided_by_note"]


def test_habit_break_offer_comes_first(catalog, rules, state, now):
    fav = [{"product_id": 2, "name": "Urban Thread Kurta Classic", "category": "fashion", "price_rupees": 1500,
            "times_ordered": 6, "last_bought": "2026-08-01"}]
    cart = {"left_hours_ago": 5, "cart_value_rupees": 900, "items": [fav[0]]}
    profile = make_profile(overdue_ratio=2.0, favourite_items=fav, open_cart=cart, deal_seeker_score=0.6)
    res = fallback_decide(profile, catalog, rules, state, now, "no API key")
    assert res.candidate.rule_no == 1
    assert res.check.offer.product_id == 2 and "10%" in res.check.offer.message


def test_fallback_offers_still_obey_rules(catalog, rules, now):
    cart = {"left_hours_ago": 5, "cart_value_rupees": 900,
            "items": [{"product_id": 2, "name": "Urban Thread Kurta Classic", "category": "fashion", "price_rupees": 1500}]}
    res = fallback_decide(make_profile(open_cart=cart), catalog, rules, RuleState(offers_last_7d=2), now, "test")
    assert res.check is None
    assert any(t["outcome"] == "blocked by rules" for t in res.tried)


def test_pipeline_without_api_key_uses_fallback(db):
    customer_id = db.execute("SELECT id FROM customers WHERE marketing_consent = 1 LIMIT 1").fetchone()[0]
    outcome = decide(db, customer_id, now=NOON_IST)
    assert outcome.decided_by in ("fallback", "gatekeeper")
    assert outcome.input_tokens is None  # no AI call was made


def test_gatekeeper_skips_second_offer_within_24h(db):
    """Find a customer who gets an offer, then ask again 2 hours later: skipped before any AI call."""
    for (customer_id,) in db.execute("SELECT id FROM customers WHERE marketing_consent = 1").fetchall():
        first = decide(db, customer_id, now=NOON_IST, use_agent=False)
        if first.offer:
            second = decide(db, customer_id, now=NOON_IST + timedelta(hours=2), use_agent=False)
            assert second.decided_by == "gatekeeper" and second.status == "nothing"
            assert "ago" in second.reasoning["summary"] or "active offer" in second.reasoning["summary"]
            return
    raise AssertionError("no customer in the small dataset got an offer")


def test_no_consent_is_skipped_by_gatekeeper(db):
    row = db.execute("SELECT id FROM customers WHERE marketing_consent = 0 LIMIT 1").fetchone()
    if row is None:
        db.execute("UPDATE customers SET marketing_consent = 0 WHERE id = 1")
        row = (1,)
    outcome = decide(db, row[0], now=NOON_IST)
    assert outcome.decided_by == "gatekeeper" and "agreed" in outcome.reasoning["summary"]


@pytest.mark.parametrize("label, steps", [
    ("API down", [ConnectionError("network down")]),
    ("timeout", [TimeoutError("too slow")]),
    ("junk output", [text_only("Let me think about a nice discount...")]),
    ("refusal", [Response([], "refusal")]),
    ("2 rejections", [tool_call("propose_offer", offer_input(message="Hurry! 15% off fashion above ₹999.")),
                      tool_call("propose_offer", offer_input(message="Hurry! 15% off fashion above ₹999."))]),
])
def test_every_ai_failure_hands_over_to_checked_fallback(db, label, steps):
    """Whatever goes wrong with the AI, the free rules decide, and their offer is still checked by the rules."""
    customer_id = db.execute("SELECT id FROM customers WHERE marketing_consent = 1 LIMIT 1").fetchone()[0]
    out = decide(db, customer_id, now=NOON_IST, agent_client=ScriptedClient(steps))
    assert out.decided_by == "fallback", label
    assert out.reasoning["agent_attempt"]["error"], label
    if out.offer:
        assert out.rule_checks and all(c["result"] != "failed" for c in out.rule_checks), label
    if label == "2 rejections" and not out.offer:
        assert out.status == "blocked"  # counts towards the circuit breaker


def test_circuit_breaker_pauses_everything(db):
    """More than half of the last 20 decisions blocked -> offers pause before any AI call."""
    customer_id = db.execute("SELECT id FROM customers WHERE marketing_consent = 1 LIMIT 1").fetchone()[0]
    for _ in range(20):
        db.execute("INSERT INTO decisions (customer_id, created_at, trigger_type, decided_by, status, reasoning, "
                   "rule_checks, rules_version) VALUES (?, '2026-10-23T05:00:00Z', 'manual', 'agent', 'blocked', "
                   "'{}', '[]', 1)", (customer_id + 1,))
    db.commit()
    client = ScriptedClient([])
    out = decide(db, customer_id, now=NOON_IST, agent_client=client)
    assert out.decided_by == "gatekeeper" and "Circuit breaker" in out.reasoning["summary"] and client.calls == []


def test_day_plan_is_capped_by_env_ceiling(db):
    assert set_day_plan(db, "2026-10-23", 500) == get_day_plan(db, "2026-10-23") <= 50
    assert set_day_plan(db, "2026-10-23", 20) == 20
