"""
Customer groups: every customer gets one clear group, a favourite category and a usual shopping hour.

Where it sits: data engineering on top of the analyst (engine/features.py). Built once (and again whenever the data
changes) into the `customer_groups` table; engine/autopilot.py reads it to plan the day's offers per category bot,
and the dashboard shows each customer's group. Only visible signals are used (never customer_traits).

Run:  python -m engine.groups            (about 2-3 minutes for 3,000 customers, no API)

Groups come from engine/segments.py (About-to-leave VIP, Points collector, Loyal regular, Deal hunter, Cart
abandoner, New customer) plus "Everyday shopper" for everyone else. The current trigger (habit break, cart, interest,
points, wallet) and the usual shopping hour let the autopilot send each offer at a moment that fits the person.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime

from db.store import connect
from engine.catalog import Catalog, load_catalog
from engine.clock import now_utc, to_ts
from engine.features import build_profile
from engine.segments import SEGMENTS
from engine.triggers import detect_triggers

EVERYDAY = ("everyday_shopper", "Everyday shopper")

SCHEMA = """
CREATE TABLE IF NOT EXISTS customer_groups (
    customer_id        INTEGER PRIMARY KEY REFERENCES customers(id),
    group_key          TEXT NOT NULL,
    group_label        TEXT NOT NULL,
    favourite_category TEXT,
    usual_hour         INTEGER,             -- IST hour they usually shop (from their sessions)
    trigger_type       TEXT,                -- strongest trigger when the groups were built
    trigger_strength   REAL,
    marketing_consent  INTEGER NOT NULL,
    built_at           TEXT NOT NULL
);
"""


def group_of(profile) -> tuple[str, str]:
    """The first segment the customer matches (same order as the Testers page), else Everyday shopper."""
    for seg in SEGMENTS:
        if seg.match(profile):
            return seg.key, seg.label
    return EVERYDAY


def build_groups(conn: sqlite3.Connection, catalog: Catalog | None = None, now: datetime | None = None,
                 progress: bool = False) -> int:
    """Compute and save the group of every customer. Returns how many customers were grouped."""
    catalog = catalog or load_catalog(conn)
    now = now or now_utc()
    conn.executescript(SCHEMA)
    ids = [r[0] for r in conn.execute("SELECT id FROM customers ORDER BY id")]
    rows = []
    for n, cid in enumerate(ids, start=1):
        p = build_profile(conn, cid, now, catalog)
        key, label = group_of(p)
        found = detect_triggers(p)
        rows.append((cid, key, label, p.favourite_categories[0] if p.favourite_categories else None,
                     p.usual_shopping_hour_ist, found[0].type if found else None,
                     found[0].strength if found else None, int(p.marketing_consent), to_ts(now)))
        if progress and n % 500 == 0:
            print(f"  {n}/{len(ids)} customers grouped...")
    conn.execute("DELETE FROM customer_groups")
    conn.executemany("INSERT INTO customer_groups VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    return len(rows)


def group_counts(conn: sqlite3.Connection) -> list[dict]:
    """How many customers per group (for the dashboard)."""
    conn.executescript(SCHEMA)
    return [{"group": r[0], "label": r[1], "customers": r[2]} for r in conn.execute(
        "SELECT group_key, group_label, COUNT(*) FROM customer_groups GROUP BY 1, 2 ORDER BY 3 DESC")]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    conn = connect()
    n = build_groups(conn, progress=True)
    print(f"Grouped {n} customers:")
    for g in group_counts(conn):
        print(f"  {g['label']:<22} {g['customers']}")


if __name__ == "__main__":
    main()
