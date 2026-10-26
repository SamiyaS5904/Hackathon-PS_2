"""
The uplift model: a small machine-learning model, trained on past campaigns, that predicts
  (1) the chance a customer buys in the next 48h WITHOUT an offer, and
  (2) the chance they buy WITH a given offer.
The difference is the offer's real effect ("uplift"). This replaces the hand-made formulas in features.py/profit.py
wherever we have training data for that kind of offer.

Where it sits: used by engine/features.py (the "buys anyway" chance) and engine/profit.py (chance with an offer),
so every part of the pipeline (candidates, rules, agent, fallback) automatically uses the better numbers.
No Claude API is involved: this is ordinary scikit-learn, trained on a laptop in a few minutes.

Why past campaigns are perfect training data: in every campaign some customers got the offer (treatment) and
others were picked but NOT sent anything (holdout), at random. Comparing the two groups shows who buys BECAUSE of
an offer and who buys anyway.

How it works ("T-learner", the standard simple uplift method):
  - control model:  trained on holdout rows    -> P(buy in 48h | no offer, customer features)
  - treated model:  trained on treatment rows  -> P(buy in 48h | offer features, customer features)
  - uplift = treated - control
Customer features are taken from the analyst profile AS IT WAS just before each past offer (build_profile at
that moment), so training and live use compute features in exactly the same way.

Where we have no training data, small, documented expert adjustments are used instead:
  - 'reminder' offers never appeared in past campaigns -> the heuristic from profit.py
  - past campaigns targeted categories, not single products -> product offers get a modest boost:
    favourite item x1.2, item in an open cart x1.5, item they showed interest in (searched/viewed) x1.3

Run:
    python -m engine.uplift --train            # build training rows (parallel), train, save models/uplift.joblib
    python -m engine.uplift --train --sample 6000
Validation against the hidden truth lives in harness/eval_uplift.py (only the harness may read customer_traits).
"""

from __future__ import annotations

import argparse
import math
import os
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import joblib
import numpy as np

from config.settings import PROJECT_ROOT, settings
from data.calendar_gen import festival_name_on, is_payday, is_weekend
from engine.catalog import Catalog
from engine.features import CustomerProfile, estimate_p_buy_no_offer
from engine.models import OfferProposal

# One model per database: a real shop's model (trained on its own campaigns) never mixes with the demo model.
_DB_STEM = Path(settings.db_path).stem
MODEL_PATH = PROJECT_ROOT / "models" / ("uplift.joblib" if _DB_STEM == "offerpilot" else f"uplift_{_DB_STEM}.joblib")
MODEL_OFFER_TYPES = ["pct_off", "flat_off", "free_delivery", "cashback_to_wallet", "points_multiplier", "early_access"]
CATEGORIES = ["electronics", "fashion", "beauty", "home", "grocery"]
PRODUCT_BOOST = {"favourite": 1.2, "cart": 1.5, "interest": 1.3}

CUSTOMER_FEATURES = [
    "total_orders", "days_since_last_order", "usual_gap_days", "overdue_ratio", "habit_strength",
    "avg_order_value", "orders_last_90d", "deal_seeker_score", "discount_dependence", "sale_order_share",
    "cart_abandon_rate", "has_open_cart", "sessions_last_30d", "views_last_7d", "points_balance",
    "points_expiring", "points_redeem_rate", "wallet_balance", "offers_last_30d", "heuristic_p48",
    "is_festival", "is_payday", "is_weekend",
]
OFFER_FEATURES = [*(f"type_{t}" for t in MODEL_OFFER_TYPES), "offer_size", "target_category_share",
                  "has_category_target", "past_redeem_rate_same_type", "past_sent_same_type"]


# ---------------------------------------------------------------------------------------------------
# Features (shared by training and live use)
# ---------------------------------------------------------------------------------------------------
def customer_features(profile: CustomerProfile, when: date) -> list[float]:
    """Numbers describing the customer at one moment. Missing values get neutral defaults."""
    return [
        profile.total_orders,
        min(profile.days_since_last_order if profile.days_since_last_order is not None else 400, 400),
        profile.usual_gap_days if profile.usual_gap_days is not None else 60,
        min(profile.overdue_ratio or 0, 20),
        profile.habit_strength or 0,
        profile.avg_order_value_rupees,
        profile.orders_last_90d,
        profile.deal_seeker_score,
        profile.discount_dependence,
        profile.sale_order_share,
        profile.cart_abandon_rate,
        1.0 if profile.open_cart else 0.0,
        profile.sessions_last_30d,
        profile.views_last_7d,
        profile.points_balance,
        1.0 if profile.points_expiring_7d else 0.0,
        profile.points_redeem_rate,
        profile.wallet_balance_rupees,
        profile.offers_last_30d,
        estimate_p_buy_no_offer(profile),
        1.0 if festival_name_on(when) else 0.0,
        1.0 if is_payday(when) else 0.0,
        1.0 if is_weekend(when) else 0.0,
    ]


