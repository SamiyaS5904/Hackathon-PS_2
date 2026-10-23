"""
Harness test 4 (Session 3 part): the AI agent loop, with a FAKE Claude client (free, repeatable).

Proves:
  - a good proposal is checked by the rules inside the loop and approved, with reasoning, trace, tokens and cost
  - a rejected proposal sends the rule reasons back to the AI, which can fix the offer
  - 2 rejections, API errors, timeouts, refusals, junk output, invalid input and the 6-call limit all end in
    status 'failed' (never an exception), so the fallback can take over
  - the requests use the configured model, effort, strict tools and the system prompt
  - through decide(): AI failure -> fallback decides but the AI cost is still recorded; 'send nothing' is logged as
    an AI decision; a used-up Day Plan means the AI is never called
Run: pytest harness/test_agent.py -v
"""

from __future__ import annotations

from engine.agent import MAX_API_CALLS, AgentContext, run_agent
from engine.decide import decide
from engine.gatekeeper import agent_runs_used, set_day_plan
from harness.conftest import NOON_IST, make_profile
from harness.fake_agent import REASONING, Response, ScriptedClient, estimate_input, offer_input, text_only, tool_call


def context(profile, catalog, rules, state, now) -> AgentContext:
    return AgentContext(conn=None, profile=profile, catalog=catalog, rules=rules, state=state, now=now,
                        trigger_type="manual")


def test_good_proposal_is_approved(profile, catalog, rules, state, now):
    client = ScriptedClient([
        tool_call("get_customer_profile"), tool_call("get_offer_limits"),
        tool_call("estimate_profit", estimate_input()), tool_call("propose_offer", offer_input()),
    ])
    out = run_agent(context(profile, catalog, rules, state, now), client=client)
    assert out.status == "approved", out.error
    assert out.check.offer.amount == 15 and out.check.ok
    assert out.reasoning["summary"] == REASONING["summary"]
    assert [t["tool"] for t in out.tool_trace] == ["get_customer_profile", "get_offer_limits", "estimate_profit",
                                                   "propose_offer"]
    assert out.input_tokens == 4 * 1200 and out.output_tokens == 4 * 250 and out.cost_usd > 0
    # The request is shaped correctly for Claude Opus 5.5
    first = client.calls[0]
    assert first["output_config"] == {"effort": "medium"} and "tool_choice" not in first
    assert all(t["strict"] for t in first["tools"]) and "OfferPilot" in first["system"]


def test_rejection_reasons_go_back_and_agent_fixes_offer(profile, catalog, rules, state, now):
    bad = offer_input(amount=25, category="electronics", message="Hurry! 25% off electronics above ₹999.")
    client = ScriptedClient([tool_call("propose_offer", bad), tool_call("propose_offer", offer_input())])
    out = run_agent(context(profile, catalog, rules, state, now), client=client)
    assert out.status == "approved" and out.rejected_proposals == 1
    feedback = client.calls[1]["messages"][-1]["content"][0]
    assert feedback["is_error"] is True
    assert "REJECTED" in feedback["content"] and "fake urgency" in feedback["content"].lower()
    assert out.first_proposal["amount"] == 25  # the dashboard can show what the AI first tried


def test_two_rejections_end_in_failure(profile, catalog, rules, state, now):
    bad = offer_input(message="Hurry! Get 15% off on fashion above ₹999.")
    client = ScriptedClient([tool_call("propose_offer", bad), tool_call("propose_offer", bad)])
    out = run_agent(context(profile, catalog, rules, state, now), client=client)
    assert out.status == "failed" and "2 proposals were rejected" in out.error


def test_agent_can_decide_to_send_nothing(profile, catalog, rules, state, now):
    client = ScriptedClient([tool_call("get_customer_profile"), tool_call("propose_no_offer", {"reasoning": REASONING})])
    out = run_agent(context(profile, catalog, rules, state, now), client=client)
    assert out.status == "no_offer" and out.check is None and out.reasoning["why_now"]


def test_api_error_becomes_failure(profile, catalog, rules, state, now):
    out = run_agent(context(profile, catalog, rules, state, now),
                    client=ScriptedClient([ConnectionError("network down")]))
    assert out.status == "failed" and "AI call failed" in out.error


