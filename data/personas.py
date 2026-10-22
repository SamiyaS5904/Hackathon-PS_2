"""
The 8 hidden customer personalities (personas) and how each customer's traits are drawn from them.

Where it sits: used only by data/generate.py. The traits produced here are the HIDDEN TRUTH saved in
the `customer_traits` table. The agent never sees them; it has to work out who a customer is from their
behaviour (orders, carts, browsing...), just like in a real company. The harness later uses this truth
to check whether the agent decided well.

Key idea: every behaviour in the dataset comes from these numbers, so the patterns are real:
  - loyal regulars order like clockwork and buy anyway  -> offers are mostly wasted on them
  - deal hunters wait for sales and react strongly to discounts
  - about-to-leave VIPs spend a lot, then their habit breaks in the last few months
  - cart abandoners leave carts and come back for free delivery
  - points hoarders pile up points and respond to bonus points
  - festival-only buyers almost only buy during big sales
  ...and so on.

`true_uplift[offer_type]` = how much an offer of that type raises the chance they order during the
offer window. Low uplift = "buys anyway" or "won't buy anyway". High uplift = a persuadable customer.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from data.catalog import CATEGORIES, REPEATABILITY

OFFER_TYPES = ["pct_off", "flat_off", "free_delivery", "cashback_to_wallet", "points_multiplier", "early_access"]
CATEGORY_NAMES = list(CATEGORIES)

Range = tuple[float, float]


@dataclass(frozen=True)
class PersonaSpec:
    """Ranges that each customer of this persona is drawn from. One persona = one kind of shopper."""

    name: str
    label: str
    share: float                      # fraction of all customers
    order_gap_days: Range             # natural rhythm between orders
    regularity: Range                 # 0 = random timing, 1 = like clockwork
    visits_per_day: float             # chance of opening the app on a normal day
    price_sensitivity: Range
    deal_affinity: Range
    churn_propensity: Range
    spam_tolerance: Range
    cart_abandon: Range
    festival_lift: float              # how much more they buy during a sale
    payday_lift: float
    weekend_lift: float
    repeat_rate: float                # chance an order item is one of their favourites
    basket_items: tuple[int, int]     # items per order
    prefers_expensive: bool           # VIPs pick the pricier option
    category_bias: dict[str, float]   # typical category mix for this persona
    uplift: dict[str, Range]          # true extra buy chance per offer type
    habit_break_prob: float           # chance their habit breaks during the year
    habit_break_days_before_end: tuple[int, int]
    wallet_topup_per_day: float
    points_redeem_prob: float         # chance they use points when they could
    age_range: tuple[int, int] = (20, 55)
    signup_days_before_end: tuple[int, int] = (400, 2200)  # most customers joined 1-6 years ago


PERSONAS: list[PersonaSpec] = [
    PersonaSpec(
        name="loyal_regular", label="Loyal regular", share=0.18,
        order_gap_days=(8, 16), regularity=(0.7, 0.9), visits_per_day=0.25,
        price_sensitivity=(0.2, 0.4), deal_affinity=(0.1, 0.3), churn_propensity=(0.05, 0.2),
        spam_tolerance=(0.3, 0.6), cart_abandon=(0.1, 0.2),
        festival_lift=1.2, payday_lift=1.1, weekend_lift=1.15, repeat_rate=0.6,
        basket_items=(1, 3), prefers_expensive=False,
        category_bias={"grocery": 3, "beauty": 2, "home": 1, "fashion": 1, "electronics": 0.5},
        # They buy anyway: offers barely change anything -> best decision is usually "send nothing"
        uplift={"pct_off": (0.02, 0.05), "flat_off": (0.02, 0.05), "free_delivery": (0.01, 0.03),
                "cashback_to_wallet": (0.02, 0.04), "points_multiplier": (0.02, 0.05), "early_access": (0.01, 0.03)},
        habit_break_prob=0.12, habit_break_days_before_end=(30, 120),
        wallet_topup_per_day=0.008, points_redeem_prob=0.35,
    ),
    PersonaSpec(
        name="deal_hunter", label="Deal hunter", share=0.16,
        order_gap_days=(30, 50), regularity=(0.3, 0.5), visits_per_day=0.30,
        price_sensitivity=(0.7, 0.95), deal_affinity=(0.7, 0.95), churn_propensity=(0.2, 0.4),
        spam_tolerance=(0.5, 0.8), cart_abandon=(0.3, 0.45),
        festival_lift=3.5, payday_lift=1.3, weekend_lift=1.2, repeat_rate=0.2,
        basket_items=(1, 3), prefers_expensive=False,
        category_bias={"fashion": 3, "electronics": 2, "beauty": 1.5, "home": 1, "grocery": 0.7},
        uplift={"pct_off": (0.25, 0.40), "flat_off": (0.20, 0.35), "free_delivery": (0.08, 0.15),
                "cashback_to_wallet": (0.20, 0.30), "points_multiplier": (0.05, 0.10), "early_access": (0.10, 0.20)},
        habit_break_prob=0.10, habit_break_days_before_end=(30, 120),
        wallet_topup_per_day=0.004, points_redeem_prob=0.6,
    ),
    PersonaSpec(
        name="vip_at_risk", label="About-to-leave VIP", share=0.08,
        order_gap_days=(6, 12), regularity=(0.6, 0.85), visits_per_day=0.35,
        price_sensitivity=(0.2, 0.4), deal_affinity=(0.3, 0.5), churn_propensity=(0.7, 0.9),
        spam_tolerance=(0.2, 0.4), cart_abandon=(0.15, 0.25),
        festival_lift=1.4, payday_lift=1.1, weekend_lift=1.1, repeat_rate=0.5,
        basket_items=(2, 4), prefers_expensive=True,
        category_bias={"electronics": 2, "fashion": 2, "home": 1.5, "beauty": 1.5, "grocery": 1},
        # Their habit is breaking: a good offer on what they love can bring them back
        uplift={"pct_off": (0.25, 0.40), "flat_off": (0.20, 0.35), "free_delivery": (0.05, 0.10),
                "cashback_to_wallet": (0.20, 0.35), "points_multiplier": (0.10, 0.15), "early_access": (0.10, 0.20)},
        habit_break_prob=1.0, habit_break_days_before_end=(40, 100),
        wallet_topup_per_day=0.010, points_redeem_prob=0.4,
    ),
    PersonaSpec(
        name="window_shopper", label="Window shopper", share=0.15,
        order_gap_days=(70, 140), regularity=(0.2, 0.4), visits_per_day=0.45,
        price_sensitivity=(0.5, 0.8), deal_affinity=(0.4, 0.6), churn_propensity=(0.3, 0.5),
        spam_tolerance=(0.2, 0.4), cart_abandon=(0.5, 0.7),
        festival_lift=1.8, payday_lift=1.2, weekend_lift=1.3, repeat_rate=0.1,
        basket_items=(1, 2), prefers_expensive=False,
        category_bias={"electronics": 3, "fashion": 2.5, "home": 1, "beauty": 1, "grocery": 0.3},
        # Browse a lot, rarely buy, offers rarely convert them -> mostly "won't buy anyway"
        uplift={"pct_off": (0.05, 0.12), "flat_off": (0.05, 0.10), "free_delivery": (0.04, 0.08),
                "cashback_to_wallet": (0.04, 0.08), "points_multiplier": (0.01, 0.03), "early_access": (0.02, 0.05)},
        habit_break_prob=0.05, habit_break_days_before_end=(30, 120),
        wallet_topup_per_day=0.002, points_redeem_prob=0.3,
    ),
    PersonaSpec(
        name="new_user", label="New user", share=0.10,
        order_gap_days=(15, 35), regularity=(0.3, 0.5), visits_per_day=0.30,
        price_sensitivity=(0.4, 0.7), deal_affinity=(0.4, 0.6), churn_propensity=(0.3, 0.5),
        spam_tolerance=(0.3, 0.5), cart_abandon=(0.25, 0.35),
        festival_lift=1.5, payday_lift=1.2, weekend_lift=1.2, repeat_rate=0.25,
        basket_items=(1, 2), prefers_expensive=False,
        category_bias={"fashion": 1.5, "beauty": 1.5, "electronics": 1.2, "home": 1, "grocery": 1},
        uplift={"pct_off": (0.15, 0.25), "flat_off": (0.15, 0.25), "free_delivery": (0.10, 0.20),
                "cashback_to_wallet": (0.10, 0.20), "points_multiplier": (0.03, 0.06), "early_access": (0.05, 0.10)},
        habit_break_prob=0.05, habit_break_days_before_end=(5, 15),
        wallet_topup_per_day=0.003, points_redeem_prob=0.3,
        age_range=(18, 35), signup_days_before_end=(20, 60),  # joined in the last 2 months
    ),
    PersonaSpec(
        name="cart_abandoner", label="Cart abandoner", share=0.13,
        order_gap_days=(25, 45), regularity=(0.3, 0.5), visits_per_day=0.35,
        price_sensitivity=(0.5, 0.8), deal_affinity=(0.4, 0.6), churn_propensity=(0.2, 0.4),
        spam_tolerance=(0.4, 0.6), cart_abandon=(0.6, 0.8),
        festival_lift=1.6, payday_lift=1.2, weekend_lift=1.2, repeat_rate=0.2,
        basket_items=(1, 3), prefers_expensive=False,
        category_bias={"fashion": 2.5, "beauty": 2, "home": 1, "electronics": 1, "grocery": 0.8},
        # The delivery fee is what stops them: free delivery is the key
        uplift={"pct_off": (0.12, 0.20), "flat_off": (0.12, 0.20), "free_delivery": (0.25, 0.40),
                "cashback_to_wallet": (0.10, 0.15), "points_multiplier": (0.03, 0.06), "early_access": (0.02, 0.05)},
        habit_break_prob=0.10, habit_break_days_before_end=(30, 120),
        wallet_topup_per_day=0.003, points_redeem_prob=0.35,
    ),
    PersonaSpec(
        name="points_hoarder", label="Points hoarder", share=0.10,
        order_gap_days=(12, 22), regularity=(0.6, 0.8), visits_per_day=0.25,
        price_sensitivity=(0.3, 0.5), deal_affinity=(0.2, 0.4), churn_propensity=(0.1, 0.25),
        spam_tolerance=(0.4, 0.6), cart_abandon=(0.15, 0.25),
        festival_lift=1.3, payday_lift=1.1, weekend_lift=1.1, repeat_rate=0.5,
        basket_items=(1, 3), prefers_expensive=False,
        category_bias={"grocery": 2.5, "beauty": 2, "home": 1.2, "fashion": 1, "electronics": 0.6},
        # They love collecting points: bonus points work, and they cost us very little
        uplift={"pct_off": (0.05, 0.10), "flat_off": (0.05, 0.10), "free_delivery": (0.03, 0.06),
                "cashback_to_wallet": (0.05, 0.10), "points_multiplier": (0.20, 0.30), "early_access": (0.05, 0.10)},
        habit_break_prob=0.12, habit_break_days_before_end=(30, 120),
        wallet_topup_per_day=0.005, points_redeem_prob=0.02,  # almost never redeem -> points pile up and expire
    ),
    PersonaSpec(
        name="festival_only", label="Festival-only buyer", share=0.10,
        order_gap_days=(200, 300), regularity=(0.2, 0.3), visits_per_day=0.12,
        price_sensitivity=(0.6, 0.9), deal_affinity=(0.7, 0.9), churn_propensity=(0.3, 0.5),
        spam_tolerance=(0.2, 0.4), cart_abandon=(0.3, 0.4),
        festival_lift=25.0, payday_lift=1.1, weekend_lift=1.1, repeat_rate=0.1,
        basket_items=(1, 3), prefers_expensive=False,
        category_bias={"electronics": 2.5, "home": 2, "fashion": 2, "beauty": 1, "grocery": 0.5},
        uplift={"pct_off": (0.10, 0.20), "flat_off": (0.10, 0.15), "free_delivery": (0.03, 0.06),
                "cashback_to_wallet": (0.05, 0.10), "points_multiplier": (0.02, 0.04), "early_access": (0.20, 0.30)},
        habit_break_prob=0.0, habit_break_days_before_end=(30, 120),
        wallet_topup_per_day=0.002, points_redeem_prob=0.4,
    ),
]

PERSONAS_BY_NAME = {p.name: p for p in PERSONAS}


@dataclass
class CustomerTraits:
    """One customer's hidden personality, drawn from their persona's ranges."""

    persona: str
    price_sensitivity: float
    deal_affinity: float
    churn_propensity: float
    spam_tolerance: float
    base_order_gap_days: float
    habit_regularity: float
    cart_abandon_prob: float
    peak_hour: int
    category_weights: dict[str, float]
    favourite_product_ids: list[int]
    habit_break_day: int | None          # day index in the simulation when the habit breaks (None = never)
    true_uplift: dict[str, float] = field(default_factory=dict)


def choose_persona(rng: random.Random) -> PersonaSpec:
    """Pick a persona according to each persona's share of the customer base."""
    return rng.choices(PERSONAS, weights=[p.share for p in PERSONAS])[0]


