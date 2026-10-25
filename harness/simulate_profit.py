"""
Harness test 6: does AI + rules actually make more money than simple approaches? Judged with the hidden truth.

For a sample of customers (default 3 per hidden persona = 24) at one fixed moment, four policies decide:
  1. send to all     - everyone gets 10% off their top category (min order Rs 499): the naive marketing blast
  2. send to all, with rules - the same blast, but only where our rules allow it (rules alone, no intelligence)
  3. free rules only - the fallback rule table (no AI)
  4. AI + rules      - the real agent (Sonnet, one-call design), checked by the rules; if it fails, the fallback
Each decision is scored with harness/truth.py: the TRUE expected profit vs sending nothing, using the hidden
personality the agent never sees. Pass bar (CLAUDE.md section 6): AI + rules > send to all, and > 0.

Uses the real AI only for policy 4, and only for answers not already saved in harness/recordings/.
Run: python -m harness.simulate_profit --sample 3        (3 per persona; ~Rs 1.6 per new AI decision)
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from engine.catalog import load_catalog
from engine.fallback import fallback_decide
from engine.models import OfferProposal
from engine.rules import check_offer
from engine.rules_store import get_active_rules
from harness.agent_runs import DEFAULT_NOW, decision_context, offer_from_record, pick_sample, recorded_agent, temp_database
from harness.truth import load_traits, true_profit_rupees


def blast_offer(profile) -> OfferProposal:
    category = profile.favourite_categories[0] if profile.favourite_categories else "fashion"
    return OfferProposal(offer_type="pct_off", amount=10, category=category, min_order_rupees=499, expiry_hours=48,
                         message=f"Hi {profile.first_name}, enjoy 10% off {category} on orders above ₹499.")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=3, help="customers per hidden persona")
    parser.add_argument("--no-cache", action="store_true", help="ignore saved AI answers (costs money)")
    args = parser.parse_args()

    conn = temp_database()
    catalog, rules, traits, now = load_catalog(conn), get_active_rules(conn), load_traits(conn), DEFAULT_NOW
    sample = pick_sample(conn, args.sample)
    totals = defaultdict(float)
    by_persona: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    offers_sent = defaultdict(int)
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
            record = recorded_agent(conn, catalog, rules, cid, now, label="simulate", use_cache=not args.no_cache)
            ai_offer = offer_from_record(record)
            if record["status"] == "failed":  # the pipeline would hand over to the fallback
                ai_offer = fallback.offer if fallback else None
            ai_cost += 0 if record["cached"] else record["cost_usd"]
            new_calls += 0 if record["cached"] else 1

            decisions = {
                "send to all": blast,
                "send to all + rules": blast_ruled.offer if blast_ruled.ok else None,
                "free rules only": fallback.offer if fallback else None,
                "AI + rules": ai_offer,
            }
            for policy, offer in decisions.items():
                value = true_profit_rupees(t, profile, offer, catalog, now)
                totals[policy] += value
                by_persona[persona][policy] += value
                offers_sent[policy] += offer is not None
            rows.append((persona, cid, trigger, record["status"], ai_offer.describe() if ai_offer else "nothing",
                         true_profit_rupees(t, profile, ai_offer, catalog, now)))

    n = sum(len(v) for v in sample.values())
    policies = ["send to all", "send to all + rules", "free rules only", "AI + rules"]
    print(f"\nTRUE expected profit vs sending nothing, {n} customers ({args.sample} per hidden persona), "
          f"moment {now:%d %b %Y %H:%M} UTC\n")
    print(f"  {'persona':<16}" + "".join(f"{p:>22}" for p in policies))
    for persona in sample:
        print(f"  {persona:<16}" + "".join(f"{'Rs ' + format(by_persona[persona][p], ',.0f'):>22}" for p in policies))
    print(f"  {'TOTAL':<16}" + "".join(f"{'Rs ' + format(totals[p], ',.0f'):>22}" for p in policies))
    print(f"  {'offers sent':<16}" + "".join(f"{offers_sent[p]:>22}" for p in policies))
    print("\n  AI decisions:")
    for persona, cid, trigger, status, what, value in rows:
        print(f"    {persona:<15} #{cid:<5} {trigger:<16} {status:<9} {what[:55]:<55} true Rs {value:,.0f}")
    ok = totals["AI + rules"] > totals["send to all"] and totals["AI + rules"] > 0
    print(f"\n  New AI calls this run: {new_calls} (${ai_cost:.3f} ~ Rs {ai_cost * 88:.0f}); the rest came from saved answers.")
    print(f"  RESULT: {'PASS' if ok else 'FAIL'} - AI + rules Rs {totals['AI + rules']:,.0f} vs send to all "
          f"Rs {totals['send to all']:,.0f} (must be higher, and above 0)")


if __name__ == "__main__":
    main()
