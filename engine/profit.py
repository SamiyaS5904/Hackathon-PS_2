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

Retention value (v2, only for LATE customers: overdue and enough past orders, config rules.yaml 'retention'):
    retention_value = churn_avoidance_share x (P(buy|offer) - P(buy|no offer)) x future_margin
    future_margin   = expected orders in the horizon (horizon days / usual gap, capped) x normal margin per order
    total_value     = expected_profit + retention_value
A lapsing customer who comes back is worth their future orders, not just today's. The rules decide how this may
be used (only for win-back offers, inside a separate budget, margin floor always applies).
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
# CALIBRATED on the company's own past campaigns (treatment vs random holdout, offers_history, 19,683 offers):
#   10% off +8.6 pts, flat Rs 100-150 +5.4-6.8, Rs 100 cashback +5.2, free delivery +3.6, early access +3.3,
#   2x points +2.5, 3x points +4.0. The bases below are those effects divided by the average of the
#   personal factors applied further down (~0.8), so an average customer gets the measured effect.
#   (The first version guessed these and over-rated points by ~2x; see documentation.md, Session 6.)
BASE_UPLIFT: dict[str, float] = {
    "pct_off": 0.11,             # for a 10% discount
    "flat_off": 0.075,           # for a flat amount worth ~10% of their usual order
    "cashback_to_wallet": 0.065, # same, but paid later -> slightly weaker
    "free_delivery": 0.045,
    "points_multiplier": 0.03,   # for 2x points (3x adds ~60% more, as measured)
    "early_access": 0.04,
    "reminder": 0.02,            # never tested in a campaign: kept below early access on purpose
}
MAX_UPLIFT = 0.60
HIGH_VALUE_ORDER_RUPEES = 2500                 # usual order at or above this = a high-value customer
FREE_DELIVERY_FULL_EFFECT_UP_TO_RUPEES = 1200  # above this usual order size, free delivery matters less and less
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
    if profile.learning_mode:
        # Real shop, no past campaigns yet: no model, and expect less from every offer (fewer offers pass)
        return round(formula * profile.uplift_factor, 3)
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
        # measured: 2x +2.5 pts, 3x +4.0 pts -> each extra 'x' above 2 adds ~60% of the 2x effect
        uplift *= 0 if offer.amount <= 1 else (offer.amount - 1 if offer.amount < 2 else 1 + 0.6 * (offer.amount - 2))

    # 2. Who they are
    if t in ("pct_off", "flat_off", "cashback_to_wallet", "early_access"):
        # deal seekers (who buy in sales) react more to discounts, but not buying in sales does NOT mean a discount
        # won't work (e.g. a lapsing regular): a moderate swing, 0.75x to 1.25x.
        # Past COUPON use (discount_dependence) deliberately does NOT raise this: coupon-trained buying is a habit
        # cost to protect against (rules 'habit_protection'), not a reason to give more coupons.
        uplift *= 0.75 + 0.5 * profile.deal_seeker_score
    if (profile.overdue_ratio or 0) >= 1.3 and profile.total_orders >= 3:
        uplift *= 1.5                                           # a breaking habit: a nudge now can save it
        if t in ("pct_off", "flat_off", "cashback_to_wallet") and usual >= HIGH_VALUE_ORDER_RUPEES:
            # measured in past campaigns: late, high-value customers reacted to % off ~2.5x more than on-time
            # ones (+16 pts vs +6), so a real discount is what wins a lapsing VIP back, not a small perk
            uplift *= 1.5
    if t == "free_delivery":
        # Rs 40 delivery matters on a Rs 800 order, hardly on a Rs 4,000 one (measured: +5.3 pts for smaller
        # baskets, +0.8 for big ones). An open cart is handled below: there the fee is often the reason.
        uplift *= min(1.0, max(0.25, FREE_DELIVERY_FULL_EFFECT_UP_TO_RUPEES / usual))
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
def is_late_customer(profile: CustomerProfile, retention: dict | None) -> bool:
    """Late = their habit is slipping: overdue vs their usual gap, with enough orders to have a habit at all."""
    return bool(retention and profile.total_orders >= retention["min_orders"]
                and (profile.overdue_ratio or 0) >= retention["min_overdue_ratio"])


def future_margin_paise(profile: CustomerProfile, catalog: Catalog, retention: dict) -> int:
    """Margin of the orders a healthy habit would bring in the horizon: expected orders x normal margin per order."""
    if not profile.usual_gap_days:
        return 0
    orders = min(retention["horizon_weeks"] * 7 / max(profile.usual_gap_days, 1), retention["max_future_orders"])
    value, margin_pct, _ = expected_order(OfferProposal(offer_type="reminder"), profile, catalog)
    return round(orders * value * margin_pct / 100)


def estimate(offer: OfferProposal, profile: CustomerProfile, catalog: Catalog,
             retention: dict | None = None) -> ProfitEstimate:
    """
    Run the full profit formula for one offer and explain it in one paragraph a manager can read.
    `retention` (rules 'retention' section) adds the retention value for late customers; without it the
    estimate is the plain 48h profit.
    """
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
    late = is_late_customer(profile, retention)
    retention_paise = 0
    if late:
        future = future_margin_paise(profile, catalog, retention)
        retention_paise = round(retention["churn_avoidance_share"] * max(p_with - p_no, 0) * future)
        explanation += (
            f" Retention (customer is late: {profile.overdue_ratio:g}x their usual gap): "
            f"{retention['churn_avoidance_share']:.0%} of the {p_with - p_no:.0%} extra chance keeps the habit, worth "
            f"Rs {future // 100:,} of margin over {retention['horizon_weeks']} weeks = Rs {retention_paise // 100:,}. "
            f"Total value = Rs {expected // 100:,} + Rs {retention_paise // 100:,} = "
            f"Rs {(expected + retention_paise) // 100:,}.")
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
        retention_value_paise=retention_paise,
        total_value_paise=expected + retention_paise,
        is_late_customer=late,
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
