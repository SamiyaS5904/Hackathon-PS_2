"""
Harness test 5: does the real AI behave sensibly? 16 golden scenarios, 2 per hidden persona (pass bar 14/16).

Each scenario is a real customer whose hidden persona we know (the agent doesn't). Expected behaviour comes from
CLAUDE.md sections 1 and 6, for example: a loyal regular gets nothing or a zero-cost perk (not a discount), an
about-to-leave VIP gets a comeback offer, a cart abandoner with a fresh cart gets an offer tied to that cart (or the
AI chooses to wait), a points hoarder isn't given a big discount.
Every scenario must ALSO have complete reasoning for the manager (summary, 2+ facts, alternatives, impact, why now)
and must not have failed over to the fallback.

Cart-abandoner scenarios get a fresh cart event (in a temporary copy of the database). The other scenarios use the
same customers and moment as harness/simulate_profit.py, so their saved AI answers are reused for free.
Run: python -m harness.eval_agent_scenarios
"""

from __future__ import annotations

import sys

from engine.catalog import load_catalog
from engine.profit import estimate
from engine.rules_store import get_active_rules
from events.simulator import fire_cart
from harness.agent_runs import DEFAULT_NOW, decision_context, offer_from_record, pick_sample, recorded_agent, temp_database

DISCOUNTS = {"pct_off", "flat_off", "cashback_to_wallet", "free_delivery"}
PERKS = {"points_multiplier", "reminder", "early_access"}


def discount_pct(offer) -> float:
    """How big a discount is, as % of the minimum order (for flat/cashback) or as stated (pct_off)."""
    if offer.offer_type == "pct_off":
        return offer.amount
    if offer.offer_type in ("flat_off", "cashback_to_wallet") and offer.min_order_rupees:
        return offer.amount / offer.min_order_rupees * 100
    return 0.0


def expected(persona: str, record: dict, offer, profile, catalog) -> tuple[bool, str]:
    """The persona's golden expectation. Returns (passed?, what was expected)."""
    status = record["status"]
    sends = offer is not None
    if persona == "loyal_regular":
        return (not sends or offer.offer_type in PERKS), "nothing or a zero-cost perk, no discount (buys anyway)"
    if persona == "vip_at_risk":
        return sends, "a comeback offer (habit broken, high value)"
    if persona == "deal_hunter":
        return sends, "an offer (reacts strongly to deals)"
    if persona == "window_shopper":
        return (not sends or discount_pct(offer) <= 10), "nothing or a small offer (rarely converts)"
    if persona == "new_user":
        return (not sends or estimate(offer, profile, catalog).offer_cost_paise <= 100_00), \
            "low-risk: no offer costing more than Rs 100"
    if persona == "cart_abandoner":
        cart_ids = {i["product_id"] for i in (profile.open_cart or {}).get("items", [])}
        names = [i["name"].lower() for i in (profile.open_cart or {}).get("items", [])]
        tied = sends and (offer.offer_type == "free_delivery" or offer.product_id in cart_ids
                          or any(n in (offer.message or "").lower() for n in names))
        return (tied or status == "waiting"), "an offer tied to the fresh cart, or a deliberate wait"
    if persona == "points_hoarder":
        return (not sends or offer.offer_type not in {"pct_off", "flat_off"} or discount_pct(offer) <= 10), \
            "points / free nudge rather than a big discount"
    if persona == "festival_only":
        return (not sends or discount_pct(offer) <= 10), "no big discount outside a sale"
    return False, "unknown persona"


def reasoning_complete(record: dict) -> tuple[bool, str]:
    r = record.get("reasoning") or {}
    missing = [k for k in ("summary", "expected_impact", "why_now") if not str(r.get(k, "")).strip()]
    if len(r.get("signals_used", [])) < 2:
        missing.append("2+ signals")
    if not r.get("alternatives_considered"):
        missing.append("alternatives")
    return not missing, ("complete" if not missing else "missing " + ", ".join(missing))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    conn = temp_database()
    catalog, rules, now = load_catalog(conn), get_active_rules(conn), DEFAULT_NOW
    sample = {p: ids[:2] for p, ids in pick_sample(conn, 3).items()}  # same customers as simulate_profit

    results, new_cost, new_calls = [], 0.0, 0
    for persona, ids in sample.items():
        for cid in ids:
            label = "simulate"
            if persona == "cart_abandoner":
                fire_cart(conn, catalog, cid, now, ago_hours=3)  # a fresh cart in the temporary copy
                label = "eval-cart"
            record = recorded_agent(conn, catalog, rules, cid, now, label=label)
            new_cost += 0 if record["cached"] else record["cost_usd"]
            new_calls += 0 if record["cached"] else 1
            profile, _, trigger, _ = decision_context(conn, catalog, rules, cid, now)
            offer = offer_from_record(record)
            behaved, wanted = expected(persona, record, offer, profile, catalog)
            explained, why = reasoning_complete(record)
            ok = behaved and explained and record["status"] != "failed"
            what = offer.describe() if offer else record["status"] + (f" {record['wait_hours']}h" if record.get("wait_hours") else "")
            results.append((ok, persona, cid, trigger, what, wanted, why, record.get("error")))

    passed = sum(r[0] for r in results)
    print(f"\nAI scenario check: {passed}/{len(results)} passed (pass bar 14/16)\n")
    for ok, persona, cid, trigger, what, wanted, why, error in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {persona:<15} #{cid:<5} {trigger:<16} -> {what[:50]:<50}")
        print(f"        expected: {wanted}; reasoning {why}{'; ERROR ' + error if error else ''}")
    print(f"\n  New AI calls: {new_calls} (${new_cost:.3f} ~ Rs {new_cost * 88:.0f}); others reused saved answers.")
    print(f"  RESULT: {'PASS' if passed >= 14 else 'FAIL'}")


if __name__ == "__main__":
    main()
