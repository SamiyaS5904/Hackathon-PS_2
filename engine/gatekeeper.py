"""
Gatekeeper: free checks BEFORE the AI is called, plus "Plan your day" (the daily AI budget).

Where it sits: STEP 3b of the pipeline.
    trigger -> gatekeeper.screen() -> skip  |  agent (AI, costs money)  |  fallback (free rules)

Why it exists: every AI decision costs ~Rs 6-9. There is no point paying the AI to think about a customer who
can't receive an offer anyway (no consent, already got 2 offers this week, got one an hour ago...). And the
manager decides each day how many AI decisions to allow; once used up, the free fallback takes over.

Day Plan rules (CLAUDE.md section 3):
  - The manager sets the number of AI decisions for today and tomorrow on the dashboard (`day_plans` table).
  - Default = MAX_AGENT_RUNS_PER_DAY from .env (50). The plan can be lower, never higher than that ceiling.
  - An "AI decision" is counted whenever the agent was actually called (decisions.input_tokens is not NULL),
    even if its proposal was later rejected, because the API cost was spent.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from config.settings import settings
from engine.clock import ist_date, now_utc, start_of_ist_day, to_ts
from engine.features import CustomerProfile
from engine.rules import RuleState
from engine.rules_store import ActiveRules

# Starting estimate for one AI decision with Sonnet 5.5 + the one-call design (measured ~$0.016-0.028).
# Once real decisions exist, the Day Plan uses their actual average cost instead (average_ai_cost_usd).
EST_COST_PER_DECISION_USD = 0.02
USD_TO_INR = 88


@dataclass
class GateResult:
    route: str            # 'skip' | 'agent' | 'fallback'
    reason: str           # plain-language explanation shown on the dashboard
    priority: float       # higher = more valuable to look at first (used when many triggers arrive at once)


# ---------------------------------------------------------------------------------------------------
# Day Plan
# ---------------------------------------------------------------------------------------------------
def get_day_plan(conn: sqlite3.Connection, day: str | None = None) -> int:
    """AI decisions allowed on `day` (YYYY-MM-DD, IST). Default: the .env ceiling."""
    day = day or ist_date(now_utc())
    row = conn.execute("SELECT agent_decision_limit FROM day_plans WHERE date = ?", (day,)).fetchone()
    limit = row[0] if row else settings.max_agent_runs_per_day
    return min(limit, settings.max_agent_runs_per_day)


def set_day_plan(conn: sqlite3.Connection, day: str, limit: int) -> int:
    """Save the manager's plan for a day. Clamped to 0..ceiling; returns the value actually saved."""
    limit = max(0, min(int(limit), settings.max_agent_runs_per_day))
    ts = to_ts(now_utc())
    conn.execute("""INSERT INTO day_plans (date, agent_decision_limit, created_at, updated_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT(date) DO UPDATE SET agent_decision_limit = excluded.agent_decision_limit,
                                                    updated_at = excluded.updated_at""", (day, limit, ts, ts))
    conn.commit()
    return limit


def agent_runs_used(conn: sqlite3.Connection, now: datetime) -> int:
    """How many times the AI was called today (IST day)."""
    return conn.execute("SELECT COUNT(*) FROM decisions WHERE input_tokens IS NOT NULL AND created_at >= ? "
                        "AND created_at <= ?", (to_ts(start_of_ist_day(now)), to_ts(now))).fetchone()[0]


def average_ai_cost_usd(conn: sqlite3.Connection, last_n: int = 20) -> float:
    """Average real cost of the last AI decisions (from logged token usage), or the starting estimate."""
    rows = [r[0] for r in conn.execute("SELECT cost_usd FROM decisions WHERE cost_usd IS NOT NULL AND cost_usd > 0 "
                                       "ORDER BY id DESC LIMIT ?", (last_n,))]
    return sum(rows) / len(rows) if rows else EST_COST_PER_DECISION_USD


def estimated_cost(decisions: int, per_decision_usd: float = EST_COST_PER_DECISION_USD) -> dict:
    """Estimated API cost for a number of AI decisions, for the Day Plan screen."""
    usd = decisions * per_decision_usd
    return {"decisions": decisions, "usd": round(usd, 2), "inr": round(usd * USD_TO_INR),
            "per_decision_inr": round(per_decision_usd * USD_TO_INR, 2)}


