"""
The decision pipeline: one call takes a customer + a trigger and produces a fully explained, logged decision.

Where it sits: the conductor of STEPS 2-6.
    decide() -> features (profile) -> gatekeeper (skip / agent / fallback) -> agent or fallback
             -> rules checker -> status (would_send / queued / scheduled / nothing / blocked) -> decisions table

Run it from the command line:
    python -m engine.decide --customer 42                 # decide for customer 42 and save it
    python -m engine.decide --customer 42 --dry-run       # show the decision without saving
    python -m engine.decide --showcase                    # one example per situation (habit break, cart, ...)
    python -m engine.decide --showcase --no-agent         # force the free fallback

Status meanings (shown on the dashboard):
  would_send - approved; customer has no Telegram linked, so it's recorded as "would send"
  queued     - approved; customer is a linked tester, the bot will deliver it (Session 4)
  scheduled  - approved; send time is later (e.g. moved out of quiet hours)
  nothing    - deliberately sent nothing (gatekeeper skip, or no offer worth it)
  blocked    - the AI's proposals kept breaking the rules and nothing could be sent
  cancelled  - a scheduled offer that failed a re-check after the rules changed
  waiting    - the AI chose to wait: maybe they buy on their own; a follow-up looks again at send_at
               (engine/followups.py). Then it becomes:
  bought_naturally - they bought while we waited: no offer was needed, its cost was saved
  followed_up      - they didn't buy: a new 'followup' decision was made (linked in the reasoning)
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from db.store import connect
from engine.agent import AgentContext, AgentOutcome, run_agent
from engine.catalog import Catalog, load_catalog
from engine.clock import now_utc, parse_ts, to_ist, to_ts
from engine.fallback import fallback_decide
from engine.features import CustomerProfile, build_profile
from engine.gatekeeper import screen
from engine.models import CheckResult, OfferProposal
from engine.rules import check_offer, load_rule_state
from engine.rules_store import get_active_rules
from engine.triggers import detect_triggers


@dataclass
class DecisionOutcome:
    """Everything about one decision, as saved in the `decisions` table."""

    customer_id: int
    first_name: str
    trigger_type: str
    decided_by: str                          # 'agent' | 'fallback' | 'gatekeeper'
    status: str
    offer: OfferProposal | None
    reasoning: dict
    rule_checks: list[dict]
    expected_profit_paise: int | None
    offer_cost_paise: int | None
    rules_version: int
    proposal: dict | None = None             # what was first proposed (before repairs)
    tool_trace: list[dict] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    followup_at: datetime | None = None      # for 'waiting': when to look at the customer again
    decision_id: int | None = None
    created_at: str = ""
    offer_kind: str = "normal"               # 'winback' = relies on retention value / habit exception (own budget)
    variant_id: str | None = None            # which message variant was sent
    variants: list[dict] | None = None       # all message variants the agent wrote (each passed the message rules)


def decide(conn: sqlite3.Connection, customer_id: int, trigger_type: str = "manual",
           trigger_detail: dict | None = None, now: datetime | None = None, use_agent: bool = True,
           dry_run: bool = False, catalog: Catalog | None = None, agent_client=None) -> DecisionOutcome:
    """
    Make, explain and (unless dry_run) save one decision for one customer.
    `agent_client` lets tests plug in a fake AI; normally the real client is created only if a key exists.
    """
    now = now or now_utc()
    catalog = catalog or load_catalog(conn)
    rules = get_active_rules(conn)
    profile = build_profile(conn, customer_id, now, catalog)
    state = load_rule_state(conn, customer_id, rules, now)
    gate = screen(conn, profile, rules, state, now, trigger_type, want_agent=use_agent,
                  agent_available=True if agent_client is not None else None)

    if gate.route == "skip":
        outcome = DecisionOutcome(
            customer_id, profile.first_name, trigger_type, "gatekeeper", "nothing", None,
            reasoning={"summary": f"Skipped before any offer was considered: {gate.reason}",
                       "signals_used": [], "alternatives_considered": [],
                       "expected_impact": "No cost; no AI call spent.", "why_now": gate.reason},
            rule_checks=[], expected_profit_paise=None, offer_cost_paise=None, rules_version=rules.version)
    elif gate.route == "agent":
        ctx = AgentContext(conn, profile, catalog, rules, state, now, trigger_type, trigger_detail,
                           allow_wait=trigger_type != "followup")
        agent = run_agent(ctx, client=agent_client)
        if agent.status == "approved":
            outcome = _agent_outcome(agent, profile, rules, now, trigger_type)
        elif agent.status == "waiting":
            # Smart timing: nothing is sent now; engine/followups.py looks at this customer again later
            outcome = DecisionOutcome(
                customer_id, profile.first_name, trigger_type, "agent", "waiting", None,
                reasoning=agent.reasoning, rule_checks=[], expected_profit_paise=None, offer_cost_paise=None,
                rules_version=rules.version, followup_at=now + timedelta(hours=agent.wait_hours))
        elif agent.status == "no_offer":
            outcome = DecisionOutcome(
                customer_id, profile.first_name, trigger_type, "agent", "nothing", None,
                reasoning=agent.reasoning, rule_checks=[], expected_profit_paise=None, offer_cost_paise=None,
                rules_version=rules.version)
        else:
            # The AI failed (error, timeout, junk, too many rejections): the free fallback decides instead.
            outcome = _fallback_outcome(profile, catalog, rules, state, now, trigger_type, agent.error)
            outcome.reasoning["agent_attempt"] = {"error": agent.error, "rejected_proposals": agent.rejected_proposals}
            if outcome.offer is None and agent.rejected_proposals:
                outcome.status = "blocked"  # counts towards the circuit breaker
        # The menu of offers code prepared for the AI: the manager sees every option and its numbers
        if agent.candidates:
            outcome.reasoning["candidates_prepared_by_code"] = agent.candidates
        # The AI was called, so its cost and trace are recorded whatever happened (counts in the Day Plan)
        outcome.tool_trace, outcome.proposal = agent.tool_trace, agent.first_proposal or outcome.proposal
        outcome.input_tokens, outcome.output_tokens = agent.input_tokens, agent.output_tokens
        outcome.cost_usd = agent.cost_usd
    else:
        outcome = _fallback_outcome(profile, catalog, rules, state, now, trigger_type, gate.reason)

    outcome.created_at = to_ts(now)
    if not dry_run:
        outcome.decision_id = _save(conn, outcome, trigger_detail)
    return outcome


def value_summary(check: CheckResult) -> dict:
    """The value split the manager sees: normal vs win-back, immediate profit, retention value, total."""
    p = check.profit
    return {"offer_kind": check.offer_kind,
            "immediate_profit_rupees": p.expected_profit_paise // 100,
            "retention_value_rupees": p.retention_value_paise // 100,
            "total_value_rupees": p.total_value_paise // 100,
            "late_customer": p.is_late_customer} if p else {"offer_kind": check.offer_kind}


def _agent_outcome(agent: AgentOutcome, profile: CustomerProfile, rules, now: datetime,
                   trigger_type: str) -> DecisionOutcome:
    """An approved AI decision. Any automatic repairs by the rules are added to the reasoning, so the manager sees
    exactly what the AI proposed and what the rules changed."""
    check = agent.check
    reasoning = dict(agent.reasoning)
    repairs = [c.detail for c in check.checks if c.result == "repaired"]
    if repairs:
        reasoning["rule_repairs"] = repairs
    reasoning["value"] = value_summary(check)
    return DecisionOutcome(
        profile.customer_id, profile.first_name, trigger_type, "agent", _delivery_status(check, profile, now),
        check.offer, reasoning=reasoning, rule_checks=[c.model_dump() for c in check.checks],
        expected_profit_paise=check.profit.expected_profit_paise if check.profit else None,
        offer_cost_paise=check.profit.offer_cost_paise if check.profit else None, rules_version=rules.version,
        offer_kind=check.offer_kind, variant_id=agent.variant_id, variants=agent.variants)


def _fallback_outcome(profile: CustomerProfile, catalog: Catalog, rules, state, now: datetime,
                      trigger_type: str, why_fallback: str) -> DecisionOutcome:
    result = fallback_decide(profile, catalog, rules, state, now, why_fallback)
    reasoning = {**result.reasoning, "fallback_rules_tried": result.tried}
    check = result.check
    if check and check.ok:
        reasoning["value"] = value_summary(check)
    return DecisionOutcome(
        profile.customer_id, profile.first_name, trigger_type, "fallback",
        _delivery_status(check, profile, now),
        check.offer if check else None,
        reasoning=reasoning,
        rule_checks=[c.model_dump() for c in check.checks] if check else [],
        expected_profit_paise=check.profit.expected_profit_paise if check and check.profit else None,
        offer_cost_paise=check.profit.offer_cost_paise if check and check.profit else None,
        rules_version=rules.version,
        proposal=result.candidate.offer.model_dump(mode="json") if result.candidate else None,
        offer_kind=check.offer_kind if check and check.ok else "normal",
    )


def _delivery_status(check: CheckResult | None, profile: CustomerProfile, now: datetime) -> str:
    """Approved offer -> scheduled (later), queued (linked tester) or would_send. No offer -> nothing."""
    if check is None or not check.ok:
        return "nothing"
    if check.offer.send_at and check.offer.send_at > now + timedelta(minutes=1):
        return "scheduled"
    return "queued" if profile.telegram_linked else "would_send"


def _save(conn: sqlite3.Connection, o: DecisionOutcome, trigger_detail: dict | None) -> int:
    offer = o.offer
    cur = conn.execute("""
        INSERT INTO decisions (customer_id, created_at, trigger_type, trigger_detail, decided_by, status, proposal,
            final_offer, message, send_at, reasoning, rule_checks, expected_profit_paise, offer_cost_paise,
            rules_version, tool_trace, input_tokens, output_tokens, cost_usd, offer_kind, variant_id, variants)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
        o.customer_id, o.created_at, o.trigger_type, json.dumps(trigger_detail) if trigger_detail else None,
        o.decided_by, o.status, json.dumps(o.proposal) if o.proposal else None,
        offer.model_dump_json() if offer else None, offer.message if offer else None,
        to_ts(offer.send_at) if offer and offer.send_at else (to_ts(o.followup_at) if o.followup_at else None),
        json.dumps(o.reasoning), json.dumps(o.rule_checks), o.expected_profit_paise, o.offer_cost_paise,
        o.rules_version, json.dumps(o.tool_trace) if o.tool_trace else None,
        o.input_tokens, o.output_tokens, o.cost_usd, o.offer_kind, o.variant_id,
        json.dumps(o.variants, ensure_ascii=False) if o.variants else None))
    conn.commit()
    return cur.lastrowid


