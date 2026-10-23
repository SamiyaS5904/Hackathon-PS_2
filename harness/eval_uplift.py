"""
Evaluation: is the trained uplift model closer to the TRUTH than the hand-made formula?

Only the harness may read `customer_traits` (the hidden truth). Each customer has a true response per offer type
(`true_uplift`). We ask both methods to predict the uplift of a standard offer of each type for a sample of
customers, and measure how well each method RANKS customers by their true response (Spearman correlation:
1.0 = perfect order, 0 = no better than random). Ranking is what matters: the agent must pick the right customers
and the right offer type.

Run: python -m harness.eval_uplift --sample 400
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from unittest.mock import patch

from scipy.stats import spearmanr

from db.store import connect
from engine.catalog import load_catalog
from engine.clock import parse_ts
from engine.features import build_profile
from engine.models import OfferProposal
from engine.profit import estimate_uplift, formula_uplift
from engine.uplift import get_model


def standard_offer(offer_type: str, profile) -> OfferProposal:
    """One typical offer of each type, on the customer's top category."""
    cat = profile.favourite_categories[0] if profile.favourite_categories else "fashion"
    usual = max(profile.avg_order_value_rupees, 500)
    return {
        "pct_off": OfferProposal(offer_type="pct_off", amount=10, category=cat, min_order_rupees=499),
        "flat_off": OfferProposal(offer_type="flat_off", amount=round(usual * 0.1), category=cat, min_order_rupees=usual),
        "free_delivery": OfferProposal(offer_type="free_delivery", min_order_rupees=299),
        "cashback_to_wallet": OfferProposal(offer_type="cashback_to_wallet", amount=100, category=cat,
                                            min_order_rupees=799),
        "points_multiplier": OfferProposal(offer_type="points_multiplier", amount=2),
        "early_access": OfferProposal(offer_type="early_access", category=cat),
    }[offer_type]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=400)
    parser.add_argument("--now", default="2026-10-23T15:36:49Z")
    args = parser.parse_args()

    model = get_model()
    if model is None:
        raise SystemExit("No trained model. Run: python -m engine.uplift --train")
    conn = connect()
    catalog = load_catalog(conn)
    now = parse_ts(args.now)
    traits = {r["customer_id"]: (r["persona"], json.loads(r["true_uplift"]))
              for r in conn.execute("SELECT customer_id, persona, true_uplift FROM customer_traits")}
    ids = random.Random(3).sample(sorted(traits), args.sample)

    types = ["pct_off", "flat_off", "free_delivery", "cashback_to_wallet", "points_multiplier", "early_access"]
    truth, by_model, by_formula = ({t: [] for t in types} for _ in range(3))
    persona_rows: dict[str, list] = {}
    for cid in ids:
        p_model = build_profile(conn, cid, now, catalog)
        with patch("engine.uplift.get_model", return_value=None):
            p_formula = build_profile(conn, cid, now, catalog)
        persona, true_up = traits[cid]
        for t in types:
            truth[t].append(true_up[t])
            offer = standard_offer(t, p_model)
            by_model[t].append(model.p_with_offer(p_model, offer, catalog) - p_model.p_buy_48h_no_offer)
            with patch("engine.uplift.get_model", return_value=None):
                by_formula[t].append(formula_uplift(standard_offer(t, p_formula), p_formula, catalog))
        persona_rows.setdefault(persona, []).append((true_up["pct_off"], by_model["pct_off"][-1],
                                                     by_formula["pct_off"][-1]))

    print(f"\nHow well each method ranks {len(ids)} customers by their TRUE response (Spearman, 1.0 = perfect):")
    # The blend is what production uses (engine/profit.py estimate_uplift): average of formula and model
    blend = {t: [(a + b) / 2 for a, b in zip(by_formula[t], by_model[t])] for t in types}
    print(f"  {'offer type':<20}{'formula':>10}{'model':>10}{'blend':>10}")
    wins = 0
    for t in types:
        f, m, b = (spearmanr(truth[t], x[t])[0] for x in (by_formula, by_model, blend))
        wins += m > f
        best = max((f, "formula"), (m, "model"), (b, "blend"))[1]
        print(f"  {t:<20}{f:>10.2f}{m:>10.2f}{b:>10.2f}   best: {best}")
    all_truth = [x for t in types for x in truth[t]]
    f_all, m_all, b_all = (spearmanr(all_truth, [v for t in types for v in x[t]])[0]
                           for x in (by_formula, by_model, blend))
    print(f"  {'ALL (which offer for whom)':<20}{f_all:>10.2f}{m_all:>10.2f}{b_all:>10.2f}")
    print(f"\nModel better on {wins}/{len(types)} offer types. Model info: {model.info['rows_treated']} treated + "
          f"{model.info['rows_control']} holdout rows, AUC {model.info['auc_treated']} / {model.info['auc_control']}")

    print("\n10% discount, average predicted uplift per hidden persona (truth = effect if the offer is opened):")
    print(f"  {'persona':<16}{'truth':>8}{'formula':>9}{'model':>8}")
    for persona, rows in sorted(persona_rows.items(), key=lambda kv: -sum(r[0] for r in kv[1]) / len(kv[1])):
        n = len(rows)
        print(f"  {persona:<16}{sum(r[0] for r in rows) / n:>8.3f}{sum(r[2] for r in rows) / n:>9.3f}"
              f"{sum(r[1] for r in rows) / n:>8.3f}")


if __name__ == "__main__":
    main()