def offer_features(offer_type: str, amount: float, category: str | None, profile: CustomerProfile) -> list[float]:
    """Numbers describing an offer for this customer: type, size relative to their usual order, target."""
    usual = max(profile.avg_order_value_rupees, 1)
    if offer_type == "pct_off":
        size = amount / 10                              # 1.0 = a 10% discount
    elif offer_type in ("flat_off", "cashback_to_wallet"):
        size = (amount / usual) / 0.10                  # 1.0 = worth 10% of their usual order
    elif offer_type == "points_multiplier":
        size = max(amount - 1, 0)                       # 1.0 = double points
    else:
        size = 1.0
    past = profile.offer_response.get(offer_type, {})
    return [
        *(1.0 if offer_type == t else 0.0 for t in MODEL_OFFER_TYPES),
        size,
        profile.category_spend_share.get(category, 0.0) if category else 1.0,
        1.0 if category else 0.0,
        past.get("redeem_rate", 0.0),
        past.get("sent", 0),
    ]


def _when(profile: CustomerProfile) -> date:
    """The calendar date (IST) the profile describes."""
    if profile.as_of:
        return (datetime.strptime(profile.as_of, "%Y-%m-%dT%H:%M:%SZ") + timedelta(hours=5, minutes=30)).date()
    return date.today()


# ---------------------------------------------------------------------------------------------------
# Live use
# ---------------------------------------------------------------------------------------------------
class UpliftModel:
    """The two trained models plus the facts about how they were trained (shown in docs and logs)."""

    def __init__(self, control, treated, info: dict):
        self.control, self.treated, self.info = control, treated, info

    def p_no_offer(self, profile: CustomerProfile) -> float:
        x = np.array([customer_features(profile, _when(profile))])
        return float(self.control.predict_proba(x)[0, 1])

    def p_with_offer(self, profile: CustomerProfile, offer: OfferProposal, catalog: Catalog) -> float | None:
        """Chance to buy with this offer, or None if the model has no training data for this offer type."""
        if offer.offer_type not in MODEL_OFFER_TYPES:
            return None
        product = catalog.get(offer.product_id)
        category = product.category if product else offer.category
        x = np.array([customer_features(profile, _when(profile))
                      + offer_features(offer.offer_type, offer.amount, category, profile)])
        p_with = float(self.treated.predict_proba(x)[0, 1])
        if product:
            # Expert adjustment: past campaigns were category-level, so product targeting gets a modest boost
            boost = 1.0
            if any(f["product_id"] == product.id for f in profile.favourite_items):
                boost = max(boost, PRODUCT_BOOST["favourite"])
            if profile.open_cart and any(i["product_id"] == product.id for i in profile.open_cart["items"]):
                boost = max(boost, PRODUCT_BOOST["cart"])
            if any(i["product_id"] == product.id for i in profile.intent_products):
                boost = max(boost, PRODUCT_BOOST["interest"])
            p_no = profile.p_buy_48h_no_offer
            p_with = p_no + (p_with - p_no) * boost
        return min(max(p_with, 0.0), 0.95)


@lru_cache(maxsize=1)
def get_model() -> UpliftModel | None:
    """The trained model, or None if it hasn't been trained yet (then the heuristics are used)."""
    if os.getenv("USE_UPLIFT_MODEL", "1") == "0" or not MODEL_PATH.exists():
        return None
    data = joblib.load(MODEL_PATH)
    return UpliftModel(data["control"], data["treated"], data["info"])


# ---------------------------------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------------------------------
_worker: dict = {}


def _init_worker(db_path: str) -> None:
    from db.store import connect
    from engine.catalog import load_catalog

    os.environ["USE_UPLIFT_MODEL"] = "0"   # training features must come from the heuristics, never a model
    _worker["conn"] = connect(Path(db_path))
    _worker["catalog"] = load_catalog(_worker["conn"])


