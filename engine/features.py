"""
The analyst: turns a customer's raw history into one short, readable profile.

Where it sits: STEP 2 of the pipeline.
    SQLite (orders, sessions, carts, wallet, points, past offers) -> build_profile() -> gatekeeper, agent,
    profit math, rules, fallback, dashboard

Why it exists: the agent can't read 500 rows per customer (slow and costly), and numbers like "how overdue is
this customer" must be calculated the same way every time. So code does the counting; the agent gets the
summary through its `get_customer_profile` tool.

What's in a profile (all from observable behaviour; the hidden `customer_traits` table is NEVER read here):
  - habit:      usual gap between orders, days since last order, overdue ratio, habit strength and status
  - favourites: items ordered 3+ times, favourite categories by spend
  - timing:     usual shopping hour (IST)
  - browsing:   recent views and searches
  - cart:       abandon rate, and any cart left open in the last 72 hours
  - money:      discount dependence, wallet balance and idle days, points balance and points expiring soon
  - offers:     how they reacted to past offers, and how many offers they got recently (fatigue)
  - estimate:   chance they buy in the next 48 hours WITHOUT any offer (the "buys anyway" number)
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from engine.catalog import Catalog
from engine.clock import days_between, parse_ts, to_ist, to_ts
from engine.learning import shop_mode

HIGH_VALUE_SPEND_RUPEES = 20_000   # lifetime spend above this = high-value customer
OPEN_CART_HOURS = 72               # an abandoned cart counts as "open" for 3 days
FAVOURITE_MIN_ORDERS = 3           # ordered 3+ times = favourite item
SENT_STATUSES = ("sent", "queued", "scheduled", "would_send")  # decisions that count as "an offer went out"
LOYAL_MIN_ORDERS = 5               # same as config/rules.yaml fairness.loyal_min_orders (default); used for the label


@dataclass
class CustomerProfile:
    """Everything the rest of the pipeline needs to know about one customer, in plain numbers."""

    customer_id: int
    first_name: str
    city: str
    city_tier: int
    age: int
    language: str
    marketing_consent: bool
    telegram_linked: bool
    days_since_signup: int

    # habit
    total_orders: int
    total_spend_rupees: int
    avg_order_value_rupees: int
    days_since_last_order: float | None
    usual_gap_days: float | None
    overdue_ratio: float | None
    habit_strength: float | None
    habit_status: str
    orders_last_30d: int
    orders_last_90d: int
    return_rate: float
    high_value: bool
    sale_order_share: float       # share of orders placed during festival sales
    deal_seeker_score: float      # 0..1, how strongly they wait for deals

    # favourites and timing
    favourite_items: list[dict] = field(default_factory=list)
    favourite_categories: list[str] = field(default_factory=list)
    category_spend_share: dict[str, float] = field(default_factory=dict)
    usual_shopping_hour_ist: int | None = None

    # browsing and cart
    sessions_last_30d: int = 0
    views_last_7d: int = 0
    recently_viewed: list[str] = field(default_factory=list)
    recent_searches: list[str] = field(default_factory=list)
    cart_abandon_rate: float = 0.0
    open_cart: dict | None = None

    # money
    discount_dependence: float = 0.0
    full_price_share_90d: float | None = None   # share of last-90-day orders with no coupon and not on a sale day
    full_price_share_12m: float | None = None   # the same over 12 months (sale seasons average out)
    discounted_orders_90d: int = 0              # coupon-discounted orders in the last 90 days
    wallet_balance_rupees: int = 0
    wallet_idle_days: float | None = None
    points_balance: int = 0
    points_expiring_7d: int = 0
    points_next_expiry: str | None = None
    points_redeem_rate: float = 0.0

    # offers
    offer_response: dict[str, dict] = field(default_factory=dict)
    recent_offers: list[dict] = field(default_factory=list)
    offers_last_7d: int = 0
    offers_last_30d: int = 0
    last_offer_at: str | None = None

    # estimate
    p_buy_48h_no_offer: float = 0.0

    # interest: products they searched for, viewed repeatedly, wishlisted or left in the cart (strongest first)
    intent_products: list[dict] = field(default_factory=list)
    is_loyal: bool = False        # enough orders to be called a loyal customer (rules: fairness.loyal_min_orders)
    learning_mode: bool = False   # real shop without past campaigns: no uplift model, conservative formula
    uplift_factor: float = 1.0    # conservative scaling of the formula in learning mode (engine/learning.py)
    as_of: str = ""               # the moment (UTC) this profile describes

    def to_dict(self) -> dict:
        return asdict(self)


def build_profile(conn: sqlite3.Connection, customer_id: int, now: datetime, catalog: Catalog) -> CustomerProfile:
    """Build the profile of one customer as of `now`. Only data with a timestamp before `now` is used."""
    now_ts = to_ts(now)
    c = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if c is None:
        raise ValueError(f"Customer {customer_id} not found")

    habit = _habit(conn, customer_id, now, now_ts)
    favourites, category_share = _favourites(conn, customer_id, now_ts, catalog)
    offer_response, recent_offers, offers_7d, offers_30d, last_offer_at = _offer_history(conn, customer_id, now)
    points = _points(conn, customer_id, now, now_ts)
    wallet_balance, wallet_idle = _wallet(conn, customer_id, now, now_ts)
    browsing = _browsing(conn, customer_id, now)

    profile = CustomerProfile(
        customer_id=customer_id,
        first_name=c["first_name"],
        city=c["city"],
        city_tier=c["city_tier"],
        age=c["age"],
        language=c["language"],
        marketing_consent=bool(c["marketing_consent"]),
        telegram_linked=c["telegram_chat_id"] is not None,
        days_since_signup=int(days_between(datetime.fromisoformat(c["signup_date"]).replace(tzinfo=now.tzinfo), now)),
        **habit,
        favourite_items=favourites,
        favourite_categories=sorted(category_share, key=category_share.get, reverse=True)[:3],
        category_spend_share=category_share,
        usual_shopping_hour_ist=browsing["usual_hour"],
        sessions_last_30d=browsing["sessions_30d"],
        views_last_7d=browsing["views_7d"],
        recently_viewed=browsing["recently_viewed"],
        recent_searches=browsing["recent_searches"],
        cart_abandon_rate=browsing["abandon_rate"],
        open_cart=_open_cart(conn, customer_id, now, catalog),
        wallet_balance_rupees=wallet_balance,
        wallet_idle_days=wallet_idle,
        **points,
        offer_response=offer_response,
        recent_offers=recent_offers,
        offers_last_7d=offers_7d,
        offers_last_30d=offers_30d,
        last_offer_at=last_offer_at,
    )
    profile.as_of = now_ts
    profile.intent_products = _intent_products(conn, customer_id, now, catalog, profile.open_cart)
    profile.is_loyal = profile.total_orders >= LOYAL_MIN_ORDERS
    profile.p_buy_48h_no_offer = estimate_p_buy_no_offer(profile)
    mode = shop_mode(conn)
    profile.learning_mode, profile.uplift_factor = mode["learning_mode"], mode["conservative_uplift_factor"]
    # If the uplift model has been trained, its "no offer" prediction replaces the hand-made formula
    # (never in learning mode: a model trained on another shop must not judge this one)
    from engine.uplift import get_model  # imported here to avoid a circular import
    model = None if profile.learning_mode else get_model()
    if model:
        profile.p_buy_48h_no_offer = round(model.p_no_offer(profile), 3)
    return profile


# ---------------------------------------------------------------------------------------------------
# Habit: the heart of "keep customers ordering"
# ---------------------------------------------------------------------------------------------------
def _habit(conn: sqlite3.Connection, customer_id: int, now: datetime, now_ts: str) -> dict:
    """
    Order rhythm. usual_gap = median days between orders (median, so one long holiday doesn't distort it).
    overdue_ratio = days since last order / usual gap: 1.0 = due today, 1.5+ = habit breaking.
    habit_strength = how regular the gaps are (1 = like clockwork, 0 = random).
    """
    rows = conn.execute("""
        SELECT o.ts, o.net_paise, o.discount_paise, o.status, COALESCE(c.is_festival, 0) on_sale
        FROM orders o LEFT JOIN calendar c ON c.date = substr(o.ts, 1, 10)
        WHERE o.customer_id = ? AND o.ts <= ? ORDER BY o.ts""", (customer_id, now_ts)).fetchall()
    placed = [r for r in rows if r["status"] != "cancelled"]
    times = [parse_ts(r["ts"]) for r in placed]
    spend = sum(r["net_paise"] for r in placed if r["status"] == "delivered")

    gaps = [days_between(a, b) for a, b in zip(times, times[1:]) if days_between(a, b) > 0.5]
    usual_gap = round(statistics.median(gaps), 1) if gaps else None
    habit_strength = None
    if len(gaps) >= 3:
        mean = statistics.mean(gaps)
        habit_strength = round(max(0.0, 1 - statistics.pstdev(gaps) / mean), 2) if mean else 0.0

    days_since = round(days_between(times[-1], now), 1) if times else None
    overdue = round(days_since / usual_gap, 2) if (usual_gap and days_since is not None) else None

    if not times:
        status = "no orders yet"
    elif usual_gap is None:
        status = "only one order so far"
    elif overdue < 0.8:
        status = "on track"
    elif overdue < 1.5:
        status = "due now"
    elif overdue < 3:
        status = "overdue - habit breaking"
    else:
        status = "lapsed - habit broken"

    returned = sum(1 for r in placed if r["status"] == "returned")
    # Deal seeking: about 20% of days are sale days, so a normal shopper places ~20% of orders in sales.
    # Someone at 60%+ is clearly waiting for deals -> discounts will move them more.
    sale_share = round(sum(r["on_sale"] for r in placed) / len(placed), 2) if placed else 0.0
    return {
        "sale_order_share": sale_share,
        "deal_seeker_score": round(min(1.0, max(0.0, (sale_share - 0.2) / 0.5)), 2) if len(placed) >= 3 else 0.0,
        "total_orders": len(placed),
        "total_spend_rupees": spend // 100,
        "avg_order_value_rupees": (sum(r["net_paise"] for r in placed) // len(placed) // 100) if placed else 0,
        "days_since_last_order": days_since,
        "usual_gap_days": usual_gap,
        "overdue_ratio": overdue,
        "habit_strength": habit_strength,
        "habit_status": status,
        "orders_last_30d": sum(1 for t in times if days_between(t, now) <= 30),
        "orders_last_90d": sum(1 for t in times if days_between(t, now) <= 90),
        "return_rate": round(returned / len(placed), 2) if placed else 0.0,
        "high_value": spend // 100 >= HIGH_VALUE_SPEND_RUPEES,
        "discount_dependence": round(sum(1 for r in placed if r["discount_paise"] > 0) / len(placed), 2)
        if placed else 0.0,
        **_full_price(placed, times, now),
    }


FULL_PRICE_MIN_ORDERS = 3      # fewer orders than this: too little to judge a buying habit


def _full_price(placed: list, times: list[datetime], now: datetime) -> dict:
    """
    Habit protection signal: how much of their buying happens at FULL price (no coupon, not on a sale day).
    Someone who mostly pays full price is a healthy customer; coupons would only teach them to wait for deals.

    Two windows, None below 3 orders in the window:
      - 90 days: recent, but seasonal. In Aug-Oct about a third of days are sale days (Independence Day, Big Billion
        Days, Diwali) and everyone buys more then, so even regulars look deal-driven in festival season.
      - 12 months: sale seasons average out. On our data it separates cleanly: loyal regulars, VIPs and points
        collectors ~0.78, deal hunters ~0.50, festival-only buyers 0. The habit rule uses this one by default
        (rules.yaml habit.share_window).
    """
    def share(rows: list) -> float | None:
        if len(rows) < FULL_PRICE_MIN_ORDERS:
            return None
        return round(sum(1 for r in rows if not r["discount_paise"] and not r["on_sale"]) / len(rows), 2)

    recent = [r for r, t in zip(placed, times) if days_between(t, now) <= 90]
    year = [r for r, t in zip(placed, times) if days_between(t, now) <= 365]
    return {"full_price_share_90d": share(recent), "full_price_share_12m": share(year),
            "discounted_orders_90d": sum(1 for r in recent if r["discount_paise"] > 0)}


def _favourites(conn: sqlite3.Connection, customer_id: int, now_ts: str, catalog: Catalog
                ) -> tuple[list[dict], dict[str, float]]:
    """Items ordered 3+ times (best targets for 'time to restock?' offers) and share of spend per category."""
    rows = conn.execute("""
        SELECT oi.product_id, oi.qty, oi.unit_price_paise, o.ts
        FROM order_items oi JOIN orders o ON o.id = oi.order_id
        WHERE o.customer_id = ? AND o.ts <= ? AND o.status != 'cancelled'""", (customer_id, now_ts)).fetchall()
    times_ordered: Counter = Counter()
    last_bought: dict[int, str] = {}
    category_spend: defaultdict[str, int] = defaultdict(int)
    for r in rows:
        times_ordered[r["product_id"]] += 1
        last_bought[r["product_id"]] = max(last_bought.get(r["product_id"], ""), r["ts"])
        product = catalog.get(r["product_id"])
        if product:
            category_spend[product.category] += r["qty"] * r["unit_price_paise"]

    favourites = []
    for product_id, count in times_ordered.most_common():
        if count < FAVOURITE_MIN_ORDERS or len(favourites) >= 5:
            break
        p = catalog.get(product_id)
        favourites.append({"product_id": product_id, "name": p.name, "category": p.category,
                           "price_rupees": p.price_paise // 100, "times_ordered": count,
                           "last_bought": last_bought[product_id][:10]})
    total = sum(category_spend.values()) or 1
    share = {cat: round(v / total, 2) for cat, v in sorted(category_spend.items(), key=lambda kv: -kv[1])}
    return favourites, share


# ---------------------------------------------------------------------------------------------------
# Browsing and cart
# ---------------------------------------------------------------------------------------------------
def _browsing(conn: sqlite3.Connection, customer_id: int, now: datetime) -> dict:
    since_90 = to_ts(now - timedelta(days=90))
    since_30 = to_ts(now - timedelta(days=30))
    since_7 = to_ts(now - timedelta(days=7))
    now_ts = to_ts(now)

    starts = [parse_ts(r[0]) for r in conn.execute(
        "SELECT start_ts FROM sessions WHERE customer_id = ? AND start_ts BETWEEN ? AND ?",
        (customer_id, since_90, now_ts))]
    hours = Counter(to_ist(s).hour for s in starts)

    views = conn.execute("""
        SELECT p.name FROM browse_events b JOIN products p ON p.id = b.product_id
        WHERE b.customer_id = ? AND b.event_type = 'view' AND b.ts BETWEEN ? AND ? ORDER BY b.ts DESC""",
                         (customer_id, since_7, now_ts)).fetchall()
    recently_viewed = list(dict.fromkeys(r[0] for r in views))[:5]
    searches = [r[0] for r in conn.execute("""
        SELECT query FROM browse_events WHERE customer_id = ? AND event_type = 'search' AND ts <= ?
        ORDER BY ts DESC LIMIT 3""", (customer_id, now_ts))]

    cart = conn.execute("""
        SELECT SUM(event_type = 'abandon'), SUM(event_type = 'checkout') FROM cart_events
        WHERE customer_id = ? AND ts BETWEEN ? AND ?""", (customer_id, since_90, now_ts)).fetchone()
    abandons, checkouts = (cart[0] or 0), (cart[1] or 0)
    return {
        "usual_hour": hours.most_common(1)[0][0] if hours else None,
        "sessions_30d": sum(1 for s in starts if s >= parse_ts(since_30)),
        "views_7d": len(views),
        "recently_viewed": recently_viewed,
        "recent_searches": searches,
        "abandon_rate": round(abandons / (abandons + checkouts), 2) if (abandons + checkouts) else 0.0,
    }


def _intent_products(conn: sqlite3.Connection, customer_id: int, now: datetime, catalog: Catalog,
                     open_cart: dict | None) -> list[dict]:
    """
    Products this customer has shown interest in recently, strongest first (max 3):
      - left in an open cart           (strongest signal)
      - added to wishlist (14 days)
      - matches what they searched for (7 days), and they looked at it
      - viewed 2+ times (7 days)
    Products they bought in the last 7 days are left out (they already have them).
    This lets offers target exactly what the customer wants right now: "Still thinking about the Mira lipstick?"
    """
    now_ts = to_ts(now)
    since_14 = to_ts(now - timedelta(days=14))
    since_7 = to_ts(now - timedelta(days=7))
    score: defaultdict[int, float] = defaultdict(float)
    signals: defaultdict[int, list[str]] = defaultdict(list)

    for item in (open_cart or {}).get("items", []):
        score[item["product_id"]] += 3
        signals[item["product_id"]].append(f"left in cart {open_cart['left_hours_ago']:.0f}h ago")
    views: Counter = Counter()
    searches: list[str] = []
    for r in conn.execute("SELECT event_type, product_id, query, ts FROM browse_events WHERE customer_id = ? "
                          "AND ts BETWEEN ? AND ?", (customer_id, since_14, now_ts)):
        if r["event_type"] == "wishlist_add" and r["product_id"]:
            score[r["product_id"]] += 2
            signals[r["product_id"]].append("added to wishlist")
        elif r["event_type"] == "view" and r["product_id"] and r["ts"] >= since_7:
            views[r["product_id"]] += 1
        elif r["event_type"] == "search" and r["query"] and r["ts"] >= since_7:
            searches.append(r["query"].lower())
    for product_id, count in views.items():
        product = catalog.get(product_id)
        if not product:
            continue
        matched = [q for q in searches if product.subcategory.lower() in q]
        if matched:
            score[product_id] += 2
            signals[product_id].append(f"searched '{matched[0]}'")
        if count >= 2:
            score[product_id] += count
            signals[product_id].append(f"viewed {count} times this week")

    bought = {r[0] for r in conn.execute("""SELECT oi.product_id FROM order_items oi JOIN orders o ON o.id = oi.order_id
                                            WHERE o.customer_id = ? AND o.ts BETWEEN ? AND ?""",
                                         (customer_id, since_7, now_ts))}
    ranked = sorted((pid for pid in score if pid not in bought and catalog.get(pid) and signals[pid]),
                    key=lambda pid: -score[pid])
    out = []
    for product_id in ranked[:3]:
        p = catalog.get(product_id)
        out.append({"product_id": p.id, "name": p.name, "category": p.category, "price_rupees": p.price_paise // 100,
                    "signals": list(dict.fromkeys(signals[product_id]))})
    return out


def _open_cart(conn: sqlite3.Connection, customer_id: int, now: datetime, catalog: Catalog) -> dict | None:
    """
    The most recent abandoned cart, if it was left in the last 72 hours and they haven't ordered since.
    That's the moment for a cart offer: the intent is fresh.
    """
    now_ts = to_ts(now)
    last = conn.execute("""
        SELECT session_id, ts, cart_value_paise FROM cart_events
        WHERE customer_id = ? AND event_type = 'abandon' AND ts <= ? ORDER BY ts DESC LIMIT 1""",
                        (customer_id, now_ts)).fetchone()
    if not last:
        return None
    left_at = parse_ts(last["ts"])
    hours_ago = days_between(left_at, now) * 24
    if hours_ago > OPEN_CART_HOURS:
        return None
    ordered_since = conn.execute("SELECT 1 FROM orders WHERE customer_id = ? AND ts > ? AND ts <= ? LIMIT 1",
                                 (customer_id, last["ts"], now_ts)).fetchone()
    if ordered_since:
        return None
    items = [catalog.get(r[0]) for r in conn.execute(
        "SELECT product_id FROM cart_events WHERE session_id = ? AND event_type = 'add'", (last["session_id"],))]
    items = [p for p in items if p]
    return {
        "left_hours_ago": round(hours_ago, 1),
        "cart_value_rupees": last["cart_value_paise"] // 100,
        "items": [{"product_id": p.id, "name": p.name, "category": p.category,
                   "price_rupees": p.price_paise // 100} for p in items],
    }


# ---------------------------------------------------------------------------------------------------
# Wallet and points
# ---------------------------------------------------------------------------------------------------
def _wallet(conn: sqlite3.Connection, customer_id: int, now: datetime, now_ts: str) -> tuple[int, float | None]:
    last = conn.execute("""
        SELECT ts, balance_after_paise FROM wallet_txns WHERE customer_id = ? AND ts <= ?
        ORDER BY ts DESC, id DESC LIMIT 1""", (customer_id, now_ts)).fetchone()
    if not last:
        return 0, None
    return last["balance_after_paise"] // 100, round(days_between(parse_ts(last["ts"]), now), 1)


def _points(conn: sqlite3.Connection, customer_id: int, now: datetime, now_ts: str) -> dict:
    """
    Replays the points ledger oldest-first to know exactly which points are left and when each lot expires.
    'Points expiring in 7 days' is a free reason to message someone (agent priority 3: zero-cost nudges).
    """
    lots: list[list] = []  # [points_left, expires_at]
    earned = redeemed = 0
    for r in conn.execute("SELECT entry_type, points, expires_at FROM points_ledger WHERE customer_id = ? "
                          "AND ts <= ? ORDER BY ts, id", (customer_id, now_ts)):
        if r["entry_type"] == "earn":
            lots.append([r["points"], r["expires_at"]])
            earned += r["points"]
            continue
        if r["entry_type"] == "redeem":
            redeemed += r["points"]
        remaining = r["points"]
        while remaining and lots:
            take = min(remaining, lots[0][0])
            lots[0][0] -= take
            remaining -= take
            if lots[0][0] == 0:
                lots.pop(0)

    live = [(pts, exp) for pts, exp in lots if exp and parse_ts(exp) > now]
    soon = [(pts, exp) for pts, exp in live if parse_ts(exp) <= now + timedelta(days=7)]
    return {
        "points_balance": sum(p for p, _ in live),
        "points_expiring_7d": sum(p for p, _ in soon),
        "points_next_expiry": min((exp for _, exp in soon), default=None),
        "points_redeem_rate": round(redeemed / earned, 2) if earned else 0.0,
    }


# ---------------------------------------------------------------------------------------------------
# Offer history and fatigue
# ---------------------------------------------------------------------------------------------------
def _offer_history(conn: sqlite3.Connection, customer_id: int, now: datetime
                   ) -> tuple[dict, list[dict], int, int, str | None]:
    """
    How they reacted to offers before (per offer type: sent / opened / redeemed) and how many offers they got
    recently. Combines past campaigns (offers_history, only the 'treatment' rows: holdout people got nothing)
    and offers OfferPilot itself decided (decisions table).
    """
    now_ts = to_ts(now)
    response: dict[str, dict] = defaultdict(lambda: {"sent": 0, "opened": 0, "redeemed": 0})
    sent_times: list[datetime] = []
    recent: list[dict] = []

    for r in conn.execute("""
        SELECT offer_type, value, category, sent_at, opened_at, redeemed_at FROM offers_history
        WHERE customer_id = ? AND group_name = 'treatment' AND sent_at <= ? ORDER BY sent_at""",
                          (customer_id, now_ts)):
        stats = response[r["offer_type"]]
        stats["sent"] += 1
        stats["opened"] += r["opened_at"] is not None
        stats["redeemed"] += r["redeemed_at"] is not None
        sent_times.append(parse_ts(r["sent_at"]))
        recent.append({"date": r["sent_at"][:10], "offer_type": r["offer_type"], "value": r["value"],
                       "category": r["category"],
                       "outcome": "used" if r["redeemed_at"] else ("opened, not used" if r["opened_at"] else "ignored")})

    for r in conn.execute(f"""
        SELECT final_offer, COALESCE(send_at, created_at) t FROM decisions
        WHERE customer_id = ? AND status IN ({','.join('?' * len(SENT_STATUSES))}) AND created_at <= ?""",
                          (customer_id, *SENT_STATUSES, now_ts)):
        sent_times.append(parse_ts(r["t"]))

    sent_times.sort()
    for stats in response.values():
        stats["redeem_rate"] = round(stats["redeemed"] / stats["sent"], 2) if stats["sent"] else 0.0
    return (
        dict(response),
        recent[-5:][::-1],
        sum(1 for t in sent_times if 0 <= days_between(t, now) <= 7),
        sum(1 for t in sent_times if 0 <= days_between(t, now) <= 30),
        to_ts(sent_times[-1]) if sent_times else None,
    )


# ---------------------------------------------------------------------------------------------------
# "Buys anyway" estimate
# ---------------------------------------------------------------------------------------------------
def estimate_p_buy_no_offer(profile: CustomerProfile) -> float:
    """
    Chance this customer orders in the next 48 hours with NO offer.

    Logic: a regular customer becomes very likely to order as they reach their usual gap (and stays likely a
    little past it). The more regular they are, the sharper that peak. Far past their gap (3x+), the habit has
    probably broken, so the chance drops. Customers with 0-1 orders get a small base chance.

    High number = "would buy anyway" -> discounts would only lose margin.
    TODO(session 7): replace this formula with an uplift model trained on past campaigns (treatment vs holdout).
    """
    if profile.total_orders == 0:
        daily = 0.01
    elif profile.usual_gap_days is None:
        daily = 0.02
    else:
        gap = max(profile.usual_gap_days, 1.0)
        x = (profile.days_since_last_order or 0) / gap
        k = 1 + 3 * (profile.habit_strength or 0.3)
        # (k+1)*x^k averages to 1 over one cycle, so on average they order once per gap
        daily = (k + 1) * min(x, 1.2) ** k / gap
        if x > 1.3:
            daily *= math.exp(-2 * (x - 1.3))  # overdue: the habit is breaking, the natural chance fades fast
    daily = min(daily, 0.9)
    return round(1 - (1 - daily) ** 2, 3)
