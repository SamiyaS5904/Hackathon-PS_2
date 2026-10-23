"""
The rules checker: every offer (from the AI agent OR the fallback) passes through here before it can be sent.

Where it sits: STEP 5 of the pipeline.
    agent / fallback -> OfferProposal -> check_offer() -> approved / repaired / rejected -> decide.py -> bot

Why it is plain code: money limits must be 100% predictable. The same offer always gets the same answer, and
no wording in a customer's data or in the agent's output can talk its way past a limit.

Three possible answers (CLAUDE.md section 4.7):
  approved -> send as is
  repaired -> a small, safe fix was applied (e.g. 25% clamped to the 8% electronics cap, send time moved out of
              quiet hours); every fix is listed so the manager can see it
  rejected -> the reasons go back to the agent so it can try again (max 2 retries), then the fallback decides

Rule groups, checked in this order (values always come from the ACTIVE rules version):
  integrity -> consent -> anti-spam -> expiry / quiet hours -> offer-type limits -> buys-anyway -> fairness ->
  margin floor -> expected profit -> budget -> message safety
Structural problems (unknown product, missing target) stop the check at once. Everything else is collected,
so a rejected proposal comes back with the full list of reasons, not just the first one.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from engine.catalog import Catalog
from engine.clock import IST, days_between, parse_ts, start_of_ist_day, to_ist, to_ts
from engine.features import SENT_STATUSES, CustomerProfile
from engine.models import CheckResult, OfferProposal, RuleCheck
from engine.profit import estimate, load_offers_config, max_amount_for_margin_floor, target_category
from engine.rules_store import ActiveRules

DISCOUNT_TYPES = ("pct_off", "flat_off", "cashback_to_wallet")
AMOUNT_TYPES = ("pct_off", "flat_off", "cashback_to_wallet", "points_multiplier")
ZERO_COST_TYPES = ("early_access", "reminder")


# ---------------------------------------------------------------------------------------------------
# The facts the rules need beyond the profile: what has already been sent and spent
# ---------------------------------------------------------------------------------------------------
@dataclass
class RuleState:
    offers_last_7d: int = 0
    hours_since_last_offer: float | None = None
    active_offers: int = 0
    spent_today_paise: int = 0
    spent_month_paise: int = 0
    customer_spent_month_paise: int = 0
    full_price_products_7d: set[int] = field(default_factory=set)
    # best offer amount a NEW customer got recently, per (offer_type, category) -> fairness rule
    new_customer_best: dict[tuple[str, str], float] = field(default_factory=dict)


def load_rule_state(conn: sqlite3.Connection, customer_id: int, rules: ActiveRules, now: datetime,
                    exclude_decision_id: int | None = None) -> RuleState:
    """
    Read sent offers, budgets used and recent purchases from the database as of `now`.
    `exclude_decision_id` leaves one decision out: used when re-checking a scheduled offer, so it doesn't count
    against itself (it would otherwise look like 'an offer already sent').
    """
    now_ts = to_ts(now)
    placeholders = ",".join("?" * len(SENT_STATUSES))
    skip = exclude_decision_id or -1
    state = RuleState()

    # Offers to THIS customer: count, last time, and which are still active (not expired yet)
    sent_times: list[datetime] = []
    for r in conn.execute(f"""
            SELECT final_offer, COALESCE(send_at, created_at) t, offer_cost_paise, created_at FROM decisions
            WHERE customer_id = ? AND status IN ({placeholders}) AND created_at <= ? AND id != ?""",
                          (customer_id, *SENT_STATUSES, now_ts, skip)):
        t = parse_ts(r["t"])
        sent_times.append(t)
        offer = json.loads(r["final_offer"]) if r["final_offer"] else {}
        if t + timedelta(hours=offer.get("expiry_hours", 24)) > now:
            state.active_offers += 1
        if r["created_at"] >= _month_start(now):
            state.customer_spent_month_paise += r["offer_cost_paise"] or 0
    for r in conn.execute("SELECT sent_at, expires_at FROM offers_history WHERE customer_id = ? AND "
                          "group_name = 'treatment' AND sent_at <= ?", (customer_id, now_ts)):
        sent_times.append(parse_ts(r["sent_at"]))
        if parse_ts(r["expires_at"]) > now:
            state.active_offers += 1
    state.offers_last_7d = sum(1 for t in sent_times if 0 <= days_between(t, now) <= 7)
    past = [t for t in sent_times if t <= now]
    state.hours_since_last_offer = round(days_between(max(past), now) * 24, 1) if past else None

    # Company-wide budget used today and this month
    today = to_ts(start_of_ist_day(now))
    state.spent_today_paise = conn.execute(
        f"SELECT COALESCE(SUM(offer_cost_paise), 0) FROM decisions WHERE status IN ({placeholders}) "
        f"AND created_at >= ? AND created_at <= ? AND id != ?", (*SENT_STATUSES, today, now_ts, skip)).fetchone()[0]
    state.spent_month_paise = conn.execute(
        f"SELECT COALESCE(SUM(offer_cost_paise), 0) FROM decisions WHERE status IN ({placeholders}) "
        f"AND created_at >= ? AND created_at <= ? AND id != ?", (*SENT_STATUSES, _month_start(now), now_ts, skip)
    ).fetchone()[0]

    # Fairness inputs
    week_ago = to_ts(now - timedelta(days=rules.fairness["no_offer_after_full_price_days"]))
    state.full_price_products_7d = {r[0] for r in conn.execute("""
        SELECT oi.product_id FROM order_items oi JOIN orders o ON o.id = oi.order_id
        WHERE o.customer_id = ? AND o.ts BETWEEN ? AND ? AND o.discount_paise = 0 AND o.status != 'cancelled'""",
                                                                 (customer_id, week_ago, now_ts))}
    since = to_ts(now - timedelta(days=rules.fairness["loyal_never_worse_than_new_days"]))
    for r in conn.execute(f"""
            SELECT d.final_offer FROM decisions d
            WHERE d.status IN ({placeholders}) AND d.created_at BETWEEN ? AND ? AND d.customer_id != ?
              AND (SELECT COUNT(*) FROM orders o WHERE o.customer_id = d.customer_id AND o.status != 'cancelled') < 2""",
                          (*SENT_STATUSES, since, now_ts, customer_id)):
        offer = json.loads(r["final_offer"])
        key = (offer["offer_type"], offer.get("category") or "")
        state.new_customer_best[key] = max(state.new_customer_best.get(key, 0), offer.get("amount", 0))
    return state


def _month_start(now: datetime) -> str:
    local = to_ist(now)
    return to_ts(local.replace(day=1, hour=0, minute=0, second=0, microsecond=0))


# ---------------------------------------------------------------------------------------------------
# The checker
# ---------------------------------------------------------------------------------------------------
class _Report:
    """Collects one RuleCheck per rule, in order."""

    def __init__(self) -> None:
        self.checks: list[RuleCheck] = []

    def passed(self, rule: str, detail: str) -> None:
        self.checks.append(RuleCheck(rule=rule, result="passed", detail=detail))

    def repaired(self, rule: str, detail: str) -> None:
        self.checks.append(RuleCheck(rule=rule, result="repaired", detail=detail))

    def failed(self, rule: str, detail: str) -> None:
        self.checks.append(RuleCheck(rule=rule, result="failed", detail=detail))

    @property
    def has_failure(self) -> bool:
        return any(c.result == "failed" for c in self.checks)


def check_offer(
    proposal: OfferProposal,
    profile: CustomerProfile,
    catalog: Catalog,
    rules: ActiveRules,
    state: RuleState,
    now: datetime,
    message_renderer: Callable[[OfferProposal], str] | None = None,
    check_message: bool = True,
) -> CheckResult:
    """
    Check one proposal against every rule. Returns the final (possibly repaired) offer, the full list of checks,
    and the profit estimate.

    `message_renderer` is used by the fallback: its message is a template filled from the offer, so after any
    repair (e.g. amount lowered) the message is re-rendered and always matches. The agent writes its own
    message, so for the agent a repaired amount means the message no longer matches -> rejected, and the agent
    is asked to rewrite it.

    `check_message=False` skips only the message rules: used to pre-price candidate offers (engine/candidates.py)
    before any message exists. A real proposal is always checked in full.
    """
    report = _Report()
    offer = proposal.model_copy(deep=True)
    p, cat_cfg = rules.profit, load_offers_config()["offer_types"]

    def result(status: str, profit=None) -> CheckResult:
        return CheckResult(status=status, offer=offer if status != "rejected" else None, checks=report.checks,
                           profit=profit, rules_version=rules.version)

    # 1. INTEGRITY (structural: stop at the first problem, nothing else can be checked without these)
    if offer.offer_type not in cat_cfg:
        report.failed("integrity.offer_type", f"'{offer.offer_type}' is not an allowed offer type.")
        return result("rejected")
    product = catalog.get(offer.product_id)
    if offer.product_id is not None and product is None:
        report.failed("integrity.product", f"Product #{offer.product_id} does not exist in the catalog.")
        return result("rejected")
    if product:
        offer.category = product.category  # a product offer always belongs to the product's category
    if offer.category is not None and offer.category not in catalog.categories:
        report.failed("integrity.category", f"Category '{offer.category}' does not exist in the catalog.")
        return result("rejected")
    if cat_cfg[offer.offer_type]["needs_target"] and not (product or offer.category):
        report.failed("integrity.target", f"A {offer.offer_type} offer must name a product or a category.")
        return result("rejected")
    if offer.offer_type in AMOUNT_TYPES and offer.amount <= 0:
        report.failed("integrity.amount", f"A {offer.offer_type} offer needs an amount above 0.")
        return result("rejected")
    report.passed("integrity", f"Valid {offer.offer_type} offer on {_target_text(offer, product)}.")

    # 2. CONSENT
    if rules.anti_spam["respect_marketing_consent"] and not profile.marketing_consent:
        report.failed("anti_spam.consent", "Customer has not agreed to receive marketing messages.")
    else:
        report.passed("anti_spam.consent", "Customer agreed to receive offers.")

    # 3. ANTI-SPAM AND STACKING
    s = rules.anti_spam
    if state.offers_last_7d >= s["max_offers_per_7_days"]:
        report.failed("anti_spam.max_per_week",
                      f"Already got {state.offers_last_7d} offers in the last 7 days (max {s['max_offers_per_7_days']}).")
    else:
        report.passed("anti_spam.max_per_week", f"{state.offers_last_7d} offers in the last 7 days "
                                                f"(max {s['max_offers_per_7_days']}).")
    if state.hours_since_last_offer is not None and state.hours_since_last_offer < s["min_hours_between_offers"]:
        report.failed("anti_spam.gap", f"Last offer was only {state.hours_since_last_offer:g}h ago "
                                       f"(need {s['min_hours_between_offers']}h).")
    else:
        report.passed("anti_spam.gap", "Enough time since the last offer.")
    if state.active_offers >= rules.integrity["max_active_offers_per_customer"]:
        report.failed("integrity.no_stacking", "Customer already has an active offer; offers can't be stacked.")
    else:
        report.passed("integrity.no_stacking", "No other active offer.")

    # 4. EXPIRY AND QUIET HOURS (repairs)
    lo, hi = rules.integrity["min_expiry_hours"], rules.integrity["max_expiry_hours"]
    if not lo <= offer.expiry_hours <= hi:
        fixed = min(max(offer.expiry_hours, lo), hi)
        report.repaired("integrity.expiry", f"Expiry {offer.expiry_hours}h changed to {fixed}h (allowed {lo}-{hi}h).")
        offer.expiry_hours = fixed
    else:
        report.passed("integrity.expiry", f"Expiry {offer.expiry_hours}h is within {lo}-{hi}h.")
    send_at = offer.send_at or now
    moved = _out_of_quiet_hours(send_at, s["quiet_hours_start"], s["quiet_hours_end"])
    if moved != send_at:
        report.repaired("anti_spam.quiet_hours",
                        f"{to_ist(send_at):%H:%M} IST is in quiet hours ({s['quiet_hours_start']}-"
                        f"{s['quiet_hours_end']}); send time moved to {to_ist(moved):%d %b %H:%M} IST.")
    else:
        report.passed("anti_spam.quiet_hours", f"Send time {to_ist(send_at):%H:%M} IST is outside quiet hours.")
    offer.send_at = moved

    # 5. LIMITS PER OFFER TYPE (repairs)
    category = target_category(offer, catalog)
    cap = p["category_cap_pct"][category] if category else min(p["category_cap_pct"].values())
    cap_text = f"{category} cap {cap:g}%" if category else f"lowest category cap {cap:g}% (whole-order offer)"
    t = offer.offer_type
    if t == "pct_off":
        if offer.amount > cap:
            report.repaired("profit.category_cap", f"{offer.amount:g}% is above the {cap_text}; lowered to {cap:g}%.")
            offer.amount = float(cap)
        else:
            report.passed("profit.category_cap", f"{offer.amount:g}% is within the {cap_text}.")
    elif t in ("flat_off", "cashback_to_wallet"):
        if t == "cashback_to_wallet" and offer.amount > p["cashback_max_rupees"]:
            report.repaired("profit.cashback_max", f"Cashback Rs {offer.amount:g} lowered to the max "
                                                   f"Rs {p['cashback_max_rupees']}.")
            offer.amount = float(p["cashback_max_rupees"])
        if offer.min_order_rupees == 0:
            needed = math.ceil(offer.amount / (cap / 100)) if cap else 0
            report.repaired("profit.flat_vs_min_order",
                            f"Rs {offer.amount:g} off needs a minimum order: set to Rs {needed} so it stays within "
                            f"the {cap_text}.")
            offer.min_order_rupees = needed
        elif offer.amount > offer.min_order_rupees * cap / 100:
            allowed = math.floor(offer.min_order_rupees * cap / 100)
            report.repaired("profit.flat_vs_min_order",
                            f"Rs {offer.amount:g} is more than {cap:g}% of the Rs {offer.min_order_rupees} minimum "
                            f"order; lowered to Rs {allowed}.")
            offer.amount = float(allowed)
        else:
            report.passed("profit.flat_vs_min_order", f"Rs {offer.amount:g} is within {cap:g}% of the minimum order.")
        if offer.amount <= 0:
            report.failed("profit.flat_vs_min_order", "After applying the cap, nothing is left to offer.")
    elif t == "free_delivery":
        need = p["free_delivery_min_order_rupees"]
        if offer.min_order_rupees < need:
            report.repaired("profit.free_delivery_min_order",
                            f"Free delivery minimum order raised from Rs {offer.min_order_rupees} to Rs {need}.")
            offer.min_order_rupees = need
        else:
            report.passed("profit.free_delivery_min_order", f"Minimum order Rs {offer.min_order_rupees} >= Rs {need}.")
    elif t == "points_multiplier":
        if offer.amount > p["points_multiplier_max"]:
            report.repaired("profit.points_max", f"{offer.amount:g}x points lowered to the max "
                                                 f"{p['points_multiplier_max']}x.")
            offer.amount = float(p["points_multiplier_max"])
        elif offer.amount <= 1:
            report.failed("profit.points_max", f"{offer.amount:g}x points is not a bonus (must be above 1x).")
        else:
            report.passed("profit.points_max", f"{offer.amount:g}x points is within the max "
                                               f"{p['points_multiplier_max']}x.")

    # 6. BUYS ANYWAY: no discounts for people who'd very likely buy without one
    p_no = profile.p_buy_48h_no_offer
    allowed_perks = set(p["perks_allowed_for_buys_anyway"]) | set(ZERO_COST_TYPES)
    if p_no > p["buys_anyway_threshold"] and t not in allowed_perks:
        report.failed("profit.buys_anyway",
                      f"{p_no:.0%} chance they buy in 48h without any offer (threshold "
                      f"{p['buys_anyway_threshold']:.0%}); a {t} would only give away margin. "
                      f"Only {', '.join(sorted(allowed_perks))} are allowed.")
    else:
        report.passed("profit.buys_anyway", f"{p_no:.0%} chance to buy without an offer (threshold "
                                            f"{p['buys_anyway_threshold']:.0%}).")

    # 7. FAIRNESS
    f = rules.fairness
    if offer.product_id in state.full_price_products_7d:
        report.failed("fairness.recent_full_price",
                      f"They bought this product at full price in the last {f['no_offer_after_full_price_days']} "
                      f"days; a discount now would feel unfair.")
    else:
        report.passed("fairness.recent_full_price", "No recent full-price purchase of this product.")
    if t in DISCOUNT_TYPES and profile.total_orders >= f["loyal_min_orders"]:
        best_new = state.new_customer_best.get((t, offer.category or ""), 0)
        if offer.amount < best_new:
            report.repaired("fairness.loyal_vs_new",
                            f"A new customer recently got {best_new:g} on the same offer; this loyal customer "
                            f"({profile.total_orders} orders) is raised from {offer.amount:g} to {best_new:g}.")
            offer.amount = float(best_new)
        else:
            report.passed("fairness.loyal_vs_new", "Not worse than what new customers got recently.")

    # 8. MARGIN FLOOR (repair by lowering the amount if that fixes it)
    floor = p["margin_floor_pct"]
    est = estimate(offer, profile, catalog)
    if est.margin_after_offer_pct < floor:
        best = max_amount_for_margin_floor(offer, profile, catalog, floor)
        if t in AMOUNT_TYPES and best > (1 if t == "points_multiplier" else 0) and best < offer.amount:
            new_amount = math.floor(best) if t != "points_multiplier" else best
            report.repaired("profit.margin_floor",
                            f"Margin after the offer would be {est.margin_after_offer_pct:.1f}% (floor {floor:g}%); "
                            f"amount lowered from {offer.amount:g} to {new_amount:g}.")
            offer.amount = float(new_amount)
            est = estimate(offer, profile, catalog)
        if est.margin_after_offer_pct < floor or offer.amount <= 0 and t in AMOUNT_TYPES:
            report.failed("profit.margin_floor", f"Margin after the offer is {est.margin_after_offer_pct:.1f}%, below "
                                                 f"the {floor:g}% floor, and lowering the amount can't fix it.")
    else:
        report.passed("profit.margin_floor", f"Margin after the offer: {est.margin_after_offer_pct:.1f}% "
                                             f"(floor {floor:g}%).")

    # 9. EXPECTED PROFIT (the heart of 'never lose money')
    min_profit = p["min_expected_profit_rupees"] * 100
    if est.expected_profit_paise < min_profit:
        report.failed("profit.expected_profit",
                      f"Expected profit Rs {est.expected_profit_paise / 100:,.0f} is below the minimum "
                      f"Rs {min_profit // 100}. {est.explanation}")
    else:
        report.passed("profit.expected_profit", f"Expected profit Rs {est.expected_profit_paise / 100:,.0f} "
                                                f"(min Rs {min_profit // 100}). {est.explanation}")

    # 10. BUDGET
    b, cost = rules.budget, est.offer_cost_paise
    for rule, used, limit, label in (
        ("budget.daily", state.spent_today_paise, b["daily_rupees"] * 100, "today's offer budget"),
        ("budget.monthly", state.spent_month_paise, b["monthly_rupees"] * 100, "this month's offer budget"),
        ("budget.per_customer", state.customer_spent_month_paise, b["per_customer_monthly_rupees"] * 100,
         "this customer's monthly cap"),
    ):
        if used + cost > limit:
            report.failed(rule, f"Offer cost Rs {cost / 100:,.0f} would exceed {label} "
                                f"(used Rs {used / 100:,.0f} of Rs {limit / 100:,.0f}).")
        else:
            report.passed(rule, f"Rs {cost / 100:,.0f} fits {label} (used Rs {used / 100:,.0f} of "
                                f"Rs {limit / 100:,.0f}).")

    # 11. MESSAGE SAFETY
    if message_renderer:
        offer.message = message_renderer(offer)
    if check_message:
        for rule, ok, detail in _message_checks(offer, product, profile, rules):
            (report.passed if ok else report.failed)(rule, detail)

    if report.has_failure:
        return result("rejected", est)
    status = "repaired" if any(c.result == "repaired" for c in report.checks) else "approved"
    return result(status, est)


# ---------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------
def _target_text(offer: OfferProposal, product) -> str:
    if product:
        return f"'{product.name}'"
    return offer.category or "any order"


def _out_of_quiet_hours(send_at: datetime, start: str, end: str) -> datetime:
    """If send_at falls in quiet hours (e.g. 22:00-08:00 IST), move it to the end of the quiet period."""
    local = to_ist(send_at)
    q_start = time.fromisoformat(start)
    q_end = time.fromisoformat(end)
    now_t = local.time()
    crosses_midnight = q_start > q_end
    in_quiet = (now_t >= q_start or now_t < q_end) if crosses_midnight else (q_start <= now_t < q_end)
    if not in_quiet:
        return send_at
    day = local.date() + timedelta(days=1) if (crosses_midnight and now_t >= q_start) else local.date()
    return datetime.combine(day, q_end, tzinfo=IST).astimezone(send_at.tzinfo)


_RUPEES = re.compile(r"(?:₹|\brs\.?|\binr)\s?([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s?%")
_HOURS = re.compile(r"(\d+)\s?(?:hours|hour|hrs|hr)\b", re.IGNORECASE)
_LINK = re.compile(r"(https?://|www\.|t\.me/|\.com\b|\.in\b|bit\.ly)", re.IGNORECASE)


def _message_checks(offer: OfferProposal, product, profile: CustomerProfile, rules: ActiveRules):
    """
    Message rules (CLAUDE.md 4.6). Yields (rule, passed?, detail).
    'Must match the offer' means: every % and every rupee amount in the text is a real number of THIS offer
    (amount, minimum order, or the customer's real wallet balance), the named product appears, and any
    'N hours' equals the real expiry. That stops both mistakes and tricks.
    """
    m = rules.message
    text = offer.message.strip()
    low = text.lower()
    if not text:
        yield "message.present", False, "The offer has no message."
        return
    yield ("message.length", len(text) <= m["max_chars"], f"{len(text)} characters (max {m['max_chars']}).")
    if not m["allow_links"]:
        yield "message.no_links", not _LINK.search(text), "Links are not allowed." if _LINK.search(text) else "No links."
    hits = [phrase for phrase in m["banned_phrases"] if phrase in low]
    yield ("message.no_fake_urgency", not hits,
           f"Fake urgency/scarcity not allowed: {', '.join(hits)}." if hits else "No fake urgency.")
    claims = [phrase for phrase in m.get("banned_claims", []) if phrase in low]
    yield ("message.no_unbacked_claims", not claims,
           f"Claims we can't back up: {', '.join(claims)}." if claims else "No unbacked claims.")
    if not m["must_match_offer"]:
        return

    problems: list[str] = []
    t = offer.offer_type
    percents = {float(x) for x in _PERCENT.findall(text)}
    if t == "pct_off" and offer.amount not in percents:
        problems.append(f"message must state the real discount {offer.amount:g}%")
    wrong_pct = {x for x in percents if not (t == "pct_off" and x == offer.amount)}
    if wrong_pct:
        problems.append(f"message mentions {', '.join(f'{x:g}%' for x in sorted(wrong_pct))} but the offer is "
                        f"{offer.describe()}")
    rupees = {float(x.replace(",", "")) for x in _RUPEES.findall(text)}
    allowed = {float(offer.min_order_rupees), float(profile.wallet_balance_rupees)}
    if t in ("flat_off", "cashback_to_wallet"):
        allowed.add(float(offer.amount))
        if offer.amount not in rupees:
            problems.append(f"message must state the real amount Rs {offer.amount:g}")
    wrong_rs = rupees - allowed
    if wrong_rs:
        problems.append(f"message mentions Rs {', '.join(f'{x:g}' for x in sorted(wrong_rs))}, which is not part "
                        f"of this offer")
    if product and product.name.lower() not in low and product.subcategory.lower() not in low:
        problems.append(f"message must name the product ('{product.name}')")
    if t == "points_multiplier":
        words = {2: "double", 3: "triple"}
        if f"{offer.amount:g}x" not in low and words.get(int(offer.amount), "~") not in low:
            problems.append(f"message must state {offer.amount:g}x points")
    hours = {int(h) for h in _HOURS.findall(text)}
    if hours and hours != {offer.expiry_hours}:
        problems.append(f"message says {', '.join(map(str, sorted(hours)))} hours but the offer expires in "
                        f"{offer.expiry_hours} hours")
    joined = "; ".join(problems)
    yield ("message.matches_offer", not problems,
           joined[:1].upper() + joined[1:] + "." if problems else "Message matches the offer exactly.")
