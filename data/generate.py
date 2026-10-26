"""
Synthetic company data generator: 3,000 customers x 12 months of realistic e-commerce behaviour.

Where it sits: STEP 1 of the pipeline. It creates the SQLite database that everything else reads:
    data/generate.py -> SQLite -> engine/features.py (analyst) -> agent -> rules -> bot / dashboard

Run:
    python -m data.generate --customers 3000 --months 12
    python -m data.generate --force          # rebuild even if the database already exists

How it works (simple version):
  1. Build the product catalog and the shopping calendar (festivals, paydays, weekends).
  2. Plan past marketing campaigns. In each one, some customers get an offer (treatment) and some are
     picked but NOT sent anything (holdout). Comparing the two shows who buys BECAUSE of an offer.
  3. For every customer: pick a hidden persona and traits (data/personas.py), then simulate every day:
       - Will they order today? Depends on their natural rhythm (days since last order vs their usual
         gap), festivals, payday, weekend, an active offer, and whether their habit has broken.
       - Will they open the app? If yes, create a session with product views, searches, wishlist
         adds and cart activity (including abandoned carts).
       - Orders get items (often their favourites), a payment method, maybe points used, a status
         (delivered / cancelled / returned), points earned, wallet refunds and cashback.
       - Discount training (data/habits.py): each coupon-discounted order makes them wait a bit more for deals
         (fewer orders on normal days, more in sales); months without coupons slowly undo it.
       - Wallet top-ups happen now and then; reward points expire after 90 days.
  4. Save everything, check that all links between tables are valid, and print a summary per persona
     so we can see the patterns are real.

Everything is driven by a fixed random seed, so the same command always produces the same data.
Hidden traits go to `customer_traits`, which only this file and harness/ may read.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from config.settings import settings
from data.calendar_gen import build_calendar, festival_name_on, is_payday, is_weekend
from data.catalog import build_customer_identity, build_products
from data.habits import DealHabit, revival_chance
from data.personas import CustomerTraits, PersonaSpec, choose_persona, sample_traits
from db.store import connect, init_schema, insert_many

# ---------------------------------------------------------------------------------------------------
# Business constants of the simulated shop (match config/offers.yaml)
# ---------------------------------------------------------------------------------------------------
DELIVERY_FEE_PAISE = 40_00              # customers pay Rs 40 delivery...
FREE_DELIVERY_ABOVE_PAISE = 499_00      # ...on orders below Rs 499
POINTS_PER_RUPEES = 10                  # 1 point per Rs 10 spent
POINT_VALUE_PAISE = 10                  # 1 point = 10 paise when redeemed
POINTS_EXPIRY_DAYS = 90
MIN_POINTS_TO_REDEEM = 200

IST_OFFSET = timedelta(hours=5, minutes=30)
FLUSH_EVERY = 250                        # write to the database every 250 customers to keep memory low

# Past campaigns: one every 14 days. Each picks 30% of customers; 20% of those are the holdout group.
CAMPAIGN_EVERY_DAYS = 14
CAMPAIGN_REACH = 0.30
HOLDOUT_SHARE = 0.20
CAMPAIGN_WINDOW_DAYS = 2                 # offer valid for 48 hours

# The rotation of past campaign offers: (offer_type, value, target, min order in rupees)
CAMPAIGN_ROTATION: list[tuple[str, float, str, int]] = [
    ("pct_off", 10, "favourite", 499),
    ("free_delivery", 0, "any", 299),
    ("flat_off", 150, "fashion", 999),
    ("points_multiplier", 2, "any", 0),
    ("pct_off", 15, "beauty", 399),
    ("cashback_to_wallet", 100, "favourite", 799),
    ("pct_off", 20, "fashion", 999),
    ("early_access", 0, "electronics", 0),
    ("flat_off", 100, "favourite", 799),
    ("points_multiplier", 3, "any", 0),
    ("pct_off", 5, "electronics", 999),
    ("free_delivery", 0, "any", 299),
]

# Column order for every table we write (must match db/schema.sql)
COLUMNS: dict[str, tuple[str, ...]] = {
    "customers": ("id", "first_name", "last_name", "gender", "age", "city", "city_tier", "signup_date",
                  "device", "language", "marketing_consent", "telegram_chat_id"),
    "customer_traits": ("customer_id", "persona", "price_sensitivity", "deal_affinity", "churn_propensity",
                        "spam_tolerance", "base_order_gap_days", "habit_regularity", "cart_abandon_prob",
                        "peak_hour", "category_weights", "favourite_product_ids", "habit_break_date",
                        "true_uplift", "wait_for_deal"),
    "products": ("id", "name", "category", "subcategory", "brand", "price_paise", "cost_paise", "margin_pct"),
    "sessions": ("id", "customer_id", "start_ts", "end_ts", "device", "source"),
    "browse_events": ("id", "session_id", "customer_id", "ts", "event_type", "product_id", "query"),
    "cart_events": ("id", "session_id", "customer_id", "ts", "event_type", "product_id", "qty", "cart_value_paise"),
    "campaigns": ("id", "name", "start_date", "offer_type", "value", "target"),
    "orders": ("id", "customer_id", "session_id", "ts", "gross_paise", "discount_paise", "delivery_fee_paise",
               "net_paise", "offer_id", "payment_method", "status", "points_earned", "points_redeemed"),
    "order_items": ("id", "order_id", "product_id", "qty", "unit_price_paise"),
    "wallet_txns": ("id", "customer_id", "ts", "txn_type", "amount_paise", "balance_after_paise", "order_id"),
    "points_ledger": ("id", "customer_id", "ts", "entry_type", "points", "expires_at", "order_id"),
    "offers_history": ("id", "campaign_id", "customer_id", "offer_type", "value", "category", "min_order_paise",
                       "group_name", "sent_at", "expires_at", "opened_at", "clicked_at", "redeemed_at",
                       "order_id", "purchased_in_window"),
    "calendar": ("date", "is_weekend", "is_payday", "is_festival", "festival_name"),
}
# Insert order: parents before children (helps if someone turns foreign keys on during the load)
TABLE_ORDER = ["customers", "customer_traits", "sessions", "browse_events", "cart_events",
               "offers_history", "orders", "order_items", "wallet_txns", "points_ledger"]


def to_utc(local: datetime) -> str:
    """IST datetime -> UTC ISO string, the format every timestamp column uses."""
    return (local - IST_OFFSET).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------------------------------
# Small helpers: ID counters and row buffers
# ---------------------------------------------------------------------------------------------------
class Ids:
    """Hands out increasing IDs per table, so rows can reference each other before they are saved."""

    def __init__(self) -> None:
        self._last: dict[str, int] = {}

    def next(self, table: str) -> int:
        self._last[table] = self._last.get(table, 0) + 1
        return self._last[table]


class Buffers:
    """Collects rows in memory per table and writes them in bulk (much faster than row by row)."""

    def __init__(self) -> None:
        self.rows: dict[str, list[tuple]] = {t: [] for t in TABLE_ORDER}
        self.counts: dict[str, int] = {t: 0 for t in TABLE_ORDER}

    def add(self, table: str, row: tuple) -> None:
        self.rows[table].append(row)

    def flush(self, conn) -> None:
        for table in TABLE_ORDER:
            if self.rows[table]:
                insert_many(conn, table, COLUMNS[table], self.rows[table])
                self.counts[table] += len(self.rows[table])
                self.rows[table].clear()
        conn.commit()


# ---------------------------------------------------------------------------------------------------
# Past campaigns
# ---------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Campaign:
    id: int
    day_idx: int
    name: str
    offer_type: str
    value: float
    target: str            # 'favourite', 'any', or a category name
    min_order_paise: int


def plan_campaigns(days: list[date]) -> list[Campaign]:
    """One campaign every 14 days, rotating through CAMPAIGN_ROTATION."""
    campaigns = []
    for n, day_idx in enumerate(range(7, len(days) - 2, CAMPAIGN_EVERY_DAYS)):
        offer_type, value, target, min_order = CAMPAIGN_ROTATION[n % len(CAMPAIGN_ROTATION)]
        name = f"{days[day_idx].strftime('%d %b %Y')} - {offer_type} {value:g} on {target}"
        campaigns.append(Campaign(n + 1, day_idx, name, offer_type, value, target, min_order * 100))
    return campaigns


# ---------------------------------------------------------------------------------------------------
# One customer's year
# ---------------------------------------------------------------------------------------------------
class CustomerSimulator:
    """
    Simulates one customer day by day and writes their rows into the shared buffers.

    State it keeps while walking through the days: when they last ordered, wallet balance, reward point
    lots (each lot expires 90 days after it was earned), and any active offer from a past campaign.
    """

    def __init__(self, customer_id: int, spec: PersonaSpec, traits: CustomerTraits, identity: dict,
                 signup_idx: int, ctx: "SimContext") -> None:
        self.cid = customer_id
        self.spec = spec
        self.traits = traits
        self.identity = identity
        self.signup_idx = signup_idx
        self.ctx = ctx
        # Each customer's days use their own random stream: a change in one customer's behaviour (e.g. discount
        # training) can't shift the names, personas and traits of every customer generated after them.
        self.rng = random.Random(f"{ctx.seed}-sim-{customer_id}")
        self.habit = DealHabit()  # hidden: how much coupons have taught them to wait for deals (data/habits.py)

        gap = traits.base_order_gap_days
        if spec.name == "new_user":
            # New users are curious: start them "almost due" so the first order comes soon after signup
            self.last_order_idx = signup_idx - gap * 0.9
        else:
            # Existing customers are somewhere in the middle of their rhythm when our data starts
            self.last_order_idx = -self.rng.uniform(0, gap)

        self.wallet_paise = 0
        self.point_lots: deque[list] = deque()   # [points_left, expires_day_idx], oldest first
        self.active_offer: dict | None = None
        self.top_category = max(traits.category_weights, key=traits.category_weights.get)
        self.campaign_plan = self._assign_campaigns()

    # -- campaigns -------------------------------------------------------------------------------
    def _assign_campaigns(self) -> dict[int, tuple[Campaign, str]]:
        """Decide which past campaigns picked this customer, and whether as treatment or holdout."""
        plan = {}
        if not self.identity["marketing_consent"]:
            return plan  # opted out of marketing: never picked
        for campaign in self.ctx.campaigns:
            if campaign.day_idx < self.signup_idx or self.rng.random() >= CAMPAIGN_REACH:
                continue
            group = "holdout" if self.rng.random() < HOLDOUT_SHARE else "treatment"
            plan[campaign.day_idx] = (campaign, group)
        return plan

    def _start_offer(self, day_idx: int, campaign: Campaign, group: str) -> None:
        """Create the offers_history record for this campaign; treatment customers may open/click it."""
        day = self.ctx.days[day_idx]
        sent_local = datetime(day.year, day.month, day.day, 11) + timedelta(minutes=self.rng.randint(0, 60))
        category = self.top_category if campaign.target == "favourite" else (
            None if campaign.target == "any" else campaign.target)
        record = {
            "id": self.ctx.ids.next("offers_history"), "campaign": campaign, "group": group,
            "category": category, "sent_local": sent_local,
            "expires_local": sent_local + timedelta(hours=48),
            "opened_local": None, "clicked_local": None, "redeemed_local": None,
            "order_id": None, "purchased": 0,
        }
        if group == "treatment":
            # Deal lovers open and click offers more often
            if self.rng.random() < 0.3 + 0.4 * self.traits.deal_affinity:
                record["opened_local"] = sent_local + timedelta(minutes=self.rng.randint(5, 300))
                if self.rng.random() < 0.3 + 0.5 * self.traits.deal_affinity:
                    record["clicked_local"] = record["opened_local"] + timedelta(minutes=self.rng.randint(1, 10))
        self.active_offer = {"record": record, "end_idx": day_idx + CAMPAIGN_WINDOW_DAYS - 1}

    def _close_offer(self) -> None:
        """Offer window is over: write the offers_history row."""
        r = self.active_offer["record"]
        c: Campaign = r["campaign"]
        self.ctx.buf.add("offers_history", (
            r["id"], c.id, self.cid, c.offer_type, c.value, r["category"], c.min_order_paise, r["group"],
            to_utc(r["sent_local"]) if r["group"] == "treatment" else None,
            to_utc(r["expires_local"]),
            to_utc(r["opened_local"]) if r["opened_local"] else None,
            to_utc(r["clicked_local"]) if r["clicked_local"] else None,
            to_utc(r["redeemed_local"]) if r["redeemed_local"] else None,
            r["order_id"], r["purchased"],
        ))
        self.active_offer = None

    def _offer_is_working(self) -> bool:
        """An offer only changes behaviour if it was sent (treatment) AND the customer opened it."""
        return bool(self.active_offer and self.active_offer["record"]["group"] == "treatment"
                    and self.active_offer["record"]["opened_local"])

    # -- the daily loop --------------------------------------------------------------------------
    def run(self) -> None:
        for day_idx in range(self.signup_idx, len(self.ctx.days)):
            self.habit.pass_days(1)
            if day_idx in self.campaign_plan:
                if self.active_offer:
                    self._close_offer()
                self._start_offer(day_idx, *self.campaign_plan[day_idx])

            self._expire_points(day_idx)
            self._maybe_topup_wallet(day_idx)

            will_order = self.rng.random() < self._order_probability(day_idx)
            clicked_today = bool(
                self.active_offer and self.active_offer["record"]["clicked_local"]
                and self.active_offer["record"]["clicked_local"].date() == self.ctx.days[day_idx])
            will_visit = will_order or clicked_today or self.rng.random() < self._visit_probability(day_idx)

            if will_visit:
                self._session(day_idx, will_order, source="notification" if clicked_today else None)
                if self.rng.random() < 0.15:  # some people come back a second time the same day
                    self._session(day_idx, False)

            if self.active_offer and day_idx >= self.active_offer["end_idx"]:
                self._close_offer()

        if self.active_offer:
            self._close_offer()

    def _habit_factor(self, day_idx: int) -> float:
        """
        1.0 normally. After their habit breaks it fades towards 0 over about two months:
        they order less and less, then stop. This is what habit-break offers should catch early.
        """
        if self.traits.habit_break_day is None or day_idx < self.traits.habit_break_day:
            return 1.0
        return 0.15 * math.exp(-(day_idx - self.traits.habit_break_day) / 20)

    def _order_probability(self, day_idx: int) -> float:
        """
        Chance of ordering today.

        Core: the closer they are to (or past) their usual gap, the more likely they order. Regular
        customers have a sharp rise right at their gap; irregular ones a slow rise. Then festivals,
        payday and weekends push it up, a broken habit pushes it down, and an opened offer adds its
        true uplift spread over the offer window.
        """
        t, spec = self.traits, self.spec
        days_since = max(day_idx - self.last_order_idx, 0.5)
        x = days_since / t.base_order_gap_days
        shape = min(x ** (1 + 4 * t.habit_regularity), 4.0)
        p = shape / t.base_order_gap_days

        if self.ctx.festival[day_idx]:
            p *= spec.festival_lift * (0.8 + 0.4 * t.deal_affinity)
        elif spec.name == "festival_only":
            p *= 0.15  # outside sales they rarely buy
        if self.ctx.payday[day_idx]:
            p *= spec.payday_lift
        if self.ctx.weekend[day_idx]:
            p *= spec.weekend_lift
        p *= self._habit_factor(day_idx)
        p *= self.habit.order_factor(self.ctx.festival[day_idx])  # coupon-trained: less on normal days, more in sales

        if self._offer_is_working():
            offer_type = self.active_offer["record"]["campaign"].offer_type
            p += t.true_uplift[offer_type] / CAMPAIGN_WINDOW_DAYS
        return min(p, 0.95)

    def _visit_probability(self, day_idx: int) -> float:
        """Chance of opening the app without ordering (browsing, comparing, leaving carts)."""
        p = self.spec.visits_per_day
        if self.ctx.weekend[day_idx]:
            p *= self.spec.weekend_lift
        if self.ctx.festival[day_idx]:
            p *= 1.0 + self.traits.deal_affinity  # deal lovers browse much more during sales
        if self._habit_factor(day_idx) < 1.0:
            p *= 0.35  # habit breaking: they visit less too
        return min(p, 0.9)

    # -- sessions, browsing, carts ---------------------------------------------------------------
    def _session_start(self, day_idx: int) -> datetime:
        """A time near their usual shopping hour."""
        day = self.ctx.days[day_idx]
        hour = min(23, max(0, round(self.rng.gauss(self.traits.peak_hour, 1.5))))
        return datetime(day.year, day.month, day.day, hour, self.rng.randint(0, 59), self.rng.randint(0, 59))

    def _session(self, day_idx: int, will_order: bool, source: str | None = None) -> None:
        ctx, rng = self.ctx, self.rng
        session_id = ctx.ids.next("sessions")
        start = self._session_start(day_idx)
        now = start

        def step() -> datetime:
            nonlocal now
            now = now + timedelta(seconds=rng.randint(20, 90))
            return now

        # Browsing: views, sometimes a search first, occasional wishlist adds
        if rng.random() < 0.35:
            searched = self._pick_product()
            query = searched["subcategory"].lower() if rng.random() < 0.7 else \
                f"{searched['brand']} {searched['subcategory']}".lower()
            ctx.buf.add("browse_events", (ctx.ids.next("browse_events"), session_id, self.cid, to_utc(step()),
                                          "search", None, query))
        n_views = rng.randint(3, 9) if self.spec.name == "window_shopper" else rng.randint(1, 5)
        for _ in range(n_views):
            product = self._pick_product()
            ctx.buf.add("browse_events", (ctx.ids.next("browse_events"), session_id, self.cid, to_utc(step()),
                                          "view", product["id"], None))
            if rng.random() < 0.06:
                ctx.buf.add("browse_events", (ctx.ids.next("browse_events"), session_id, self.cid,
                                              to_utc(step()), "wishlist_add", product["id"], None))

        if will_order:
            self._order(day_idx, session_id, step)
        elif rng.random() < 0.15 + 0.4 * self.traits.cart_abandon_prob:
            # Adds to cart but doesn't buy: either leaves it there (abandon) or removes it
            cart_value = 0
            items = [self._pick_product() for _ in range(rng.randint(1, 2))]
            for product in items:
                cart_value += product["price_paise"]
                ctx.buf.add("cart_events", (ctx.ids.next("cart_events"), session_id, self.cid, to_utc(step()),
                                            "add", product["id"], 1, cart_value))
            if rng.random() < self.traits.cart_abandon_prob:
                ctx.buf.add("cart_events", (ctx.ids.next("cart_events"), session_id, self.cid, to_utc(step()),
                                            "abandon", None, 1, cart_value))
            else:
                for product in items:
                    cart_value -= product["price_paise"]
                    ctx.buf.add("cart_events", (ctx.ids.next("cart_events"), session_id, self.cid,
                                                to_utc(step()), "remove", product["id"], 1, cart_value))

        end = step()
        if source is None:
            source = "ad" if rng.random() < 0.15 else "organic"
        ctx.buf.add("sessions", (session_id, self.cid, to_utc(start), to_utc(end), self.identity["device"], source))

    def _pick_product(self, category: str | None = None) -> dict:
        """
        Pick a product this customer would plausibly look at or buy.

        Often one of their favourites (how often depends on the persona's repeat rate); otherwise from
        a category they like. VIPs lean to the pricier option, price-sensitive people to the cheaper one.
        """
        rng, t = self.rng, self.traits
        if category is None and rng.random() < self.spec.repeat_rate:
            return self.ctx.products_by_id[rng.choice(t.favourite_product_ids)]
        if category is None:
            category = rng.choices(list(t.category_weights), weights=list(t.category_weights.values()))[0]
        pool = self.ctx.products_by_category[category]
        options = [rng.choice(pool) for _ in range(2)]
        if self.spec.prefers_expensive:
            return max(options, key=lambda p: p["price_paise"])
        if t.price_sensitivity > 0.6:
            return min(options, key=lambda p: p["price_paise"])
        return options[0]

    # -- orders ----------------------------------------------------------------------------------
    def _order(self, day_idx: int, session_id: int, step) -> None:
        ctx, rng = self.ctx, self.rng
        order_id = ctx.ids.next("orders")
        offer = self.active_offer["record"] if self.active_offer else None
        offer_working = self._offer_is_working()
        campaign: Campaign | None = offer["campaign"] if offer else None

        # Choose items. If an opened offer targets a category, the first item comes from it:
        # that's what the offer pulled them in for.
        items: list[dict] = []
        if offer_working and offer["category"]:
            items.append(self._pick_product(offer["category"]))
        basket_size = rng.randint(*self.spec.basket_items)
        while len(items) < basket_size:
            items.append(self._pick_product())
        # Offers with a minimum order make people add one more item to reach it
        if offer_working and campaign.min_order_paise:
            for _ in range(2):
                if sum(p["price_paise"] for p in items) >= campaign.min_order_paise:
                    break
                items.append(self._pick_product(offer["category"]))

        lines, cart_value = [], 0
        for product in items:
            qty = rng.randint(1, 3) if product["category"] == "grocery" else 1
            lines.append((product, qty))
            cart_value += product["price_paise"] * qty
            ctx.buf.add("cart_events", (ctx.ids.next("cart_events"), session_id, self.cid, to_utc(step()),
                                        "add", product["id"], qty, cart_value))
        checkout_time = step()
        ctx.buf.add("cart_events", (ctx.ids.next("cart_events"), session_id, self.cid, to_utc(checkout_time),
                                    "checkout", None, 1, cart_value))
        gross = cart_value

        # Apply a past campaign offer, if this order qualifies
        discount, waive_delivery, points_multiplier, cashback_paise, redeemed = 0, False, 1, 0, False
        if offer:
            offer["purchased"] = 1  # bought inside the window (counts for holdout too)
        if offer_working and gross >= campaign.min_order_paise:
            in_target = [(p, q) for p, q in lines if offer["category"] is None or p["category"] == offer["category"]]
            if in_target:
                redeemed = True
                if campaign.offer_type == "pct_off":
                    discount = round(sum(p["price_paise"] * q for p, q in in_target) * campaign.value / 100)
                elif campaign.offer_type == "flat_off":
                    discount = int(campaign.value * 100)
                elif campaign.offer_type == "free_delivery":
                    waive_delivery = True
                elif campaign.offer_type == "cashback_to_wallet":
                    cashback_paise = int(campaign.value * 100)
                elif campaign.offer_type == "points_multiplier":
                    points_multiplier = int(campaign.value)
                # early_access: no money effect; buying in the early-access category counts as redeemed

        if discount or cashback_paise:
            self.habit.coupon_order()  # a coupon-discounted order teaches them to wait for the next deal
        if (offer_working and self.traits.habit_break_day is not None and day_idx >= self.traits.habit_break_day
                and self.rng.random() < revival_chance()):
            self.traits.habit_break_day = None  # an offer brought a lapsing customer back: the habit revives
        payable = gross - discount
        delivery_fee = 0 if (waive_delivery or payable >= FREE_DELIVERY_ABOVE_PAISE) else DELIVERY_FEE_PAISE
        net = payable + delivery_fee

        # Use reward points? (points hoarders almost never do)
        points_redeemed = 0
        balance = sum(lot[0] for lot in self.point_lots)
        if balance >= MIN_POINTS_TO_REDEEM and rng.random() < self.spec.points_redeem_prob:
            max_points_by_value = int(net * 0.2) // POINT_VALUE_PAISE  # points can pay up to 20% of the order
            points_redeemed = min(balance, max_points_by_value)
            if points_redeemed:
                self._use_points(points_redeemed)
                net -= points_redeemed * POINT_VALUE_PAISE
                ctx.buf.add("points_ledger", (ctx.ids.next("points_ledger"), self.cid, to_utc(checkout_time),
                                              "redeem", points_redeemed, None, order_id))

        payment = self._payment_method(net)
        if payment == "wallet":
            self.wallet_paise -= net
            ctx.buf.add("wallet_txns", (ctx.ids.next("wallet_txns"), self.cid, to_utc(checkout_time), "spend",
                                        net, self.wallet_paise, order_id))

        has_fashion = any(p["category"] == "fashion" for p, _ in lines)
        roll = rng.random()
        status = "cancelled" if roll < 0.03 else ("returned" if roll < 0.03 + (0.10 if has_fashion else 0.03)
                                                 else "delivered")

        points_earned = 0
        if status == "delivered":
            points_earned = (net // 100 // POINTS_PER_RUPEES) * points_multiplier
            if points_earned:
                expires = checkout_time + timedelta(days=POINTS_EXPIRY_DAYS)
                self.point_lots.append([points_earned, day_idx + POINTS_EXPIRY_DAYS])
                ctx.buf.add("points_ledger", (ctx.ids.next("points_ledger"), self.cid, to_utc(checkout_time),
                                              "earn", points_earned, to_utc(expires), order_id))
            if cashback_paise:
                self.wallet_paise += cashback_paise
                ctx.buf.add("wallet_txns", (ctx.ids.next("wallet_txns"), self.cid,
                                            to_utc(checkout_time + timedelta(days=3)), "cashback",
                                            cashback_paise, self.wallet_paise, order_id))
        elif payment == "wallet" or (payment in ("upi", "card") and rng.random() < 0.5):
            # Refund for cancelled/returned orders goes back to the wallet
            self.wallet_paise += net
            ctx.buf.add("wallet_txns", (ctx.ids.next("wallet_txns"), self.cid,
                                        to_utc(checkout_time + timedelta(days=rng.randint(1, 7))), "refund",
                                        net, self.wallet_paise, order_id))

        ctx.buf.add("orders", (order_id, self.cid, session_id, to_utc(checkout_time), gross, discount, delivery_fee,
                               net, offer["id"] if redeemed else None, payment, status, points_earned,
                               points_redeemed))
        for product, qty in lines:
            ctx.buf.add("order_items", (ctx.ids.next("order_items"), order_id, product["id"], qty,
                                        product["price_paise"]))
        if redeemed:
            offer["redeemed_local"] = checkout_time
            offer["order_id"] = order_id
        self.last_order_idx = day_idx

    def _payment_method(self, net_paise: int) -> str:
        """Wallet if there's enough balance (about half the time); otherwise UPI/card/COD (COD more in small towns)."""
        rng = self.rng
        if self.wallet_paise >= net_paise and rng.random() < 0.55:
            return "wallet"
        cod_weight = 40 if self.identity["city_tier"] == 3 else 20
        return rng.choices(["upi", "card", "cod"], weights=[50, 25, cod_weight])[0]

    # -- wallet and points housekeeping ----------------------------------------------------------
    def _maybe_topup_wallet(self, day_idx: int) -> None:
        if self.rng.random() < self.spec.wallet_topup_per_day:
            amount = self.rng.choice([200, 500, 1000, 2000]) * 100
            self.wallet_paise += amount
            self.ctx.buf.add("wallet_txns", (self.ctx.ids.next("wallet_txns"), self.cid,
                                             to_utc(self._session_start(day_idx)), "topup", amount,
                                             self.wallet_paise, None))

    def _use_points(self, points: int) -> None:
        """Spend points from the oldest lot first (the ones closest to expiring)."""
        while points and self.point_lots:
            take = min(points, self.point_lots[0][0])
            self.point_lots[0][0] -= take
            points -= take
            if self.point_lots[0][0] == 0:
                self.point_lots.popleft()

    def _expire_points(self, day_idx: int) -> None:
        """Points not used within 90 days expire. Points hoarders lose a lot this way."""
        while self.point_lots and self.point_lots[0][1] <= day_idx:
            points_left, _ = self.point_lots.popleft()
            if points_left:
                day = self.ctx.days[day_idx]
                self.ctx.buf.add("points_ledger", (self.ctx.ids.next("points_ledger"), self.cid,
                                                   to_utc(datetime(day.year, day.month, day.day, 0, 5)),
                                                   "expire", points_left, None, None))


# ---------------------------------------------------------------------------------------------------
# Shared context and the main run
# ---------------------------------------------------------------------------------------------------
@dataclass
class SimContext:
    rng: random.Random
    seed: int
    ids: Ids
    buf: Buffers
    days: list[date]
    festival: list[bool]
    payday: list[bool]
    weekend: list[bool]
    campaigns: list[Campaign]
    products_by_id: dict[int, dict]
    products_by_category: dict[str, list[dict]]


def generate(n_customers: int, months: int, end_date: date, seed: int, db_path: Path) -> None:
    started = time.time()
    rng = random.Random(seed)
    n_days = round(months * 365 / 12)
    start_date = end_date - timedelta(days=n_days - 1)
    days = [start_date + timedelta(days=i) for i in range(n_days)]

    products = build_products(rng)
    products_by_category: dict[str, list[dict]] = {}
    for p in products:
        products_by_category.setdefault(p["category"], []).append(p)

    ctx = SimContext(
        rng=rng, seed=seed, ids=Ids(), buf=Buffers(), days=days,
        festival=[festival_name_on(d) is not None for d in days],
        payday=[is_payday(d) for d in days],
        weekend=[is_weekend(d) for d in days],
        campaigns=plan_campaigns(days),
        products_by_id={p["id"]: p for p in products},
        products_by_category=products_by_category,
    )

    conn = connect(db_path)
    init_schema(conn)
    # Orders and offers point at each other, so links are checked once at the end instead of per insert
    conn.execute("PRAGMA foreign_keys = OFF")

    insert_many(conn, "products", COLUMNS["products"], [tuple(p[c] for c in COLUMNS["products"]) for p in products])
    insert_many(conn, "calendar", COLUMNS["calendar"], build_calendar(start_date, end_date))
    insert_many(conn, "campaigns", COLUMNS["campaigns"],
                [(c.id, c.name, days[c.day_idx].isoformat(), c.offer_type, c.value, c.target) for c in ctx.campaigns])
    conn.commit()

    for customer_id in range(1, n_customers + 1):
        spec = choose_persona(rng)
        traits = sample_traits(spec, rng, products_by_category, n_days)
        identity = build_customer_identity(rng, spec.age_range)
        signup_days_ago = rng.randint(*spec.signup_days_before_end)
        signup_date = end_date - timedelta(days=signup_days_ago)
        signup_idx = max(0, n_days - signup_days_ago)

        ctx.buf.add("customers", (customer_id, identity["first_name"], identity["last_name"], identity["gender"],
                                  identity["age"], identity["city"], identity["city_tier"], signup_date.isoformat(),
                                  identity["device"], identity["language"], identity["marketing_consent"], None))
        sim = CustomerSimulator(customer_id, spec, traits, identity, signup_idx, ctx)
        sim.run()

        break_date = days[traits.habit_break_day].isoformat() if traits.habit_break_day is not None else None
        ctx.buf.add("customer_traits", (customer_id, traits.persona, traits.price_sensitivity, traits.deal_affinity,
                                        traits.churn_propensity, traits.spam_tolerance, traits.base_order_gap_days,
                                        traits.habit_regularity, traits.cart_abandon_prob, traits.peak_hour,
                                        json.dumps(traits.category_weights), json.dumps(traits.favourite_product_ids),
                                        break_date, json.dumps(traits.true_uplift),
                                        round(sim.habit.wait_for_deal, 4)))

        if customer_id % FLUSH_EVERY == 0:
            ctx.buf.flush(conn)
            print(f"  {customer_id}/{n_customers} customers simulated...")
    ctx.buf.flush(conn)

    problems = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.execute("PRAGMA foreign_keys = ON")
    print(f"\nDone in {time.time() - started:.0f}s -> {db_path}")
    print(f"Data window: {start_date} to {end_date} ({n_days} days). Broken links between tables: {len(problems)}")
    print_summary(conn)
    conn.close()


# ---------------------------------------------------------------------------------------------------
# Summary: proves the patterns are really in the data
# ---------------------------------------------------------------------------------------------------
def print_summary(conn) -> None:
    print("\nRows per table:")
    for table in ["customers", "products", "sessions", "browse_events", "cart_events", "orders", "order_items",
                  "wallet_txns", "points_ledger", "campaigns", "offers_history", "calendar"]:
        print(f"  {table:<16}{conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:>10,}")

    end_date = conn.execute("SELECT MAX(date) FROM calendar").fetchone()[0]
    rows = conn.execute(f"""
        WITH o AS (
            SELECT customer_id, COUNT(*) n, SUM(net_paise) spend, MAX(ts) last_ts,
                   (julianday(MAX(ts)) - julianday(MIN(ts))) / NULLIF(COUNT(*) - 1, 0) avg_gap
            FROM orders GROUP BY customer_id),
        ab AS (SELECT customer_id, SUM(event_type = 'abandon') abandons FROM cart_events GROUP BY customer_id),
        pts AS (SELECT customer_id, SUM(CASE entry_type WHEN 'earn' THEN points ELSE -points END) bal,
                       SUM(CASE entry_type WHEN 'expire' THEN points ELSE 0 END) expired
                FROM points_ledger GROUP BY customer_id)
        SELECT t.persona, COUNT(*) customers,
               ROUND(AVG(COALESCE(o.n, 0)), 1) orders,
               ROUND(AVG(o.avg_gap), 1) gap,
               ROUND(AVG(COALESCE(o.spend, 0)) / 100) spend_rs,
               ROUND(AVG(COALESCE(ab.abandons, 0)), 1) abandons,
               ROUND(100.0 * AVG(CASE WHEN o.avg_gap IS NOT NULL AND
                   (julianday('{end_date}') - julianday(o.last_ts)) >= 1.5 * o.avg_gap THEN 1 ELSE 0 END)) overdue_pct,
               ROUND(AVG(COALESCE(pts.bal, 0))) points_bal,
               ROUND(AVG(COALESCE(pts.expired, 0))) points_expired
        FROM customer_traits t
        LEFT JOIN o ON o.customer_id = t.customer_id
        LEFT JOIN ab ON ab.customer_id = t.customer_id
        LEFT JOIN pts ON pts.customer_id = t.customer_id
        GROUP BY t.persona ORDER BY customers DESC""").fetchall()
    print("\nPersona patterns (averages per customer):")
    print(f"  {'persona':<16}{'cust':>6}{'orders':>8}{'gap(d)':>8}{'spend Rs':>10}{'abandons':>9}"
          f"{'overdue%':>9}{'points':>8}{'expired':>8}")
    for r in rows:
        print(f"  {r['persona']:<16}{r['customers']:>6}{r['orders']:>8}{r['gap'] or 0:>8}{r['spend_rs']:>10,.0f}"
              f"{r['abandons']:>9}{r['overdue_pct']:>9.0f}{r['points_bal']:>8.0f}{r['points_expired']:>8.0f}")

    uplift = conn.execute("""
        SELECT t.persona,
               AVG(CASE WHEN h.group_name = 'treatment' THEN h.purchased_in_window END) treat,
               AVG(CASE WHEN h.group_name = 'holdout' THEN h.purchased_in_window END) hold
        FROM offers_history h JOIN customer_traits t ON t.customer_id = h.customer_id
        GROUP BY t.persona ORDER BY (treat - hold) DESC""").fetchall()
    print("\nPast campaigns: buy rate within 48h, offer sent vs holdout (the difference = real effect of offers):")
    for r in uplift:
        treat, hold = (r["treat"] or 0) * 100, (r["hold"] or 0) * 100
        print(f"  {r['persona']:<16} sent {treat:5.1f}%   holdout {hold:5.1f}%   effect {treat - hold:+5.1f} pts")

    fest = conn.execute("""
        SELECT c.is_festival, COUNT(o.id) * 1.0 / COUNT(DISTINCT c.date) per_day
        FROM calendar c LEFT JOIN orders o ON substr(o.ts, 1, 10) = c.date
        GROUP BY c.is_festival""").fetchall()
    rates = {r["is_festival"]: r["per_day"] for r in fest}
    print(f"\nOrders per day: festival days {rates.get(1, 0):.0f} vs normal days {rates.get(0, 0):.0f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the synthetic OfferPilot company dataset.")
    parser.add_argument("--customers", type=int, default=3000)
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today() - timedelta(days=1),
                        help="last day of history (default: yesterday)")
    parser.add_argument("--db", type=Path, default=settings.db_path)
    parser.add_argument("--force", action="store_true", help="delete and rebuild an existing database")
    args = parser.parse_args()

    if args.db.exists():
        if not args.force:
            raise SystemExit(f"{args.db} already exists. It may hold decisions and testers. "
                             f"Re-run with --force to delete it and rebuild.")
        args.db.unlink()

    print(f"Generating {args.customers} customers x {args.months} months (seed {args.seed})...")
    generate(args.customers, args.months, args.end_date, args.seed, args.db)


if __name__ == "__main__":
    main()
