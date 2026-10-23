"""
Fallback rules: a small, cautious rule table that decides offers WITHOUT the AI.

Where it sits: STEP 5b of the pipeline. decide.py calls it when the agent can't be used:
no API key, today's AI plan used up, API error, timeout, invalid agent output, or 2 rejected proposals.

How it works (CLAUDE.md section 4.8): candidates are built in a fixed order and the FIRST one that passes the
rules checker wins. Fallback offers go through exactly the same rules as the agent's offers.
  1. Habit break (overdue >= 1.5x usual gap) + has a favourite item -> 10% off that item (within the cap)
  2. Cart abandoned in the last 72h                                -> free delivery
  3. Inactive >= 30 days and high value                           -> 10% off their favourite category
  4. Points expiring within 7 days                                -> free reminder
  5. Wallet balance >= Rs 200 unused for 30 days                  -> free reminder
  6. Nothing fits / nothing passes the rules                      -> send nothing

Messages come from config/templates.yaml and are filled from the final checked offer, so they always match.
Reasoning is template text that names the rule that fired, so the manager still sees WHY.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache

import yaml

from config.settings import CONFIG_DIR
from engine.catalog import Catalog
from engine.clock import friendly_ist, parse_ts
from engine.features import CustomerProfile
from engine.models import CheckResult, OfferProposal
from engine.rules import RuleState, check_offer
from engine.rules_store import ActiveRules

HABIT_OVERDUE_RATIO = 1.5
COMEBACK_INACTIVE_DAYS = 30
WALLET_REMINDER_MIN_RUPEES = 200
WALLET_REMINDER_IDLE_DAYS = 30
FALLBACK_DISCOUNT_PCT = 10
HABIT_MIN_ORDER_RUPEES = 499
EXPIRY_HOURS = 24


@lru_cache(maxsize=1)
def load_templates() -> dict:
    with open(CONFIG_DIR / "templates.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass
class Candidate:
    """One fallback idea: which rule fired, the offer it suggests, and the template for its message."""

    rule_no: int
    rule_name: str
    template: str
    offer: OfferProposal
    context: dict = field(default_factory=dict)  # extra values for the template (product name, cart value...)


@dataclass
class FallbackResult:
    check: CheckResult | None        # None = send nothing
    candidate: Candidate | None
    reasoning: dict
    tried: list[dict]                # every rule considered and what happened (for the dashboard)


def build_candidates(profile: CustomerProfile, catalog: Catalog, rules: ActiveRules) -> tuple[list[Candidate], list[dict]]:
    """Walk the rule table in order. Returns the candidates that fit, plus a note for each rule that didn't."""
    caps = rules.profit["category_cap_pct"]
    fits: list[Candidate] = []
    skipped: list[dict] = []

    # 1. Habit break -> favourite item
    if (profile.overdue_ratio or 0) >= HABIT_OVERDUE_RATIO and profile.favourite_items:
        fav = profile.favourite_items[0]
        amount = float(min(FALLBACK_DISCOUNT_PCT, caps[fav["category"]]))
        # Minimum order of at least Rs 499: favourites are often cheap restock items (snacks, tea), and a discount
        # on a Rs 99 item alone can't earn the minimum profit. The discount still applies only to the favourite.
        fits.append(Candidate(1, "habit break -> favourite item", "habit_favourite",
                              OfferProposal(offer_type="pct_off", amount=amount, product_id=fav["product_id"],
                                            min_order_rupees=max(fav["price_rupees"], HABIT_MIN_ORDER_RUPEES),
                                            expiry_hours=EXPIRY_HOURS),
                              {"product_name": fav["name"]}))
    else:
        skipped.append({"rule": "1. habit break -> favourite item",
                        "why_not": "not overdue enough" if profile.favourite_items else "no favourite item"})

    # 2. Cart abandoned -> free delivery
    if profile.open_cart and profile.open_cart["items"]:
        item = profile.open_cart["items"][0]
        fits.append(Candidate(2, "cart abandoned -> free delivery", "cart_free_delivery",
                              OfferProposal(offer_type="free_delivery",
                                            min_order_rupees=rules.profit["free_delivery_min_order_rupees"],
                                            expiry_hours=EXPIRY_HOURS),
                              {"product_name": item["name"]}))
    else:
        skipped.append({"rule": "2. cart abandoned -> free delivery", "why_not": "no cart left in the last 72h"})

    # 3. Inactive high-value -> comeback on favourite category
    inactive = (profile.days_since_last_order or 0) >= COMEBACK_INACTIVE_DAYS
    if inactive and profile.high_value and profile.favourite_categories:
        category = profile.favourite_categories[0]
        fits.append(Candidate(3, "inactive high-value -> comeback", "comeback_category",
                              OfferProposal(offer_type="pct_off", amount=float(min(FALLBACK_DISCOUNT_PCT, caps[category])),
                                            category=category, min_order_rupees=499, expiry_hours=EXPIRY_HOURS)))
    else:
        skipped.append({"rule": "3. inactive high-value -> comeback",
                        "why_not": "not inactive 30+ days" if not inactive else "not a high-value customer"})

    # 4. Points expiring -> free reminder
    if profile.points_expiring_7d > 0:
        fits.append(Candidate(4, "points expiring -> reminder", "points_expiring",
                              OfferProposal(offer_type="reminder", expiry_hours=EXPIRY_HOURS)))
    else:
        skipped.append({"rule": "4. points expiring -> reminder", "why_not": "no points expiring within 7 days"})

    # 5. Idle wallet -> free reminder
    if (profile.wallet_balance_rupees >= WALLET_REMINDER_MIN_RUPEES
            and (profile.wallet_idle_days or 0) >= WALLET_REMINDER_IDLE_DAYS):
        fits.append(Candidate(5, "idle wallet -> reminder", "wallet_reminder",
                              OfferProposal(offer_type="reminder", expiry_hours=EXPIRY_HOURS)))
    else:
        skipped.append({"rule": "5. idle wallet -> reminder", "why_not": "no idle wallet balance of Rs 200+"})
    return fits, skipped


