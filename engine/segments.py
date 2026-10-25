"""
Demo profiles: good customers for a real tester to "play", grouped by what the company can SEE about them.

Where it sits: helper for the Testers page (api/main.py -> /api/testers/profiles). Not part of the decision pipeline.
The marketing manager wants to say "link my friend to an about-to-leave VIP" without knowing hidden personas
(customer_traits is hidden: only the generator and the harness may read it). So each segment below is defined only
by observable signals from the analyst profile (engine/features.py): order habit, spend, deal-seeking, carts, points.
harness/test_segments.py checks against the hidden truth that these picks really are mostly the persona they claim.

A good demo pick must also be able to receive an offer right now: marketing consent, nothing decided for them in
the last 7 days, not already played by a tester.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from engine.catalog import Catalog
from engine.clock import to_ts
from engine.features import CustomerProfile, build_profile


@dataclass(frozen=True)
class Segment:
    key: str
    label: str
    signals: str                                   # how the segment is recognised, in plain words
    expect: str                                    # what the AI should sensibly do for them
    match: Callable[[CustomerProfile], bool]
    score: Callable[[CustomerProfile], float]      # higher = a clearer example of the segment


SEGMENTS = [
    Segment("about_to_leave_vip", "About-to-leave VIP",
            "habit broken (well past their usual gap), big spender",
            "a real comeback offer",
            lambda p: p.total_orders >= 6 and (p.overdue_ratio or 0) >= 2.5 and p.avg_order_value_rupees >= 2000,
            lambda p: (p.overdue_ratio or 0) + p.total_spend_rupees / 20000),
    Segment("points_collector", "Points collector",
            "big points balance, rarely redeems",
            "points or a free nudge rather than a big discount",
            lambda p: p.points_balance >= 1000 and p.points_redeem_rate < 0.15,
            lambda p: p.points_balance / 1000),
    Segment("loyal_regular", "Loyal regular",
            "orders on schedule, likely to buy anyway",
            "nothing, or a zero-cost perk (no discount)",
            lambda p: p.total_orders >= 12 and (p.overdue_ratio or 9) < 1.0 and (p.habit_strength or 0) >= 0.6,
            lambda p: p.p_buy_48h_no_offer + (p.habit_strength or 0)),
    Segment("deal_hunter", "Deal hunter",
            "often buys in sales, all year round (not only at festivals)",
            "a discount within the caps",
            # buying ONLY in sales (score 1.0) is the festival-only pattern: they wait for the next big sale
            lambda p: 0.35 <= p.deal_seeker_score < 0.9 and p.total_orders >= 8,
            lambda p: p.total_orders / 10 + p.deal_seeker_score),
    Segment("cart_abandoner", "Cart abandoner",
            "orders regularly, but often leaves items in the cart",
            "an offer tied to the cart (or a deliberate wait)",
            # few orders + many abandoned carts is a window shopper (rarely buys at all), not a cart abandoner
            lambda p: p.cart_abandon_rate >= 0.6 and 6 <= p.total_orders <= 20,
            lambda p: p.cart_abandon_rate),
    Segment("new_customer", "New customer",
            "only one or two orders so far",
            "a small, low-risk offer",
            lambda p: p.total_orders <= 2,
            lambda p: -p.total_orders),
]


def demo_profiles(conn: sqlite3.Connection, catalog: Catalog, now: datetime, per_segment: int = 3,
                  sample_step: int = 5) -> list[dict]:
    """
    Up to `per_segment` clear examples per segment, from a spread-out sample (every `sample_step`-th customer).
    Each customer appears in at most one segment (the first one they match, in the order above: points collectors
    come before loyal regulars because both order steadily, and the points balance is the sharper signal).
    """
    busy = {r[0] for r in conn.execute("SELECT DISTINCT customer_id FROM decisions WHERE created_at >= ?",
                                       (to_ts(now - timedelta(days=7)),))}
    busy |= {r[0] for r in conn.execute("SELECT customer_id FROM testers WHERE customer_id IS NOT NULL")}
    ids = [r[0] for r in conn.execute("SELECT id FROM customers WHERE marketing_consent = 1 ORDER BY id")][::sample_step]

    found: dict[str, list[tuple[float, CustomerProfile]]] = {s.key: [] for s in SEGMENTS}
    for cid in ids:
        if cid in busy:
            continue
        profile = build_profile(conn, cid, now, catalog)
        for seg in SEGMENTS:
            if seg.match(profile):
                found[seg.key].append((seg.score(profile), profile))
                break

    out = []
    for seg in SEGMENTS:
        picks = sorted(found[seg.key], key=lambda x: -x[0])[:per_segment]
        out.append({
            "key": seg.key, "label": seg.label, "signals": seg.signals, "expect": seg.expect,
            "customers": [{"id": p.customer_id, "name": p.first_name, "city": p.city, "orders": p.total_orders,
                           "habit_status": p.habit_status, "spend_rupees": p.total_spend_rupees,
                           "buys_anyway": p.p_buy_48h_no_offer} for _, p in picks],
        })
    return out
