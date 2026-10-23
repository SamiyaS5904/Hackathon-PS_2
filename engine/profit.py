"""
Profit math: "will this offer make the company money, compared to sending nothing?"

Where it sits: called by rules.py (profit protection rules), by the agent's `estimate_profit` tool (Session 3)
and by the fallback. The LLM NEVER does this math; it only reads the result (CLAUDE.md non-negotiable rule).

The formula (CLAUDE.md section 4.1):

    expected_profit = P(buy | offer) x margin_after_offer  -  P(buy | no offer) x normal_margin

  - First part: what we expect to earn if we send the offer (higher chance to buy, smaller margin).
  - Second part: what we would have earned anyway by sending nothing.
  - If the customer buys anyway (P(buy | no offer) is high), the second part is big and the result goes
    negative -> the offer would only give margin away.

How the two chances are estimated (simple, explainable heuristics for the MVP):
  - P(buy | no offer) comes from the analyst: `profile.p_buy_48h_no_offer`.
  - The UPLIFT of an offer (how much it raises the chance) starts from a base per offer type and is scaled by
    observable signals: deal seeking, a breaking habit, an open cart, favourite items, how this customer
    reacted to the same offer type before, and the size of the offer. It shrinks for people who'd buy anyway.
  When the uplift model is trained (python -m engine.uplift --train), it replaces the heuristic for every offer
  type it has data on; the heuristic stays as the fallback (e.g. 'reminder', or before the model is trained).
"""

from __future__ import annotations

import math
from functools import lru_cache

import yaml

from config.settings import CONFIG_DIR
from engine.catalog import Catalog
from engine.features import CustomerProfile
from engine.models import OfferProposal, ProfitEstimate

# Starting uplift per offer type: extra chance to buy within 48h for an "average" customer.
BASE_UPLIFT: dict[str, float] = {
    "pct_off": 0.12,             # for a 10% discount
    "flat_off": 0.10,            # for a flat amount worth ~10% of their usual order
    "cashback_to_wallet": 0.09,  # same, but paid later -> slightly weaker
    "free_delivery": 0.06,
    "points_multiplier": 0.04,   # for 2x points
    "early_access": 0.04,
    "reminder": 0.03,
}
MAX_UPLIFT = 0.60
MAX_P_BUY = 0.95


