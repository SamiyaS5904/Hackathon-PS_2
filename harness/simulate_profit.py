"""
Harness test 6: does AI + rules actually make more money than simple approaches? Judged with the hidden truth.

For a sample of customers (default 3 per hidden persona = 24) at one fixed moment, four policies decide:
  1. send to all     - everyone gets 10% off their top category (min order Rs 499): the naive marketing blast
  2. send to all, with rules - the same blast, but only where our rules allow it (rules alone, no intelligence)
  3. free rules only - the fallback rule table (no AI)
  4. AI + rules      - the real agent (Sonnet, one-call design), checked by the rules; if it fails, the fallback
Each decision is scored with harness/truth.py, using the hidden personality the agent never sees:
  - 48h profit: the TRUE expected profit of the next 48 hours vs sending nothing
  - 12-week profit: a seeded day-by-day simulation of the next 12 weeks (habit revival, discount training)
  - full-price share: of the orders in those 12 weeks, how many were bought with no coupon and not in a sale,
    next to the same share in each customer's history ("before"). Coupons that train customers to wait for
    deals show up here.
Pass bar: AI + rules beats send to all and is above 0, on 48h AND on 12-week profit.

Uses the real AI only for policy 4, and only for answers not already saved in harness/recordings/.
Run: python -m harness.simulate_profit --sample 3        (3 per persona; ~Rs 1.6 per new AI decision)
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from engine.catalog import load_catalog
from engine.fallback import fallback_decide
from engine.guardrails import save_harness_result
from engine.models import OfferProposal
from engine.rules import check_offer
from engine.rules_store import get_active_rules
from harness.agent_runs import DEFAULT_NOW, decision_context, offer_from_record, pick_sample, recorded_agent, temp_database
from harness.truth import load_traits, simulate_12_weeks, true_profit_rupees


def blast_offer(profile) -> OfferProposal:
    category = profile.favourite_categories[0] if profile.favourite_categories else "fashion"
    return OfferProposal(offer_type="pct_off", amount=10, category=category, min_order_rupees=499, expiry_hours=48,
                         message=f"Hi {profile.first_name}, enjoy 10% off {category} on orders above ₹499.")


# Cost guard: a typo like --sample 300 would mean 2,400 AI calls (~Rs 4,000). CLAUDE.md: small samples only.
MAX_NEW_CALLS = 40
EST_INR_PER_CALL = 1.7


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=3, help="customers per hidden persona")
    parser.add_argument("--no-cache", action="store_true", help="ignore saved AI answers (costs money)")
    parser.add_argument("--skip-ai", action="store_true",
                        help="free run: score only the policies without the AI (AI + rules is left out)")
    parser.add_argument("--max-new-calls", type=int, default=MAX_NEW_CALLS,
                        help=f"refuse to run if more than this many AI calls might be needed (default {MAX_NEW_CALLS})")
    args = parser.parse_args()
    worst_case = args.sample * 8  # 8 personas; saved answers make most re-runs free, but plan for the worst
    if worst_case > args.max_new_calls and not args.skip_ai:
        sys.exit(f"Stopped: --sample {args.sample} could need up to {worst_case} AI calls "
                 f"(~Rs {worst_case * EST_INR_PER_CALL:,.0f}). Use a smaller --sample, or pass "
                 f"--max-new-calls {worst_case} if you really mean it.")

    conn = temp_database()
    catalog, rules, traits, now = load_catalog(conn), get_active_rules(conn), load_traits(conn), DEFAULT_NOW
    sample = pick_sample(conn, args.sample)
    totals, totals_12w = defaultdict(float), defaultdict(float)
    by_persona: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    offers_sent = defaultdict(int)
    full_price_shares: dict[str, list[float]] = defaultdict(list)   # per policy, one share per customer
    history_shares: list[float] = []
    ai_cost, new_calls, rows = 0.0, 0, []

    for persona, ids in sample.items():
        for cid in ids:
            profile, state, trigger, _ = decision_context(conn, catalog, rules, cid, now)
            t = traits[cid]

            blast = blast_offer(profile)
            blast_ruled = check_offer(blast, profile, catalog, rules, state, now,
                                      message_renderer=lambda o, p=profile: f"Hi {p.first_name}, enjoy {o.amount:g}% "
                                                                            f"off {o.category} on orders above ₹{o.min_order_rupees}.")
            fallback = fallback_decide(profile, catalog, rules, state, now, "simulation").check
            record = ({"status": "skipped", "cached": True, "cost_usd": 0, "offer": None} if args.skip_ai else
                      recorded_agent(conn, catalog, rules, cid, now, label="simulate", use_cache=not args.no_cache))
            ai_offer = offer_from_record(record)
            if record["status"] == "failed":  # the pipeline would hand over to the fallback
                ai_offer = fallback.offer if fallback else None
            ai_cost += 0 if record["cached"] else record["cost_usd"]
            new_calls += 0 if record["cached"] else 1

            decisions = {
                "send to all": blast,
                "send to all + rules": blast_ruled.offer if blast_ruled.ok else None,
                "free rules only": fallback.offer if fallback else None,
                **({} if args.skip_ai else {"AI + rules": ai_offer}),
            }
            base = simulate_12_weeks(t, profile, None, catalog, now)   # 12 weeks with no offer
            if profile.full_price_share_90d is not None:
                history_shares.append(profile.full_price_share_90d)
            values_12w = {}
            for policy, offer in decisions.items():
                value = true_profit_rupees(t, profile, offer, catalog, now)
                run = simulate_12_weeks(t, profile, offer, catalog, now) if offer else base
                values_12w[policy] = run.margin_rupees - base.margin_rupees
                totals[policy] += value
                totals_12w[policy] += values_12w[policy]
                by_persona[persona][policy] += values_12w[policy]
                offers_sent[policy] += offer is not None
                if run.orders > 0:
                    full_price_shares[policy].append(run.full_price_orders / run.orders)
            if base.orders > 0:
                full_price_shares["send nothing"].append(base.full_price_orders / base.orders)
            rows.append((persona, cid, trigger, record["status"], ai_offer.describe() if ai_offer else "nothing",
                         true_profit_rupees(t, profile, ai_offer, catalog, now), values_12w.get("AI + rules", 0.0)))

    n = sum(len(v) for v in sample.values())
    policies = ["send to all", "send to all + rules", "free rules only"] + ([] if args.skip_ai else ["AI + rules"])
    avg = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731
    share_after = {p: round(avg(full_price_shares[p]), 3) for p in ["send nothing", *policies]}
    share_before = round(avg(history_shares), 3)
    print(f"\nTRUE profit vs sending nothing, {n} customers ({args.sample} per hidden persona), "
          f"moment {now:%d %b %Y %H:%M} UTC\n")
    print(f"  12-week profit by persona\n  {'persona':<16}" + "".join(f"{p:>22}" for p in policies))
    for persona in sample:
        print(f"  {persona:<16}" + "".join(f"{'Rs ' + format(by_persona[persona][p], ',.0f'):>22}" for p in policies))
    print(f"  {'TOTAL 12 weeks':<16}" + "".join(f"{'Rs ' + format(totals_12w[p], ',.0f'):>22}" for p in policies))
    print(f"  {'TOTAL 48 hours':<16}" + "".join(f"{'Rs ' + format(totals[p], ',.0f'):>22}" for p in policies))
    print(f"  {'offers sent':<16}" + "".join(f"{offers_sent[p]:>22}" for p in policies))
    print(f"  {'full-price share':<16}" + "".join(f"{share_after[p]:>22.0%}" for p in policies))
    print(f"\n  Full-price share of orders: history {share_before:.0%} · next 12 weeks with no offers "
          f"{share_after['send nothing']:.0%}")
    print("\n  AI decisions:")
    for persona, cid, trigger, status, what, value, value_12w in rows:
        print(f"    {persona:<15} #{cid:<5} {trigger:<16} {status:<9} {what[:50]:<50} 48h Rs {value:>5,.0f} "
              f"12w Rs {value_12w:>6,.0f}")
    beats = lambda tot: tot["AI + rules"] > tot["send to all"] and tot["AI + rules"] > 0  # noqa: E731
    if args.skip_ai:
        print("\n  Free run (--skip-ai): AI + rules not scored, results not saved for the dashboard.")
        return
    ok = beats(totals) and beats(totals_12w)
    print(f"\n  New AI calls this run: {new_calls} (${ai_cost:.3f} ~ Rs {ai_cost * 88:.0f}); the rest came from saved answers.")
    save_harness_result("simulate_profit", {
        "ok": ok, "customers": n,
        "totals_rupees": {p: round(totals[p]) for p in policies},
        "totals_12w_rupees": {p: round(totals_12w[p]) for p in policies},
        "offers_sent": {p: offers_sent[p] for p in policies},
        "full_price_share_before": share_before, "full_price_share_after": share_after,
        "ai_decisions": [{"persona": r[0], "customer_id": r[1], "trigger": r[2], "status": r[3], "offer": r[4],
                          "profit_48h": round(r[5]), "profit_12w": round(r[6])} for r in rows]})
    print(f"  RESULT: {'PASS' if ok else 'FAIL'} - AI + rules 12w Rs {totals_12w['AI + rules']:,.0f} vs send to all "
          f"Rs {totals_12w['send to all']:,.0f}; 48h Rs {totals['AI + rules']:,.0f} vs Rs {totals['send to all']:,.0f} "
          f"(must be higher on both, and above 0)")

if __name__ == "__main__":
    main()
