"""
Tests for v2 messages: variants, Hinglish and price anchors (engine/agent.py, engine/rules.py, engine/fallback.py).

Proves: every message variant is checked (a dishonest one blocks the proposal even if the chosen one is fine);
2-3 variants are required; the chosen variant and all variants are stored with the decision; a price anchor is
allowed only with the catalog's real before/after numbers; Hinglish pressure phrases and false Hinglish deadlines are
rejected; Hindi-speaking customers get the Hinglish fallback template.
Run: pytest harness/test_variants.py -v
"""

from __future__ import annotations

import json

from engine.agent import AgentContext, run_agent
from engine.decide import decide
from engine.fallback import fallback_decide
from engine.rules import check_offer
from harness.conftest import NOON_IST, make_profile, offer
from harness.fake_agent import REASONING, ScriptedClient, offer_input, tool_call

HONEST = "Hi Riya! Get 15% off on fashion above ₹999, valid for 24 hours."


def variants(*texts: str) -> list[dict]:
    return [{"id": chr(65 + i), "tone": t, "text": text} for i, (t, text) in
            enumerate(zip(["warm", "price-first", "playful"], texts))]


def propose(texts: list[str], chosen: str = "A") -> dict:
    return offer_input(variants=variants(*texts), chosen_variant_id=chosen)


def test_dishonest_other_variant_blocks_the_proposal(catalog, rules, state, now):
    client = ScriptedClient([tool_call("propose_offer", propose([HONEST, "Hurry! " + HONEST])),
                             tool_call("propose_offer", propose([HONEST, "Hi Riya, 15% off fashion above ₹999."]))])
    out = run_agent(AgentContext(None, make_profile(), catalog, rules, state, now, "manual"), client=client)
    assert out.status == "approved" and out.rejected_proposals == 1
    first_reply = client.calls[1]["messages"][-1]["content"][0]["content"]
    assert "variant B" in first_reply and "no_fake_urgency" in first_reply
    assert out.variant_id == "A" and [v["id"] for v in out.variants] == ["A", "B"]
    assert out.check.offer.message == HONEST and out.reasoning["why_this_variant"]


def test_two_to_three_variants_are_required(catalog, rules, state, now):
    client = ScriptedClient([tool_call("propose_offer", propose([HONEST])),
                             tool_call("propose_no_offer", {"reasoning": REASONING})])
    out = run_agent(AgentContext(None, make_profile(), catalog, rules, state, now, "manual"), client=client)
    assert out.status == "no_offer" and out.rejected_proposals == 1


def test_variants_are_stored_with_the_decision(db):
    customer_id = db.execute("SELECT id FROM customers WHERE marketing_consent = 1 LIMIT 1").fetchone()[0]
    db.execute("UPDATE customers SET language = 'English' WHERE id = ?", (customer_id,))
    client = ScriptedClient([tool_call("propose_offer", offer_input(
        offer_type="early_access", amount=0, category="fashion", min_order_rupees=0,
        variants=variants("Hi there! Early access to the new fashion drop is yours.",
                          "A little something: early access to our new fashion drop."), chosen_variant_id="B"))])
    out = decide(db, customer_id, now=NOON_IST, agent_client=client)
    assert out.decided_by == "agent" and out.offer is not None, out.reasoning.get("summary")
    row = db.execute("SELECT variant_id, variants, message FROM decisions WHERE id = ?", (out.decision_id,)).fetchone()
    assert row["variant_id"] == "B" and len(json.loads(row["variants"])) == 2
    assert row["message"].startswith("A little something")


def test_price_anchor_must_use_the_real_numbers(catalog, rules, state, now):
    kurta = dict(offer_type="pct_off", amount=10, product_id=2, min_order_rupees=999, expiry_hours=24)
    real = check_offer(offer(**kurta, message="Hi Riya, the Urban Thread Kurta Classic: ₹1500 → ₹1350 (10% off)."),
                       make_profile(), catalog, rules, state, now)
    fake = check_offer(offer(**kurta, message="Hi Riya, the Urban Thread Kurta Classic: ₹1999 → ₹1350 (10% off)."),
                       make_profile(), catalog, rules, state, now)
    assert not any(c.rule == "message.matches_offer" and c.result == "failed" for c in real.checks), real.failures
    assert any(c.rule == "message.matches_offer" and c.result == "failed" for c in fake.checks)


def test_hinglish_pressure_and_false_deadline_rejected(catalog, rules, state, now):
    base = dict(offer_type="pct_off", amount=15, category="fashion", min_order_rupees=999, expiry_hours=48)
    pushy = check_offer(offer(**base, message="Hi Riya, jaldi karo! Fashion pe 15% off, ₹999 se upar."),
                        make_profile(), catalog, rules, state, now)
    deadline = check_offer(offer(**base, message="Hi Riya, fashion pe 15% off ₹999 se upar, sirf aaj raat tak."),
                           make_profile(), catalog, rules, state, now)
    assert any(c.rule == "message.no_fake_urgency" and c.result == "failed" for c in pushy.checks)
    assert any(c.rule == "message.honest_deadline" and c.result == "failed" for c in deadline.checks)


def test_hindi_customer_gets_hinglish_fallback_template(catalog, rules, state, now):
    profile = make_profile(language="Hindi", points_expiring_7d=300, points_next_expiry="2026-10-27T00:00:00Z",
                           p_buy_48h_no_offer=0.05)
    result = fallback_decide(profile, catalog, rules, state, now, "test")
    assert result.check is not None and result.check.ok, result.reasoning
    assert "aapke 300 reward points" in result.check.offer.message
