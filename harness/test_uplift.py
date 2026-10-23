"""
Tests for the uplift model plumbing (engine/uplift.py). Uses a tiny model trained on random rows, so these tests
check the mechanics (features line up, unknown offer types fall back, product boosts, blending), not accuracy.
Accuracy against the hidden truth is measured by harness/eval_uplift.py.
"""

from __future__ import annotations

import random
from unittest.mock import patch

from engine.models import OfferProposal
from engine.profit import estimate_uplift, formula_uplift
from engine.uplift import CUSTOMER_FEATURES, OFFER_FEATURES, customer_features, offer_features, train
from harness.conftest import NOON_IST, make_profile


def tiny_model():
    rng = random.Random(1)
    rows = []
    for _ in range(400):
        cust = [rng.random() for _ in CUSTOMER_FEATURES]
        offer = [rng.random() for _ in OFFER_FEATURES]
        rows.append((cust, offer, rng.random() < 0.7, rng.random() < 0.3))
    return train(rows)


def test_feature_vectors_match_their_names():
    profile = make_profile()
    assert len(customer_features(profile, NOON_IST.date())) == len(CUSTOMER_FEATURES)
    assert len(offer_features("pct_off", 10, "fashion", profile)) == len(OFFER_FEATURES)


def test_model_gives_probabilities_and_skips_unknown_types(catalog):
    model = tiny_model()
    profile = make_profile()
    assert 0 <= model.p_no_offer(profile) <= 1
    p = model.p_with_offer(profile, OfferProposal(offer_type="pct_off", amount=10, category="fashion"), catalog)
    assert 0 <= p <= 0.95
    assert model.p_with_offer(profile, OfferProposal(offer_type="reminder"), catalog) is None


def test_production_uses_average_of_model_and_formula(catalog):
    model = tiny_model()
    profile = make_profile()
    offer = OfferProposal(offer_type="pct_off", amount=10, category="fashion", min_order_rupees=499)
    with patch("engine.uplift.get_model", return_value=model):
        blended = estimate_uplift(offer, profile, catalog)
    expected = (formula_uplift(offer, profile, catalog)
                + model.p_with_offer(profile, offer, catalog) - profile.p_buy_48h_no_offer) / 2
    assert abs(blended - round(min(expected, 0.6), 3)) < 1e-9


def test_without_model_the_formula_is_used(catalog):
    profile = make_profile()
    offer = OfferProposal(offer_type="points_multiplier", amount=2)
    assert estimate_uplift(offer, profile, catalog) == formula_uplift(offer, profile, catalog)
