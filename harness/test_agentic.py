"""
Tests for the "more agentic" features (all with the fake Claude client, free and repeatable):
  B  the AI prices several of its own ideas in one call, with a rules check per idea
  C  trigger detection (cart, interest, habit break, points, wallet), strongest first
  E  honest deadlines ("until midnight" only if true)      F  verified loyalty claims
  G  the AI may WAIT; follow-ups then resolve it (bought on their own, or a new decision); no endless waiting
Run: pytest harness/test_agentic.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from engine.agent import AgentContext, run_agent
from engine.decide import decide
from engine.followups import run_due_followups
from engine.rules import check_offer
from engine.triggers import detect_triggers
from harness.conftest import NOON_IST, make_profile, offer
from harness.fake_agent import REASONING, ScriptedClient, offer_input, tool_call

CART = {"left_hours_ago": 1.0, "cart_value_rupees": 1500,
        "items": [{"product_id": 2, "name": "Urban Thread Kurta Classic", "category": "fashion", "price_rupees": 1500}]}


def ctx(profile, catalog, rules, state, now, **kw) -> AgentContext:
    return AgentContext(conn=None, profile=profile, catalog=catalog, rules=rules, state=state, now=now,
                        trigger_type=kw.pop("trigger_type", "manual"), **kw)


# --- B: pricing several own ideas in one call ------------------------------------------------------
def test_agent_prices_several_ideas_in_one_call(profile, catalog, rules, state, now):
    seen = {}

    def read_prices(messages):
        seen["result"] = json.loads(messages[-1]["content"][0]["content"])
        return tool_call("propose_no_offer", {"reasoning": REASONING})

    ideas = [dict(offer_input(), amount=a) for a in (10, 50)]  # 50% is over the fashion cap -> repaired
    for i in ideas:
        for k in ("send_at_ist", "variants", "chosen_variant_id", "why_this_variant", "reasoning"):
            i.pop(k)
    run_agent(ctx(profile, catalog, rules, state, now),
              client=ScriptedClient([tool_call("estimate_profit", {"ideas": ideas}), read_prices]))
    priced = seen["result"]["priced_ideas"]
    assert len(priced) == 2 and all("expected_profit_rupees" in p for p in priced)
    assert any("lowered to 30%" in " ".join(p.get("rules_adjusted", [])) for p in priced)


# --- C: triggers -------------------------------------------------------------------------------------
def test_triggers_detected_strongest_first():
    profile = make_profile(open_cart=CART, overdue_ratio=2.0, total_orders=10, points_expiring_7d=200,
                           intent_products=[{"product_id": 4, "name": "Glowveda Serum Neo", "category": "beauty",
                                             "price_rupees": 900, "signals": ["searched 'serum'", "viewed 3 times"]}])
    found = detect_triggers(profile)
    assert [t.type for t in found][:2] == ["cart_abandoned", "habit_break"]
    assert {"product_interest", "points_expiring"} <= {t.type for t in found}
    cart = found[0]
    assert cart.detail["product"] == "Urban Thread Kurta Classic" and cart.suggested_delay_hours == 1.0


def test_no_triggers_for_quiet_customer():
    assert detect_triggers(make_profile()) == []


# --- E: honest deadlines -------------------------------------------------------------------------------
def test_until_midnight_allowed_when_true(profile, catalog, rules, state):
    evening = datetime(2026, 10, 23, 14, 30, tzinfo=timezone.utc)  # 20:00 IST -> 4 hours to midnight
    o = offer(offer_type="pct_off", amount=15, category="fashion", min_order_rupees=999, expiry_hours=4,
              message="Hi Riya, 15% off fashion above ₹999, valid until midnight tonight.")
    res = check_offer(o, profile, catalog, rules, state, evening)
    assert next(c for c in res.checks if c.rule == "message.honest_deadline").result == "passed"


def test_until_midnight_blocked_when_false(profile, catalog, rules, state):
    evening = datetime(2026, 10, 23, 14, 30, tzinfo=timezone.utc)
    o = offer(offer_type="pct_off", amount=15, category="fashion", min_order_rupees=999, expiry_hours=24,
              message="Hi Riya, 15% off fashion above ₹999, valid until midnight tonight.")
    res = check_offer(o, profile, catalog, rules, state, evening)
    assert res.status == "rejected"
    assert next(c for c in res.checks if c.rule == "message.honest_deadline").result == "failed"


def test_agent_sees_hours_until_midnight_and_loyalty(catalog, rules, state, now):
    seen = {}

    def capture(messages):
        seen["first"] = messages[0]["content"]
        return tool_call("propose_no_offer", {"reasoning": REASONING})

    run_agent(ctx(make_profile(total_orders=8), catalog, rules, state, now), client=ScriptedClient([capture]))
    limits = json.loads(seen["first"].split("<offer_limits>")[1].split("</offer_limits>")[0])
    assert limits["hours_until_midnight_ist"] == 12 and limits["customer_is_loyal"] is True  # noon IST


# --- F: verified loyalty -------------------------------------------------------------------------------
def test_loyal_claim_needs_real_loyalty(catalog, rules, state, now):
    o = offer(offer_type="pct_off", amount=15, category="fashion", min_order_rupees=999,
              message="Hi Riya, as a loyal customer enjoy 15% off fashion above ₹999.")
    new = check_offer(o, make_profile(total_orders=2), catalog, rules, state, now)
    loyal = check_offer(o, make_profile(total_orders=9), catalog, rules, state, now)
    assert next(c for c in new.checks if c.rule == "message.verified_loyalty").result == "failed"
    assert next(c for c in loyal.checks if c.rule == "message.verified_loyalty").result == "passed"


# --- G: wait first, follow up later ---------------------------------------------------------------------
def test_agent_can_choose_to_wait(catalog, rules, state, now):
    out = run_agent(ctx(make_profile(open_cart=CART), catalog, rules, state, now),
                    client=ScriptedClient([tool_call("propose_wait", {"check_again_in_hours": 6,
                                                                      "reasoning": REASONING})]))
    assert out.status == "waiting" and out.wait_hours == 6


def test_wait_hours_are_kept_in_range(profile, catalog, rules, state, now):
    out = run_agent(ctx(profile, catalog, rules, state, now),
                    client=ScriptedClient([tool_call("propose_wait", {"check_again_in_hours": 500,
                                                                      "reasoning": REASONING})]))
    assert out.wait_hours == 48


def test_followup_decision_cannot_wait_again(profile, catalog, rules, state, now):
    client = ScriptedClient([tool_call("propose_wait", {"check_again_in_hours": 6, "reasoning": REASONING}),
                             tool_call("propose_no_offer", {"reasoning": REASONING})])
    out = run_agent(ctx(profile, catalog, rules, state, now, trigger_type="followup", allow_wait=False), client=client)
    assert out.status == "no_offer"
    assert "Not allowed" in client.calls[1]["messages"][-1]["content"][0]["content"]


def _customer(db) -> int:
    return db.execute("SELECT id FROM customers WHERE marketing_consent = 1 LIMIT 1").fetchone()[0]


def _wait_decision(db, now):
    client = ScriptedClient([tool_call("propose_wait", {"check_again_in_hours": 6, "reasoning": REASONING})])
    out = decide(db, _customer(db), "cart_abandoned", now=now, agent_client=client)
    assert out.status == "waiting" and out.decision_id
    return out


def test_waiting_customer_is_skipped_until_followup(db):
    _wait_decision(db, NOON_IST)
    again = decide(db, _customer(db), "product_interest", now=NOON_IST + timedelta(hours=1))
    assert again.decided_by == "gatekeeper" and "waiting" in again.reasoning["summary"]


def test_followup_marks_bought_naturally_when_they_ordered(db):
    out = _wait_decision(db, NOON_IST)
    db.execute("INSERT INTO orders (customer_id, ts, gross_paise, discount_paise, delivery_fee_paise, net_paise, "
               "payment_method, status) VALUES (?, '2026-10-23T09:00:00Z', 99900, 0, 0, 99900, 'upi', 'delivered')",
               (out.customer_id,))
    report = run_due_followups(db, now=NOON_IST + timedelta(hours=7))
    assert report[0]["result"] == "bought_naturally"
    row = db.execute("SELECT status, reasoning FROM decisions WHERE id = ?", (out.decision_id,)).fetchone()
    assert row["status"] == "bought_naturally" and "cost was saved" in row["reasoning"]


def test_followup_makes_new_decision_when_they_did_not_buy(db):
    out = _wait_decision(db, NOON_IST)
    report = run_due_followups(db, now=NOON_IST + timedelta(hours=7))  # no key in tests -> fallback decides
    assert report[0]["result"] == "followed_up" and report[0]["new_decision_id"]
    new = db.execute("SELECT trigger_type, trigger_detail FROM decisions WHERE id = ?",
                     (report[0]["new_decision_id"],)).fetchone()
    assert new["trigger_type"] == "followup" and str(out.decision_id) in new["trigger_detail"]