def _rows_for(batch: list[tuple]) -> list[tuple]:
    """Build (customer features, offer features, treated?, bought?) for a batch of past offers."""
    from engine.clock import parse_ts
    from engine.features import build_profile

    out = []
    for customer_id, offer_type, value, category, group, moment, bought in batch:
        when = parse_ts(moment) - timedelta(minutes=1)  # the profile just BEFORE the offer was sent
        profile = build_profile(_worker["conn"], customer_id, when, _worker["catalog"])
        out.append((customer_features(profile, _when(profile)),
                    offer_features(offer_type, value, category, profile), group == "treatment", bought))
    return out


def build_training_rows(db_path: Path, sample: int | None, workers: int) -> list[tuple]:
    """Every past offer (or a sample), with features as of that moment. Runs in parallel processes."""
    import random
    import sqlite3

    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT customer_id, offer_type, value, category, group_name,
               COALESCE(sent_at, strftime('%Y-%m-%dT%H:%M:%SZ', expires_at, '-48 hours')), purchased_in_window
        FROM offers_history""").fetchall()
    conn.close()
    if sample and sample < len(rows):
        holdout = [r for r in rows if r[4] == "holdout"]           # keep every holdout row: they are the scarce ones
        treatment = [r for r in rows if r[4] == "treatment"]
        rows = holdout + random.Random(7).sample(treatment, max(sample - len(holdout), 0))
    batches = [rows[i:i + 200] for i in range(0, len(rows), 200)]
    result: list[tuple] = []
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(str(db_path),)) as pool:
        for n, part in enumerate(pool.map(_rows_for, batches), start=1):
            result.extend(part)
            if n % 10 == 0:
                print(f"  {len(result):,}/{len(rows):,} past offers turned into training rows...", flush=True)
    return result


def train(rows: list[tuple]) -> UpliftModel:
    """Fit the control and treated models, check them on held-back rows, and return the model."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import train_test_split

    def fit(x: np.ndarray, y: np.ndarray, name: str):
        x_tr, x_te, y_tr, y_te = train_test_split(x, y, test_size=0.2, random_state=7, stratify=y)
        params = dict(max_depth=3, learning_rate=0.05, max_iter=300, min_samples_leaf=40, l2_regularization=1.0,
                      random_state=7)
        check = HistGradientBoostingClassifier(**params).fit(x_tr, y_tr)
        auc = roc_auc_score(y_te, check.predict_proba(x_te)[:, 1])
        final = HistGradientBoostingClassifier(**params).fit(x, y)  # refit on everything for live use
        print(f"  {name}: {len(y):,} rows, {int(y.sum()):,} bought, test AUC {auc:.3f}")
        return final, round(auc, 3)

    control_rows = [r for r in rows if not r[2]]
    treated_rows = [r for r in rows if r[2]]
    control, auc_c = fit(np.array([r[0] for r in control_rows]), np.array([r[3] for r in control_rows]),
                         "control (no offer)")
    treated, auc_t = fit(np.array([r[0] + r[1] for r in treated_rows]), np.array([r[3] for r in treated_rows]),
                         "treated (with offer)")
    info = {"trained_at": datetime.now().isoformat(timespec="seconds"), "rows_control": len(control_rows),
            "rows_treated": len(treated_rows), "auc_control": auc_c, "auc_treated": auc_t,
            "customer_features": CUSTOMER_FEATURES, "offer_features": OFFER_FEATURES}
    return UpliftModel(control, treated, info)


def main() -> None:
    from config.settings import settings

    parser = argparse.ArgumentParser(description="Train the uplift model on past campaigns.")
    parser.add_argument("--train", action="store_true")
    parser.add_argument("--sample", type=int, help="limit training rows (all holdout rows are always kept)")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    args = parser.parse_args()
    if not args.train:
        parser.error("use --train")

    started = time.time()
    print(f"Building training rows from past campaigns ({args.workers} workers)...", flush=True)
    rows = build_training_rows(settings.db_path, args.sample, args.workers)
    print(f"Training on {len(rows):,} rows...", flush=True)
    model = train(rows)
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"control": model.control, "treated": model.treated, "info": model.info}, MODEL_PATH)
    print(f"Saved {MODEL_PATH} in {time.time() - started:.0f}s. Info: {model.info['rows_control']} control rows, "
          f"{model.info['rows_treated']} treated rows, AUC control {model.info['auc_control']}, "
          f"treated {model.info['auc_treated']}")


if __name__ == "__main__":
    main()
