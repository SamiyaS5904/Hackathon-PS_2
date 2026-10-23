"""
Candidate offers: code builds and prices a short list of sensible offers BEFORE the AI is called.

Where it sits: between the gatekeeper and the agent (one-call design, CLAUDE.md section 3).
    profile + active rules -> build_candidates() -> priced, rule-checked list -> agent's first message

Why it exists (cost): in the first real runs the AI spent 2-5 API calls asking estimate_profit about ideas such as
"2x points", "free delivery", "10% off favourite category". Those ideas are predictable, and the maths is plain
code anyway. So code now prepares them up front, and the AI can usually decide in ONE call: pick the best, maybe
adjust it, write the message and the reasoning. That roughly halves the tokens per decision.

What the AI still does (the agentic part): judge which candidate fits this customer's story (habit, cart, past
reactions, searches), adjust it, write a personal message, explain the decision, or reject them all and send
nothing. It can still call any tool if it wants an idea that isn't in the list.

Every candidate goes through the real rules checker (all rules except the message, which doesn't exist yet), so
the AI sees exactly which ideas are allowed, what the rules would repair, and what each one is expected to earn.
"""

from __future__ import annotations

import math
from datetime import datetime

from engine.catalog import Catalog
from engine.features import CustomerProfile
from engine.models import CheckResult, OfferProposal
from engine.rules import RuleState, check_offer
from engine.rules_store import ActiveRules

EXPIRY_HOURS = 48
MAX_PASSING_SHOWN = 8   # best allowed candidates given to the AI
MAX_BLOCKED_SHOWN = 3   # plus a few blocked ones, so the AI knows why obvious ideas are off the table


def _ideas(profile: CustomerProfile, catalog: Catalog, rules: ActiveRules) -> list[tuple[str, OfferProposal]]:
    """
    The idea list, from cheapest to most expensive. Each idea uses this customer's own facts (favourite item,
    cart item, top category, usual order size) so the candidates are already personalised.
    """
    p = rules.profit
    caps = p["category_cap_pct"]
    ideas: list[tuple[str, OfferProposal]] = []

    def add(label: str, **fields) -> None:
        ideas.append((label, OfferProposal(expiry_hours=EXPIRY_HOURS, **fields)))

    # Free / nearly free first
    if profile.points_expiring_7d or (profile.wallet_balance_rupees >= 200):
        add("free reminder (points expiring / wallet balance)", offer_type="reminder")
    add("2x reward points", offer_type="points_multiplier", amount=2)
    if p["points_multiplier_max"] >= 3:
        add("3x reward points", offer_type="points_multiplier", amount=3)
    add("free delivery", offer_type="free_delivery", min_order_rupees=p["free_delivery_min_order_rupees"])

    top_category = profile.favourite_categories[0] if profile.favourite_categories else None
    if top_category:
        add(f"early access to {top_category}", offer_type="early_access", category=top_category)

    # Discounts on what this customer actually wants
    if profile.favourite_items:
        fav = profile.favourite_items[0]
        add(f"10% off favourite item ({fav['name']})", offer_type="pct_off",
            amount=float(min(10, caps[fav["category"]])), product_id=fav["product_id"],
            min_order_rupees=max(fav["price_rupees"], 499))
    if profile.open_cart and profile.open_cart["items"]:
        item = profile.open_cart["items"][0]
        add(f"10% off cart item ({item['name']})", offer_type="pct_off",
            amount=float(min(10, caps[item["category"]])), product_id=item["product_id"],
            min_order_rupees=item["price_rupees"])
    if top_category:
        cap = caps[top_category]
        add(f"10% off {top_category}", offer_type="pct_off", amount=float(min(10, cap)), category=top_category,
            min_order_rupees=499)
        if cap > 10:
            add(f"{min(15, cap):g}% off {top_category}", offer_type="pct_off", amount=float(min(15, cap)),
                category=top_category, min_order_rupees=999)
        # Flat amount worth ~10% of their usual order, with a minimum order that keeps it inside the cap
        usual = max(profile.avg_order_value_rupees, 500)
        flat = max(50, int(round(usual * 0.10 / 50) * 50))
        min_order = max(499, int(math.ceil(flat * 100 / cap / 100) * 100)) if cap else 0
        add(f"Rs {flat} off {top_category}", offer_type="flat_off", amount=float(flat), category=top_category,
            min_order_rupees=min_order)
        cashback = min(flat, p["cashback_max_rupees"])
        add(f"Rs {cashback} wallet cashback on {top_category}", offer_type="cashback_to_wallet",
            amount=float(cashback), category=top_category, min_order_rupees=min_order)
    return ideas


def build_candidates(profile: CustomerProfile, catalog: Catalog, rules: ActiveRules, state: RuleState,
                     now: datetime) -> list[dict]:
    """
    Price and rule-check every idea, then return the best allowed ones (by expected profit) plus a few blocked
    ones with the reason. Compact dicts, ready to put into the AI's first message.
    """
    priced: list[tuple[str, CheckResult]] = []
    for label, offer in _ideas(profile, catalog, rules):
        priced.append((label, check_offer(offer, profile, catalog, rules, state, now, check_message=False)))

    allowed = sorted((x for x in priced if x[1].ok), key=lambda x: -x[1].profit.expected_profit_paise)
    blocked = [x for x in priced if not x[1].ok]
    out = []
    for n, (label, res) in enumerate(allowed[:MAX_PASSING_SHOWN] + blocked[:MAX_BLOCKED_SHOWN], start=1):
        offer = res.offer if res.ok else None
        entry = {"id": f"C{n}", "idea": label, "allowed_by_rules": res.ok}
        if offer:
            product = catalog.get(offer.product_id)
            entry["offer"] = {"offer_type": offer.offer_type, "amount": offer.amount, "product_id": offer.product_id,
                              "product_name": product.name if product else None, "category": offer.category,
                              "min_order_rupees": offer.min_order_rupees, "expiry_hours": offer.expiry_hours}
            repairs = [c.detail for c in res.checks if c.result == "repaired" and c.rule != "anti_spam.quiet_hours"]
            if repairs:
                entry["rules_adjusted"] = repairs
        else:
            entry["blocked_because"] = [f.split(": ", 1)[-1][:160] for f in res.failures]
        if res.profit:
            entry["expected_profit_rupees"] = res.profit.expected_profit_paise // 100
            entry["chance_to_buy_48h"] = [res.profit.p_buy_no_offer, res.profit.p_buy_with_offer]
            entry["offer_cost_rupees"] = res.profit.offer_cost_paise // 100
            entry["margin_after_pct"] = res.profit.margin_after_offer_pct
        out.append(entry)
    return out