def sample_traits(
    spec: PersonaSpec,
    rng: random.Random,
    products_by_category: dict[str, list[dict]],
    n_days: int,
) -> CustomerTraits:
    """
    Draw one customer's hidden traits from their persona.

    Each number is picked inside the persona's range, so two deal hunters are similar but not identical.
    `n_days` is the length of the simulation, used to place the habit-break day near the end.
    """
    def pick(r: Range) -> float:
        return round(rng.uniform(*r), 3)

    category_weights = _personal_category_weights(spec, rng)

    habit_break_day = None
    if rng.random() < spec.habit_break_prob:
        days_before_end = rng.randint(*spec.habit_break_days_before_end)
        habit_break_day = max(0, n_days - days_before_end)

    return CustomerTraits(
        persona=spec.name,
        price_sensitivity=pick(spec.price_sensitivity),
        deal_affinity=pick(spec.deal_affinity),
        churn_propensity=pick(spec.churn_propensity),
        spam_tolerance=pick(spec.spam_tolerance),
        base_order_gap_days=pick(spec.order_gap_days),
        habit_regularity=pick(spec.regularity),
        cart_abandon_prob=pick(spec.cart_abandon),
        # Most people shop in the evening; a few in the morning or late night
        peak_hour=rng.choices(range(8, 24), weights=[1, 1, 2, 2, 2, 3, 3, 2, 2, 3, 4, 6, 7, 7, 5, 3])[0],
        category_weights=category_weights,
        favourite_product_ids=_pick_favourites(rng, category_weights, products_by_category),
        habit_break_day=habit_break_day,
        true_uplift={offer: pick(spec.uplift[offer]) for offer in OFFER_TYPES},
    )