def template_values(candidate: Candidate, offer: OfferProposal, profile: CustomerProfile, now: datetime) -> dict:
    """Every value a template may use, taken from the REAL offer and profile (so text and offer always match)."""
    send_at = offer.send_at or now
    discount = (f"{offer.amount:g}%" if offer.offer_type == "pct_off"
                else f"₹{offer.amount:g}" if offer.offer_type in ("flat_off", "cashback_to_wallet") else "")
    points_date = ""
    if profile.points_next_expiry:
        points_date = parse_ts(profile.points_next_expiry).strftime("%d %b").lstrip("0")
    cart = profile.open_cart or {}
    return {
        "first_name": profile.first_name,
        "discount": discount,
        "category": offer.category or "",
        "min_order": f"₹{offer.min_order_rupees}",
        "expiry": friendly_ist(send_at + timedelta(hours=offer.expiry_hours)),
        "points": profile.points_expiring_7d,
        "points_expiry_date": points_date,
        "wallet_balance": f"₹{profile.wallet_balance_rupees}",
        "wallet_idle": f"{profile.wallet_idle_days or 0:.0f}",
        "usual_gap": f"{profile.usual_gap_days or 0:g}",
        "days_since": f"{profile.days_since_last_order or 0:.0f}",
        "cart_hours": f"{cart.get('left_hours_ago', 0):.0f}",
        "cart_value": f"₹{cart.get('cart_value_rupees', 0)}",
        **candidate.context,
    }


def fallback_decide(profile: CustomerProfile, catalog: Catalog, rules: ActiveRules, state: RuleState,
                    now: datetime, why_fallback: str) -> FallbackResult:
    """
    Try each fitting candidate through the rules checker; the first that passes wins.
    If none pass (or none fit), the decision is 'send nothing', with every reason listed.
    """
    templates = load_templates()
    candidates, tried = build_candidates(profile, catalog, rules)
    tried = [{"rule": s["rule"], "outcome": "did not apply", "detail": s["why_not"]} for s in tried]

    for candidate in candidates:
        def render(offer: OfferProposal, c: Candidate = candidate) -> str:
            return templates[c.template]["message"].format(**template_values(c, offer, profile, now))

        check = check_offer(candidate.offer, profile, catalog, rules, state, now, message_renderer=render)
        if check.ok:
            values = template_values(candidate, check.offer, profile, now)
            tried.append({"rule": f"{candidate.rule_no}. {candidate.rule_name}", "outcome": "chosen",
                          "detail": check.offer.describe()})
            tried.sort(key=_rule_no)
            reasoning = {
                "summary": templates[candidate.template]["summary"].format(**values),
                "signals_used": _signals(profile),
                "alternatives_considered": [f"{t['rule']}: {t['outcome']} ({t['detail']})"
                                            for t in tried if t["outcome"] != "chosen"],
                "expected_impact": check.profit.explanation if check.profit else "",
                "why_now": f"Fallback rule {candidate.rule_no} ({candidate.rule_name}) applies right now.",
                "decided_by_note": f"Decided by the fallback rules because: {why_fallback}.",
            }
            return FallbackResult(check, candidate, reasoning, tried)
        tried.append({"rule": f"{candidate.rule_no}. {candidate.rule_name}", "outcome": "blocked by rules",
                      "detail": "; ".join(check.failures)})

    tried.sort(key=_rule_no)
    reasoning = {
        "summary": "Send nothing: no fallback rule produced an offer that passes every business rule.",
        "signals_used": _signals(profile),
        "alternatives_considered": [f"{t['rule']}: {t['outcome']} ({t['detail']})" for t in tried],
        "expected_impact": "No offer cost. The customer keeps their normal chance to buy "
                           f"({profile.p_buy_48h_no_offer:.0%} in 48h).",
        "why_now": "Nothing worth sending at this moment.",
        "decided_by_note": f"Decided by the fallback rules because: {why_fallback}.",
    }
    return FallbackResult(None, None, reasoning, tried)


def _rule_no(entry: dict) -> int:
    """'3. inactive high-value -> comeback' -> 3, so the dashboard lists rules in table order."""
    return int(entry["rule"].split(".")[0])


def _signals(profile: CustomerProfile) -> list[str]:
    """The key facts behind a fallback decision, in the same plain style as the agent's reasoning."""
    signals = [f"Habit: {profile.habit_status}"]
    if profile.usual_gap_days:
        signals.append(f"Usually orders every {profile.usual_gap_days:g} days; last order "
                       f"{profile.days_since_last_order:.0f} days ago")
    signals.append(f"{profile.total_orders} orders, lifetime spend Rs {profile.total_spend_rupees:,}")
    signals.append(f"Chance to buy in 48h without an offer: {profile.p_buy_48h_no_offer:.0%}")
    if profile.open_cart:
        signals.append(f"Cart worth Rs {profile.open_cart['cart_value_rupees']:,} left "
                       f"{profile.open_cart['left_hours_ago']:.0f}h ago")
    if profile.points_expiring_7d:
        signals.append(f"{profile.points_expiring_7d} points expiring within 7 days")
    if profile.favourite_items:
        signals.append(f"Favourite item: {profile.favourite_items[0]['name']} "
                       f"(ordered {profile.favourite_items[0]['times_ordered']} times)")
    return signals
