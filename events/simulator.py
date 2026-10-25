"""
The event simulator: makes "live" shopping moments happen, then lets the pipeline react to them.

Where it sits: STEP 3 (triggers). There's no real shop, so this creates the moments a real shop would see, as real
rows in the database (sessions, searches, product views, carts), then calls decide() with the matching trigger.
Delivery is the bot's job: if the customer is linked to a tester, the bot sends the approved offer.

Fire one event (best for demos: watch the offer arrive on your Telegram):
    python -m events.simulator fire cart   --customer 648                 # left a cart (3h ago by default)
    python -m events.simulator fire search --customer 648 --query lipstick
    python -m events.simulator fire view   --customer 648 --product 137   # viewed one product 3 times
    python -m events.simulator fire none   --customer 648                 # just decide with the strongest trigger
Background traffic (random customers + every linked tester), and follow-ups for "wait" decisions:
    python -m events.simulator tick --customers 20
    python -m events.simulator loop --speed 60          # one tick every 60 seconds

Cost guard: by default only linked testers' customers get AI decisions ("--ai-for testers"); background customers
use the free fallback rules. "--ai-for all" uses the AI for everyone (still capped by today's Day Plan).
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta

from db.store import connect
from engine.catalog import Catalog, load_catalog
from engine.clock import now_utc, to_ts
from engine.decide import decide, format_decision
from engine.features import build_profile
from engine.followups import run_due_followups
from engine.triggers import detect_triggers


# ---------------------------------------------------------------------------------------------------
# Creating live events as real data
# ---------------------------------------------------------------------------------------------------
def _session(conn: sqlite3.Connection, customer_id: int, start: datetime, minutes: int = 6) -> int:
    device = conn.execute("SELECT device FROM customers WHERE id = ?", (customer_id,)).fetchone()[0]
    cur = conn.execute("INSERT INTO sessions (customer_id, start_ts, end_ts, device, source) VALUES (?, ?, ?, ?, 'organic')",
                       (customer_id, to_ts(start), to_ts(start + timedelta(minutes=minutes)), device))
    return cur.lastrowid


def _pick_product(conn: sqlite3.Connection, catalog: Catalog, customer_id: int, now: datetime) -> int:
    """A product this customer plausibly wants: a favourite if they have one, else from their top category."""
    profile = build_profile(conn, customer_id, now, catalog)
    if profile.favourite_items:
        return profile.favourite_items[0]["product_id"]
    category = profile.favourite_categories[0] if profile.favourite_categories else "fashion"
    return random.choice([p.id for p in catalog.products.values() if p.category == category])


def fire_cart(conn: sqlite3.Connection, catalog: Catalog, customer_id: int, now: datetime,
              product_id: int | None = None, ago_hours: float = 3.0) -> dict:
    """The customer added a product to the cart `ago_hours` ago and left without ordering."""
    product = catalog.get(product_id or _pick_product(conn, catalog, customer_id, now))
    at = now - timedelta(hours=ago_hours)
    sid = _session(conn, customer_id, at)
    conn.execute("INSERT INTO browse_events (session_id, customer_id, ts, event_type, product_id) VALUES (?, ?, ?, 'view', ?)",
                 (sid, customer_id, to_ts(at + timedelta(minutes=1)), product.id))
    conn.execute("""INSERT INTO cart_events (session_id, customer_id, ts, event_type, product_id, qty, cart_value_paise)
                    VALUES (?, ?, ?, 'add', ?, 1, ?), (?, ?, ?, 'abandon', NULL, 1, ?)""",
                 (sid, customer_id, to_ts(at + timedelta(minutes=2)), product.id, product.price_paise,
                  sid, customer_id, to_ts(at + timedelta(minutes=5)), product.price_paise))
    conn.commit()
    return {"event": "cart", "product": product.name, "left_hours_ago": ago_hours}


def fire_search(conn: sqlite3.Connection, catalog: Catalog, customer_id: int, now: datetime, query: str,
                ago_hours: float = 1.0) -> dict:
    """The customer searched for something and looked at two matching products (one of them twice)."""
    q = query.lower().strip()
    matches = [p for p in catalog.products.values() if p.subcategory.lower() in q or q in p.subcategory.lower()
               or q in p.name.lower()]
    if not matches:
        raise ValueError(f"No product matches '{query}' (try e.g. lipstick, sneakers, earphones, kurta, serum)")
    random.shuffle(matches)
    at = now - timedelta(hours=ago_hours)
    sid = _session(conn, customer_id, at)
    rows = [(sid, customer_id, to_ts(at), "search", None, q)]
    for n, product in enumerate([matches[0], matches[0], *matches[1:2]], start=1):
        rows.append((sid, customer_id, to_ts(at + timedelta(minutes=n)), "view", product.id, None))
    conn.executemany("INSERT INTO browse_events (session_id, customer_id, ts, event_type, product_id, query) "
                     "VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    return {"event": "search", "query": q, "top_product": matches[0].name}


def fire_views(conn: sqlite3.Connection, catalog: Catalog, customer_id: int, now: datetime,
               product_id: int | None = None, times: int = 3) -> dict:
    """The customer looked at the same product several times over the last day."""
    product = catalog.get(product_id or _pick_product(conn, catalog, customer_id, now))
    for n in range(times):
        at = now - timedelta(hours=20 - n * 6)
        sid = _session(conn, customer_id, at, minutes=3)
        conn.execute("INSERT INTO browse_events (session_id, customer_id, ts, event_type, product_id) "
                     "VALUES (?, ?, ?, 'view', ?)", (sid, customer_id, to_ts(at + timedelta(minutes=1)), product.id))
    conn.commit()
    return {"event": "views", "product": product.name, "times": times}


# ---------------------------------------------------------------------------------------------------
# Reacting: detect the trigger and decide
# ---------------------------------------------------------------------------------------------------
def react(conn: sqlite3.Connection, catalog: Catalog, customer_id: int, now: datetime, use_agent: bool,
          preferred: str | None = None):
    """Find the strongest trigger (or the preferred one if present) and make a decision for it."""
    found = detect_triggers(build_profile(conn, customer_id, now, catalog))
    if not found:
        return None
    trigger = next((t for t in found if t.type == preferred), found[0])
    return decide(conn, customer_id, trigger.type, trigger.detail, now=now, use_agent=use_agent, catalog=catalog)


def linked_customer_ids(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT id FROM customers WHERE telegram_chat_id IS NOT NULL")]


def tick(conn: sqlite3.Connection, catalog: Catalog, n_customers: int, ai_for: str, now: datetime | None = None,
         seed: int | None = None) -> dict:
    """
    One round of background activity: follow-ups that are due, every linked tester, and a few random customers.
    Customers without any trigger are skipped (nothing happened to them), so no decision is wasted.
    """
    now = now or now_utc()
    followups = run_due_followups(conn, now=now, catalog=catalog)
    testers = linked_customer_ids(conn)
    pool = [r[0] for r in conn.execute("SELECT id FROM customers WHERE marketing_consent = 1")]
    others = random.Random(seed).sample([c for c in pool if c not in testers], min(n_customers, len(pool)))
    decisions = []
    for cid in testers + others:
        use_ai = ai_for == "all" or (ai_for == "testers" and cid in testers)
        outcome = react(conn, catalog, cid, now, use_agent=use_ai)
        if outcome:
            decisions.append(outcome)
    return {"followups": followups, "decisions": decisions}


# ---------------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------------
def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Simulate live shopping events and let OfferPilot react.")
    sub = parser.add_subparsers(dest="command", required=True)
    fire = sub.add_parser("fire", help="create one event for one customer, then decide")
    fire.add_argument("event", choices=["cart", "search", "view", "none"])
    fire.add_argument("--customer", type=int, required=True)
    fire.add_argument("--product", type=int)
    fire.add_argument("--query", default="lipstick")
    fire.add_argument("--ago-hours", type=float, default=3.0, help="for cart: how long ago it was left")
    fire.add_argument("--no-agent", action="store_true", help="use the free fallback rules")
    for name in ("tick", "loop"):
        p = sub.add_parser(name)
        p.add_argument("--customers", type=int, default=10, help="random background customers per tick")
        p.add_argument("--ai-for", choices=["testers", "all", "none"], default="testers")
        if name == "loop":
            p.add_argument("--speed", type=float, default=60, help="seconds between ticks")
    args = parser.parse_args()

    conn = connect()
    catalog = load_catalog(conn)
    if args.command == "fire":
        now = now_utc()
        if args.event == "cart":
            info = fire_cart(conn, catalog, args.customer, now, args.product, args.ago_hours)
            preferred = "cart_abandoned"
        elif args.event == "search":
            info = fire_search(conn, catalog, args.customer, now, args.query)
            preferred = "product_interest"
        elif args.event == "view":
            info = fire_views(conn, catalog, args.customer, now, args.product)
            preferred = "product_interest"
        else:
            info, preferred = {"event": "none"}, None
        print(f"Event created: {info}")
        outcome = react(conn, catalog, args.customer, now, use_agent=not args.no_agent, preferred=preferred)
        print(format_decision(outcome) if outcome else "No trigger for this customer right now: nothing to decide.")
        if outcome and outcome.status == "queued":
            print("\nThe bot will deliver it within ~15 seconds (keep `python -m bot.telegram_bot` running).")
        return

    def one_tick() -> None:
        result = tick(conn, catalog, args.customers, args.ai_for)
        print(f"\n[{now_utc():%H:%M:%S} UTC] follow-ups: {len(result['followups'])}, "
              f"decisions: {len(result['decisions'])}")
        for o in result["decisions"]:
            what = o.offer.describe() if o.offer else o.reasoning.get("summary", "")[:80]
            print(f"  #{o.customer_id} {o.first_name:<10} {o.trigger_type:<17} {o.decided_by:<10} {o.status:<12} {what}")

    if args.command == "tick":
        one_tick()
    else:
        while True:
            one_tick()
            time.sleep(args.speed)


if __name__ == "__main__":
    main()
