"""
Guardrail report: what the rules are protecting right now, and what they have caught so far.

Where it sits: read-only, beside the pipeline. api/main.py (/api/guardrails) calls build_report(); the dashboard's
Guardrails page shows it. It makes no decisions and changes nothing; it only reads the decisions table.

What it answers for the marketing manager:
  - Is the circuit breaker about to trip? (today's budget used, share of recent decisions blocked)
  - How much of the daily / monthly offer budget is used, and which customers are closest to their monthly cap?
  - Which rules did the work? Three layers, all plain code:
      1. menu: offers code priced for the AI but blocked before the AI could pick them
      2. AI proposals the rules rejected and sent back to the AI
      3. repairs on offers that went out (e.g. send time moved out of quiet hours)
  - Why the gatekeeper skipped customers, and why the free fallback was used instead of the AI.
  - The latest harness results (harness/results/*.json), the proof that the rules hold.
Key idea: "trust me" is not enough for a manager. Every guardrail shows its count and its reason.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from config.settings import PROJECT_ROOT
from engine.clock import parse_ts, start_of_ist_day, to_ist, to_ts
from engine.features import SENT_STATUSES
from engine.gatekeeper import circuit_breaker
from engine.rules import RuleState
from engine.rules_store import ActiveRules

RESULTS_DIR = PROJECT_ROOT / "harness" / "results"

# Rule names -> words a manager understands (used for every chart and list on the page)
RULE_LABELS = {
    "profit.expected_profit": "Minimum expected profit",
    "profit.margin_floor": "Margin floor",
    "profit.category_cap": "Category discount cap",
    "profit.buys_anyway": "No discount for 'buys anyway'",
    "profit.flat_vs_min_order": "Flat discount vs min order",
    "profit.free_delivery_min_order": "Free delivery min order",
    "profit.cashback_max": "Cashback limit",
    "profit.points_max": "Points multiplier limit",
    "budget.daily": "Daily budget",
    "budget.monthly": "Monthly budget",
    "budget.per_customer": "Per-customer monthly cap",
    "anti_spam.consent": "Marketing consent",
    "anti_spam.max_per_week": "Max offers per week",
    "anti_spam.gap": "Gap between offers",
    "anti_spam.quiet_hours": "Quiet hours",
    "integrity.no_stacking": "No stacking",
    "integrity.expiry": "Expiry 2-48h",
    "integrity.product": "Product exists",
    "integrity.category": "Category exists",
    "integrity.offer_type": "Allowed offer type",
    "integrity.amount": "Valid amount",
    "integrity.target": "Valid target",
    "fairness.loyal_vs_new": "Loyal never worse than new",
    "fairness.recent_full_price": "No offer after full-price buy",
    "message.matches_offer": "Message matches offer",
    "message.honest_deadline": "Honest deadline",
    "message.verified_loyalty": "Verified loyalty claim",
    "message.no_fake_urgency": "No fake urgency",
    "message.no_unbacked_claims": "No unbacked claims",
    "message.length": "Length, no links",
}

# Menu entries store only the reason text (to keep the AI's menu short), so the rule is recognised by its wording.
_MENU_REASON_TO_RULE = [
    (re.compile(r"expected profit", re.I), "profit.expected_profit"),
    (re.compile(r"margin", re.I), "profit.margin_floor"),
    (re.compile(r"buy(s)? anyway|without an offer.*above", re.I), "profit.buys_anyway"),
    (re.compile(r"monthly cap|this customer", re.I), "budget.per_customer"),
    (re.compile(r"month", re.I), "budget.monthly"),
    (re.compile(r"budget", re.I), "budget.daily"),
    (re.compile(r"bought .* full price|full price", re.I), "fairness.recent_full_price"),
]


def _rule_of_menu_reason(text: str) -> str:
    for pattern, rule in _MENU_REASON_TO_RULE:
        if pattern.search(text):
            return rule
    return "other"


def _loads(value: str | None, default):
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def _skip_reason_group(reason: str) -> str:
    """Gatekeeper reasons contain numbers and dates; group them so the list reads as categories."""
    reason = reason.replace("Skipped before any offer was considered: ", "")
    for key, label in [("marketing", "No marketing consent"), ("offers this week", "Weekly offer limit reached"),
                       ("offer only", "Less than 24h since last offer"), ("waiting", "AI is waiting to see if they buy"),
                       ("active offer", "Already has an active offer"), ("Circuit breaker", "Circuit breaker pause")]:
        if key in reason:
            return label
    return reason[:80]


def _fallback_reason_group(note: str) -> str:
    note = note.replace("Decided by the fallback rules because: ", "").rstrip(".")
    for key, label in [("plan is used up", "Today's AI plan used up"), ("no Claude API key", "No API key"),
                       ("switched off", "Manager chose free rules"), ("rejected by the rules", "AI proposals rejected twice"),
                       ("timed out", "AI timed out"), ("timeout", "AI timed out")]:
        if key in note:
            return label
    return "AI error: " + note[:60] if note else "Unknown"


def budget_status(conn: sqlite3.Connection, rules: ActiveRules, now: datetime, top_n: int = 5) -> dict:
    """Offer money committed today / this month (full cost if every offer is used) vs the limits."""
    placeholders = ",".join("?" * len(SENT_STATUSES))
    today = to_ts(start_of_ist_day(now))
    month = to_ts(to_ist(now).replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    spent = lambda since: conn.execute(  # noqa: E731
        f"SELECT COALESCE(SUM(offer_cost_paise), 0) FROM decisions WHERE status IN ({placeholders}) AND created_at >= ?",
        (*SENT_STATUSES, since)).fetchone()[0]
    per_customer = conn.execute(
        f"""SELECT d.customer_id, c.first_name, SUM(d.offer_cost_paise) AS used FROM decisions d
            JOIN customers c ON c.id = d.customer_id
            WHERE d.status IN ({placeholders}) AND d.created_at >= ? AND d.offer_cost_paise > 0
            GROUP BY d.customer_id ORDER BY used DESC LIMIT ?""", (*SENT_STATUSES, month, top_n)).fetchall()
    b = rules.budget
    return {
        "daily": {"used_rupees": spent(today) // 100, "limit_rupees": b["daily_rupees"]},
        "monthly": {"used_rupees": spent(month) // 100, "limit_rupees": b["monthly_rupees"]},
        "per_customer_cap_rupees": b["per_customer_monthly_rupees"],
        "top_customers": [{"customer_id": r[0], "first_name": r[1], "used_rupees": r[2] // 100} for r in per_customer],
    }


def breaker_status(conn: sqlite3.Connection, rules: ActiveRules, now: datetime, daily_used_rupees: int) -> dict:
    """The two circuit-breaker gauges, plus whether it is tripped right now (same function the gatekeeper uses)."""
    cb = rules.budget["circuit_breaker"]
    window = cb["blocked_rate_window"]
    recent = [r[0] for r in conn.execute("SELECT status FROM decisions WHERE created_at <= ? ORDER BY id DESC LIMIT ?",
                                         (to_ts(now), window))]
    paused = circuit_breaker(conn, rules, RuleState(spent_today_paise=daily_used_rupees * 100), now)
    return {
        "paused": paused,
        "blocked_recent": sum(1 for s in recent if s == "blocked"),
        "recent_count": len(recent),
        "window": window,
        "blocked_rate_max": cb["blocked_rate_max"],
        "pause_when_daily_budget_used": cb["pause_when_daily_budget_used"],
    }


def rule_activity(conn: sqlite3.Connection, now: datetime, days: int = 30) -> dict:
    """Count what each rule caught in the last `days`, across the three layers (menu, AI proposals, final offer)."""
    since = to_ts(now - timedelta(days=days))
    menu_blocks, ai_rejections, repairs = Counter(), Counter(), Counter()
    fallback_reasons, skip_reasons = Counter(), Counter()
    menu_total = ai_proposals = ai_rejected = 0
    by_decider: dict[str, int] = defaultdict(int)

    rows = conn.execute("SELECT decided_by, status, reasoning, rule_checks, tool_trace FROM decisions "
                        "WHERE created_at >= ?", (since,)).fetchall()
    for decided_by, status, reasoning_json, checks_json, trace_json in rows:
        by_decider[decided_by] += 1
        reasoning = _loads(reasoning_json, {})
        # Layer 1: options code priced for the AI but blocked before it could choose them
        for option in reasoning.get("candidates_prepared_by_code", []):
            menu_total += 1
            for reason in option.get("blocked_because", [])[:1]:  # the first reason is enough to say why
                menu_blocks[_rule_of_menu_reason(reason)] += 1
        # Layer 2: AI proposals the rules rejected (each one went back to the AI with the reason)
        for step in _loads(trace_json, []):
            if step.get("tool") != "propose_offer":
                continue
            ai_proposals += 1
            result = str(step.get("result", ""))
            if result.startswith("rejected: "):
                ai_rejected += 1
                for failure in result[len("rejected: "):].split("; "):
                    rule = failure.split(":", 1)[0].strip()
                    if rule in RULE_LABELS:
                        ai_rejections[rule] += 1
        # Layer 3: safe repairs on the offer that went out
        for check in _loads(checks_json, []):
            if check.get("result") == "repaired":
                repairs[check["rule"]] += 1
        if decided_by == "fallback":
            fallback_reasons[_fallback_reason_group(reasoning.get("decided_by_note", ""))] += 1
        if decided_by == "gatekeeper":
            skip_reasons[_skip_reason_group(reasoning.get("summary", ""))] += 1

    rules_seen = set(menu_blocks) | set(ai_rejections) | set(repairs)
    by_rule = sorted(({"rule": r, "label": RULE_LABELS.get(r, "Other"), "menu_blocked": menu_blocks[r],
                       "ai_rejected": ai_rejections[r], "repaired": repairs[r]} for r in rules_seen),
                     key=lambda x: -(x["menu_blocked"] + x["ai_rejected"] + x["repaired"]))
    return {
        "days": days,
        "decisions": len(rows),
        "by_decider": dict(by_decider),
        "menu_options": menu_total,
        "menu_blocked": sum(menu_blocks.values()),
        "ai_proposals": ai_proposals,
        "ai_rejected": ai_rejected,
        "repairs": sum(repairs.values()),
        "by_rule": by_rule,
        "fallback_reasons": [{"reason": k, "count": v} for k, v in fallback_reasons.most_common()],
        "skip_reasons": [{"reason": k, "count": v} for k, v in skip_reasons.most_common()],
    }


def harness_results() -> dict:
    """Latest saved harness results (written by pytest and the two real-AI checks). Missing files -> None."""
    out = {}
    for name in ("pytest", "simulate_profit", "eval_agent_scenarios"):
        path = RESULTS_DIR / f"{name}.json"
        out[name] = _loads(path.read_text(encoding="utf-8"), None) if path.exists() else None
    return out


def save_harness_result(name: str, data: dict, now: datetime | None = None) -> None:
    """Called by the harness at the end of a run, so the dashboard can show the proof."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    data = {**data, "ran_at": to_ts(now or datetime.now().astimezone())}
    (RESULTS_DIR / f"{name}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def limits_summary(rules: ActiveRules) -> list[dict]:
    """The headline limits in force (active rules version), for the 'what is protected' list."""
    p, s = rules.profit, rules.anti_spam
    return [
        {"label": "Minimum expected profit per offer", "value": f"₹{p['min_expected_profit_rupees']}"},
        {"label": "Order margin after the offer", "value": f"≥ {p['margin_floor_pct']}%"},
        {"label": "No discount when chance to buy anyway is above", "value": f"{p['buys_anyway_threshold']:.0%}"},
        {"label": "Offers per customer", "value": f"max {s['max_offers_per_7_days']} per 7 days, "
                                                  f"{s['min_hours_between_offers']}h apart"},
        {"label": "Quiet hours (IST)", "value": f"{s['quiet_hours_start']}–{s['quiet_hours_end']}"},
    ]


def build_report(conn: sqlite3.Connection, rules: ActiveRules, now: datetime) -> dict:
    budgets = budget_status(conn, rules, now)
    return {
        "rules_version": rules.version,
        "breaker": breaker_status(conn, rules, now, budgets["daily"]["used_rupees"]),
        "budgets": budgets,
        "activity": rule_activity(conn, now),
        "limits": limits_summary(rules),
        "harness": harness_results(),
    }