# ---------------------------------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------------------------------
def circuit_breaker(conn: sqlite3.Connection, rules: ActiveRules, state: RuleState, now: datetime) -> str | None:
    """
    Returns a reason if ALL offers must pause right now, else None.
      - today's offer budget is fully used, or
      - more than half of the last N decisions were 'blocked' (agent proposals the rules kept rejecting):
        something is wrong, so stop and alert instead of hammering the rules.
    """
    cb = rules.budget["circuit_breaker"]
    if cb["pause_when_daily_budget_used"] and state.spent_today_paise >= rules.budget["daily_rupees"] * 100:
        return f"Today's offer budget (Rs {rules.budget['daily_rupees']:,}) is fully used."
    window = cb["blocked_rate_window"]
    recent = [r[0] for r in conn.execute("SELECT status FROM decisions WHERE created_at <= ? ORDER BY id DESC LIMIT ?",
                                         (to_ts(now), window))]
    if len(recent) >= window:
        blocked = sum(1 for s in recent if s == "blocked") / len(recent)
        if blocked > cb["blocked_rate_max"]:
            return (f"{blocked:.0%} of the last {window} decisions were blocked by the rules "
                    f"(limit {cb['blocked_rate_max']:.0%}). Offers paused until someone checks.")
    return None


# ---------------------------------------------------------------------------------------------------
# Screening
# ---------------------------------------------------------------------------------------------------
def priority_score(profile: CustomerProfile, trigger_type: str) -> float:
    """
    How valuable is it to look at this customer now? Follows the agent priorities in CLAUDE.md section 1:
    habit breaks of valuable customers first, then fresh carts, then free nudges.
    """
    score = 0.0
    overdue = profile.overdue_ratio or 0
    if overdue >= 1.3 and profile.total_orders >= 3:
        score += 50 + min(profile.total_spend_rupees / 2000, 30)       # breaking habit, more if they spend a lot
    if profile.open_cart:
        score += 30 + min(profile.open_cart["cart_value_rupees"] / 200, 15)
    if profile.points_expiring_7d or (profile.wallet_balance_rupees >= 200 and (profile.wallet_idle_days or 0) >= 30):
        score += 10
    score -= 40 * profile.p_buy_48h_no_offer                           # buys anyway -> less worth it
    if trigger_type == "manual":
        score += 100                                                    # the manager asked for this one
    return round(score, 1)


def screen(conn: sqlite3.Connection, profile: CustomerProfile, rules: ActiveRules, state: RuleState,
           now: datetime, trigger_type: str, want_agent: bool = True,
           agent_available: bool | None = None) -> GateResult:
    """
    Decide where this trigger goes: skip it, ask the AI, or use the free fallback.
    `agent_available` defaults to "is an API key configured?"; tests pass True when they supply a fake AI client.
    """
    if agent_available is None:
        agent_available = settings.agent_enabled
    priority = priority_score(profile, trigger_type)
    spam = rules.anti_spam

    # Customers who can't receive any offer right now: skip without spending anything
    if spam["respect_marketing_consent"] and not profile.marketing_consent:
        return GateResult("skip", "Customer has not agreed to receive marketing messages.", priority)
    if state.offers_last_7d >= spam["max_offers_per_7_days"]:
        return GateResult("skip", f"Already got {state.offers_last_7d} offers this week "
                                  f"(max {spam['max_offers_per_7_days']}).", priority)
    if state.hours_since_last_offer is not None and state.hours_since_last_offer < spam["min_hours_between_offers"]:
        return GateResult("skip", f"Got an offer only {state.hours_since_last_offer:g}h ago "
                                  f"(need {spam['min_hours_between_offers']}h).", priority)
    waiting = conn.execute("SELECT send_at FROM decisions WHERE customer_id = ? AND status = 'waiting' AND send_at > ? "
                           "ORDER BY send_at LIMIT 1", (profile.customer_id, to_ts(now))).fetchone()
    if waiting and trigger_type != "followup":
        return GateResult("skip", f"The AI is already waiting to see if this customer buys on their own "
                                  f"(follow-up at {waiting[0]}).", priority)
    if state.active_offers >= rules.integrity["max_active_offers_per_customer"]:
        return GateResult("skip", "Customer already has an active offer.", priority)
    paused = circuit_breaker(conn, rules, state, now)
    if paused:
        return GateResult("skip", f"Circuit breaker: {paused}", priority)

    # Agent or fallback?
    if not want_agent:
        return GateResult("fallback", "AI was switched off for this run", priority)
    if not agent_available:
        return GateResult("fallback", "no Claude API key is configured", priority)
    plan, used = get_day_plan(conn, ist_date(now)), agent_runs_used(conn, now)
    if used >= plan:
        return GateResult("fallback", f"today's AI plan is used up ({used}/{plan} AI decisions)", priority)
    return GateResult("agent", f"AI decision {used + 1} of {plan} planned for today", priority)