@lru_cache(maxsize=1)
def load_offers_config() -> dict:
    """config/offers.yaml (offer types and shared cost numbers), read once."""
    with open(CONFIG_DIR / "offers.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------------------------------
# What order are we talking about?
# ---------------------------------------------------------------------------------------------------
def target_category(offer: OfferProposal, catalog: Catalog) -> str | None:
    """The category an offer applies to: the product's category, the named category, or None (whole order)."""
    product = catalog.get(offer.product_id)
    return product.category if product else offer.category


def expected_order(offer: OfferProposal, profile: CustomerProfile, catalog: Catalog) -> tuple[int, float, int]:
    """
    Expected order this offer leads to: (order value in paise, margin %, discount base in paise).

    - Product offer: they buy at least that product (or up to the minimum order).
    - Category / whole-order offer: their usual order size (or the minimum order if higher).
    The discount base is what a % discount applies to (the product, or the whole order).
    """
    product = catalog.get(offer.product_id)
    min_order = offer.min_order_rupees * 100
    usual = profile.avg_order_value_rupees * 100
    category = target_category(offer, catalog)

    if product:
        value = max(product.price_paise, min_order)
        return value, product.margin_pct, product.price_paise

    if not usual:
        usual = round(catalog.category_avg_price_paise.get(category, 100_000) * 1.5) if category else 100_000
    value = max(usual, min_order)
    if category:
        margin = catalog.category_avg_margin_pct[category]
    elif profile.category_spend_share:
        # whole-order offer: blend category margins by what this customer usually buys
        margin = sum(share * catalog.category_avg_margin_pct[c] for c, share in profile.category_spend_share.items())
    else:
        margin = sum(catalog.category_avg_margin_pct.values()) / len(catalog.category_avg_margin_pct)
    return value, round(margin, 2), value


def offer_cost_paise(offer: OfferProposal, order_value: int, discount_base: int) -> int:
    """What the offer costs the company if the customer uses it (cost models from config/offers.yaml)."""
    cfg = load_offers_config()
    t = offer.offer_type
    if t == "pct_off":
        return round(discount_base * offer.amount / 100)
    if t in ("flat_off", "cashback_to_wallet"):
        return round(offer.amount * 100)
    if t == "free_delivery":
        return round(cfg["delivery_fee_rupees"] * 100)
    if t == "points_multiplier":
        # normal points are worth ~1% of the order (10 points per Rs 100, 10 paise each); extra points cost more
        normal_points_value = order_value * cfg["points_per_100_rupees"] / 100 * cfg["point_value_rupees"]
        return round(max(offer.amount - 1, 0) * normal_points_value)
    return 0  # early_access, reminder


# ---------------------------------------------------------------------------------------------------
# How much does the offer raise the chance to buy?
# ---------------------------------------------------------------------------------------------------
def estimate_uplift(offer: OfferProposal, profile: CustomerProfile, catalog: Catalog) -> float:
    """
    Extra chance that this customer buys in the next 48h BECAUSE of this offer.

    When the uplift model is trained and knows this offer type, the answer is the AVERAGE of the model and the
    hand-made formula. Measured against the hidden truth (harness/eval_uplift.py, 400 customers), this blend ranked
    customers better than either method alone on 5 of 6 offer types (overall 0.50 vs 0.44 formula, 0.41 model):
    the two make different mistakes, so averaging cancels part of them. Otherwise the formula alone is used.
    """
    formula = formula_uplift(offer, profile, catalog)
    from engine.uplift import get_model  # imported here to avoid a circular import
    model = get_model()
    if model:
        p_with = model.p_with_offer(profile, offer, catalog)
        if p_with is not None:
            return round(min((formula + p_with - profile.p_buy_48h_no_offer) / 2, MAX_UPLIFT), 3)
    return formula


def formula_uplift(offer: OfferProposal, profile: CustomerProfile, catalog: Catalog) -> float:
    """
    The hand-made uplift formula (0..0.6). Every factor is an observable signal from the profile, in plain words.
    Used alone before the model is trained (and for 'reminder', which the model has no data on).
    """
    t = offer.offer_type
    uplift = BASE_UPLIFT[t]
    usual = max(profile.avg_order_value_rupees, 1)

    # 1. Size of the incentive (diminishing returns: double the discount is not double the effect)
    if t == "pct_off":
        uplift *= math.sqrt(max(offer.amount, 0) / 10)
    elif t in ("flat_off", "cashback_to_wallet"):
        uplift *= math.sqrt((offer.amount / usual) / 0.10) if offer.amount else 0
    elif t == "points_multiplier":
        uplift *= max(offer.amount - 1, 0)

    # 2. Who they are
    deal_seeking = max(profile.deal_seeker_score, profile.discount_dependence)
    if t in ("pct_off", "flat_off", "cashback_to_wallet", "early_access"):
        uplift *= 0.5 + 1.2 * deal_seeking                     # deal seekers react much more to discounts
    if (profile.overdue_ratio or 0) >= 1.3 and profile.total_orders >= 3:
        uplift *= 1.5                                           # a breaking habit: a nudge now can save it
    favourite_ids = {f["product_id"] for f in profile.favourite_items}
    if offer.product_id in favourite_ids:
        uplift *= 1.3                                           # an offer on something they love works better
    if profile.open_cart:
        cart_ids = {i["product_id"] for i in profile.open_cart["items"]}
        cart_categories = {i["category"] for i in profile.open_cart["items"]}
        if t == "free_delivery":
            uplift *= 2.5                                       # the delivery fee is often why carts get left
        elif offer.product_id in cart_ids or (offer.category in cart_categories and not offer.product_id):
            uplift *= 1.8
    if t == "points_multiplier" and profile.points_balance >= 500 and profile.points_redeem_rate < 0.1:
        uplift *= 3.0                                           # points collectors love bonus points
    if t == "reminder":
        if profile.points_expiring_7d:
            uplift *= 2.0
        if profile.wallet_balance_rupees >= 200 and (profile.wallet_idle_days or 0) >= 30:
            uplift *= 1.5

    # 3. How they reacted to this offer type before (only if we have at least 2 examples)
    past = profile.offer_response.get(t)
    if past and past["sent"] >= 2:
        uplift *= min(2.0, max(0.5, 0.6 + 1.6 * past["redeem_rate"]))

    # 4. Someone who'd buy anyway can't be moved much further
    uplift *= 1 - profile.p_buy_48h_no_offer
    return round(min(uplift, MAX_UPLIFT), 3)


# ---------------------------------------------------------------------------------------------------
# The final number
# ---------------------------------------------------------------------------------------------------
def estimate(offer: OfferProposal, profile: CustomerProfile, catalog: Catalog) -> ProfitEstimate:
    """Run the full profit formula for one offer and explain it in one paragraph a manager can read."""
    value, margin_pct, discount_base = expected_order(offer, profile, catalog)
    normal_margin = round(value * margin_pct / 100)
    cost = offer_cost_paise(offer, value, discount_base)
    margin_after = normal_margin - cost
    p_no = profile.p_buy_48h_no_offer
    p_with = min(MAX_P_BUY, max(0.0, p_no + estimate_uplift(offer, profile, catalog)))
    expected = round(p_with * margin_after - p_no * normal_margin)

    explanation = (
        f"Without an offer: {p_no:.0%} chance to buy in 48h. With it: {p_with:.0%}. "
        f"Expected order Rs {value // 100:,} at {margin_pct:.0f}% margin (Rs {normal_margin // 100:,}). "
        f"Offer costs Rs {cost // 100:,}, leaving Rs {margin_after // 100:,}. "
        f"Expected profit = {p_with:.2f} x Rs {margin_after // 100:,} - {p_no:.2f} x Rs {normal_margin // 100:,} "
        f"= Rs {expected // 100:,}."
    )
    return ProfitEstimate(
        p_buy_no_offer=round(p_no, 3),
        p_buy_with_offer=round(p_with, 3),
        expected_order_value_paise=value,
        normal_margin_paise=normal_margin,
        offer_cost_paise=cost,
        margin_after_offer_paise=margin_after,
        margin_after_offer_pct=round(margin_after / value * 100, 2) if value else 0.0,
        expected_profit_paise=expected,
        explanation=explanation,
    )


def max_amount_for_margin_floor(offer: OfferProposal, profile: CustomerProfile, catalog: Catalog,
                                floor_pct: float) -> float:
    """
    The biggest offer amount that still keeps the order margin at or above the floor.
    Used by the rules checker to REPAIR an offer that is too generous (instead of rejecting it outright).
    Returns 0 when even the smallest offer would break the floor.
    """
    value, margin_pct, discount_base = expected_order(offer, profile, catalog)
    allowed_cost = value * (margin_pct - floor_pct) / 100
    if allowed_cost <= 0:
        return 0.0
    t = offer.offer_type
    if t == "pct_off":
        return math.floor(allowed_cost / discount_base * 100 * 10) / 10 if discount_base else 0.0
    if t in ("flat_off", "cashback_to_wallet"):
        return float(math.floor(allowed_cost / 100))
    if t == "points_multiplier":
        cfg = load_offers_config()
        normal_points_value = value * cfg["points_per_100_rupees"] / 100 * cfg["point_value_rupees"]
        return math.floor((1 + allowed_cost / normal_points_value) * 10) / 10 if normal_points_value else 1.0
    return offer.amount  # free / fixed-cost types have no amount to lower