def recheck_scheduled(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """
    Re-check every offer that is decided but not sent yet, against the CURRENT rules version.
    Called after the marketing team saves new rules (CLAUDE.md 4.0: "offers get revised when rules change").
    Still fine -> keeps its place with the new version recorded. Now breaks a rule -> repaired or cancelled.
    """
    now = now or now_utc()
    catalog, rules = load_catalog(conn), get_active_rules(conn)
    report = []
    for row in conn.execute("SELECT * FROM decisions WHERE status = 'scheduled' AND send_at > ?",
                            (to_ts(now),)).fetchall():
        offer = OfferProposal.model_validate_json(row["final_offer"])
        profile = build_profile(conn, row["customer_id"], now, catalog)
        state = load_rule_state(conn, row["customer_id"], rules, now, exclude_decision_id=row["id"])
        check = check_offer(offer, profile, catalog, rules, state, now)
        reasoning = json.loads(row["reasoning"])
        if check.ok:
            conn.execute("UPDATE decisions SET final_offer = ?, rule_checks = ?, rules_version = ?, "
                         "offer_cost_paise = ?, expected_profit_paise = ?, offer_kind = ? WHERE id = ?",
                         (check.offer.model_dump_json(), json.dumps([c.model_dump() for c in check.checks]),
                          rules.version, check.profit.offer_cost_paise, check.profit.expected_profit_paise,
                          check.offer_kind, row["id"]))
            report.append({"decision_id": row["id"], "result": "still valid", "rules_version": rules.version})
        else:
            reasoning["cancelled_note"] = (f"Cancelled before sending: rules version {rules.version} no longer allows "
                                           f"it ({'; '.join(check.failures)}).")
            conn.execute("UPDATE decisions SET status = 'cancelled', reasoning = ?, rule_checks = ?, rules_version = ? "
                         "WHERE id = ?", (json.dumps(reasoning), json.dumps([c.model_dump() for c in check.checks]),
                                          rules.version, row["id"]))
            report.append({"decision_id": row["id"], "result": "cancelled", "reasons": check.failures})
    conn.commit()
    return report


# ---------------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------------
def format_decision(o: DecisionOutcome) -> str:
    """Readable report of a decision, the same information the dashboard will show."""
    lines = [f"Customer #{o.customer_id} {o.first_name}  |  trigger: {o.trigger_type}  |  decided by: {o.decided_by}"
             f"  |  status: {o.status.upper()}  |  rules v{o.rules_version}"]
    if o.offer:
        send = to_ist(o.offer.send_at).strftime("%d %b %H:%M IST") if o.offer.send_at else "now"
        lines += [f"  OFFER    {o.offer.describe()}, expires in {o.offer.expiry_hours}h, send {send}",
                  f"  MESSAGE  {o.offer.message}"]
    r = o.reasoning
    lines.append(f"  WHY      {r.get('summary', '')}")
    for s in r.get("signals_used", []):
        lines.append(f"           - {s}")
    if r.get("expected_impact"):
        lines.append(f"  IMPACT   {r['expected_impact']}")
    if r.get("decided_by_note"):
        lines.append(f"  NOTE     {r['decided_by_note']}")
    for a in r.get("alternatives_considered", []):
        lines.append(f"  ALSO     {a[:160]}")
    if r.get("why_now"):
        lines.append(f"  WHY NOW  {r['why_now']}")
    for repair in r.get("rule_repairs", []):
        lines.append(f"  REPAIRED {repair[:160]}")
    if r.get("agent_attempt"):
        lines.append(f"  AI       tried but failed: {r['agent_attempt']['error']}")
    menu = r.get("candidates_prepared_by_code") or []
    if menu:
        allowed = [c for c in menu if c["allowed_by_rules"]]
        lines.append(f"  MENU     {len(menu)} candidate offers prepared by code ({len(allowed)} allowed): "
                     + "; ".join(f"{c['idea']} Rs {c.get('expected_profit_rupees')}" for c in allowed[:4]))
    for step in o.tool_trace:
        lines.append(f"  TOOL     {step['tool']}: {str(step.get('result', ''))[:140]}")
    if o.input_tokens is not None:
        lines.append(f"  COST     {o.input_tokens:,} input + {o.output_tokens:,} output tokens = ${o.cost_usd:.4f}")
    for c in o.rule_checks:
        if c["result"] != "passed":
            lines.append(f"  RULE     [{c['result']}] {c['rule']}: {c['detail'][:160]}")
    if o.rule_checks:
        passed = sum(1 for c in o.rule_checks if c["result"] == "passed")
        lines.append(f"  RULES    {passed}/{len(o.rule_checks)} checks passed outright")
    if o.decision_id:
        lines.append(f"  SAVED    decision #{o.decision_id}")
    return "\n".join(lines)


SHOWCASE = [
    ("habit breaking", lambda p: (p.overdue_ratio or 0) >= 1.5 and p.total_orders >= 5),
    ("cart left behind", lambda p: p.open_cart is not None),
    ("points expiring", lambda p: p.points_expiring_7d > 0),
    ("regular, on track (buys anyway?)", lambda p: p.habit_status in ("on track", "due now") and p.total_orders >= 15),
    ("new customer", lambda p: p.total_orders <= 1),
]


def pick_showcase(conn: sqlite3.Connection, catalog: Catalog, now: datetime, seed: int = 7) -> list[tuple[str, int]]:
    """Find one customer for each situation, using only observable profiles (never the hidden personas)."""
    ids = [r[0] for r in conn.execute("SELECT id FROM customers WHERE marketing_consent = 1")]
    random.Random(seed).shuffle(ids)
    found: dict[str, int] = {}
    for cid in ids[:400]:
        profile = build_profile(conn, cid, now, catalog)
        for label, matches in SHOWCASE:
            if label not in found and matches(profile):
                found[label] = cid
                break
        if len(found) == len(SHOWCASE):
            break
    return [(label, found[label]) for label, _ in SHOWCASE if label in found]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # so Rs symbol and emoji print on Windows terminals
    parser = argparse.ArgumentParser(description="Make an offer decision for one or more customers.")
    parser.add_argument("--customer", type=int, action="append", help="customer id (can repeat)")
    parser.add_argument("--showcase", action="store_true", help="one example customer per situation")
    parser.add_argument("--trigger", default="manual")
    parser.add_argument("--auto-trigger", action="store_true",
                        help="use the strongest trigger detected for each customer (cart, interest, habit break...)")
    parser.add_argument("--no-agent", action="store_true", help="force the free fallback rules")
    parser.add_argument("--dry-run", action="store_true", help="show the decision but don't save it")
    parser.add_argument("--now", type=parse_ts, help="pretend it's this time (UTC, e.g. 2026-10-23T06:30:00Z)")
    args = parser.parse_args()

    conn = connect()
    catalog = load_catalog(conn)
    now = args.now or now_utc()
    targets = [("requested", c) for c in (args.customer or [])]
    if args.showcase:
        targets += pick_showcase(conn, catalog, now)
    if not targets:
        parser.error("give --customer ID or --showcase")
    for label, cid in targets:
        trigger, detail = args.trigger, None
        if args.auto_trigger:
            found = detect_triggers(build_profile(conn, cid, now, catalog))
            if found:
                trigger, detail = found[0].type, found[0].detail
        outcome = decide(conn, cid, trigger, trigger_detail=detail, now=now, use_agent=not args.no_agent,
                         dry_run=args.dry_run, catalog=catalog)
        print(f"\n=== {label} ===")
        print(format_decision(outcome))


if __name__ == "__main__":
    main()