def test_timeout_becomes_failure(profile, catalog, rules, state, now):
    client = ScriptedClient([tool_call("get_customer_profile"), TimeoutError("too slow")])
    out = run_agent(context(profile, catalog, rules, state, now), client=client)
    assert out.status == "failed" and "TimeoutError" in out.error
    assert out.input_tokens == 1200  # the first call still cost money and is counted


def test_junk_output_becomes_failure(profile, catalog, rules, state, now):
    out = run_agent(context(profile, catalog, rules, state, now), client=ScriptedClient([text_only()]))
    assert out.status == "failed" and "without proposing" in out.error


def test_refusal_becomes_failure(profile, catalog, rules, state, now):
    out = run_agent(context(profile, catalog, rules, state, now), client=ScriptedClient([Response([], "refusal")]))
    assert out.status == "failed" and "declined" in out.error


def test_invalid_proposal_counts_as_rejection(profile, catalog, rules, state, now):
    client = ScriptedClient([tool_call("propose_offer", offer_input(amount=-50)),
                             tool_call("propose_offer", offer_input())])
    out = run_agent(context(profile, catalog, rules, state, now), client=client)
    assert out.status == "approved" and out.rejected_proposals == 1


def test_call_limit_stops_endless_tool_use(profile, catalog, rules, state, now):
    client = ScriptedClient([tool_call("get_customer_profile") for _ in range(MAX_API_CALLS)])
    out = run_agent(context(profile, catalog, rules, state, now), client=client)
    assert out.status == "failed" and f"after {MAX_API_CALLS} AI calls" in out.error
    assert len(client.calls) == MAX_API_CALLS


def test_tools_return_profile_and_limits(profile, catalog, rules, state, now):
    seen = {}

    def capture_then_decide(messages):
        seen["results"] = messages[-1]["content"]
        return tool_call("propose_no_offer", {"reasoning": REASONING})

    client = ScriptedClient([tool_call("get_offer_limits"), capture_then_decide])
    run_agent(context(profile, catalog, rules, state, now), client=client)
    text = seen["results"][0]["content"]
    assert '"category_discount_cap_pct"' in text and '"electronics": 8' in text


def test_buys_anyway_customer_only_sees_perks_in_limits(catalog, rules, state, now):
    seen = {}

    def capture(messages):
        seen["text"] = messages[-1]["content"][0]["content"]
        return tool_call("propose_no_offer", {"reasoning": REASONING})

    profile = make_profile(p_buy_48h_no_offer=0.85)
    run_agent(context(profile, catalog, rules, state, now), client=ScriptedClient([tool_call("get_offer_limits"), capture]))
    assert '"discounts_allowed": false' in seen["text"] and '"pct_off"' not in seen["text"].split("buys_anyway")[0]


# --- through the full pipeline (small real database) ---------------------------------------------
def _customer(db) -> int:
    return db.execute("SELECT id FROM customers WHERE marketing_consent = 1 LIMIT 1").fetchone()[0]


def test_agent_failure_hands_over_to_fallback_and_cost_is_recorded(db):
    client = ScriptedClient([tool_call("get_customer_profile"), ConnectionError("down")])
    out = decide(db, _customer(db), now=NOON_IST, agent_client=client)
    assert out.decided_by == "fallback"
    assert out.reasoning["agent_attempt"]["error"].startswith("AI call failed")
    assert out.input_tokens == 1200 and out.cost_usd > 0
    assert agent_runs_used(db, NOON_IST) == 1  # the AI call counts against today's plan


def test_agent_no_offer_is_logged_as_agent_decision(db):
    client = ScriptedClient([tool_call("propose_no_offer", {"reasoning": REASONING})])
    out = decide(db, _customer(db), now=NOON_IST, agent_client=client)
    assert out.decided_by == "agent" and out.status == "nothing" and out.decision_id
    row = db.execute("SELECT tool_trace, input_tokens FROM decisions WHERE id = ?", (out.decision_id,)).fetchone()
    assert "propose_no_offer" in row["tool_trace"] and row["input_tokens"] == 1200


def test_used_up_day_plan_never_calls_the_ai(db):
    set_day_plan(db, "2026-10-23", 0)
    client = ScriptedClient([])  # any call would fail the test
    out = decide(db, _customer(db), now=NOON_IST, agent_client=client)
    assert out.decided_by in ("fallback", "gatekeeper") and client.calls == []
    if out.decided_by == "fallback":
        assert "plan is used up" in out.reasoning["decided_by_note"]