def _personal_category_weights(spec: PersonaSpec, rng: random.Random) -> dict[str, float]:
    """
    Persona's typical category mix + personal taste.

    Multiplying by random noise means one deal hunter may love sneakers while another loves gadgets,
    so "favourite category" differs person to person (important for personalised offers).
    """
    raw = {c: spec.category_bias.get(c, 1.0) * rng.gammavariate(2.0, 1.0) for c in CATEGORY_NAMES}
    total = sum(raw.values())
    return {c: round(w / total, 3) for c, w in raw.items()}


def _pick_favourites(
    rng: random.Random,
    category_weights: dict[str, float],
    products_by_category: dict[str, list[dict]],
) -> list[int]:
    """
    Choose 3-6 products this customer will keep re-ordering.

    Weighted by their category taste AND by how re-orderable the category is (rice yes, smartwatch no).
    These favourites are what habit-break offers should target ("time to restock your Green Tea?").
    """
    weights = {c: category_weights[c] * REPEATABILITY[c] for c in CATEGORY_NAMES}
    favourites: list[int] = []
    for _ in range(rng.randint(3, 6)):
        category = rng.choices(list(weights), weights=list(weights.values()))[0]
        product = rng.choice(products_by_category[category])
        if product["id"] not in favourites:
            favourites.append(product["id"])
    return favourites
