"""
Import a real shop's CSV export into OfferPilot's database (instead of our synthetic data).

Where it sits: an alternative STEP 1. data/generate.py makes synthetic data; this loads a brand's own data into the
same schema, so the rest of the pipeline (features, rules, agent, dashboard) runs unchanged.

Run:
    python -m data.import_csv --mapping docs/csv_import/mapping.example.yaml \\
        --customers docs/csv_import/customers.csv --products docs/csv_import/products.csv \\
        --orders docs/csv_import/orders.csv [--campaigns campaigns.csv] --db data/brand.db
Then point the app at it: DB_PATH=data/brand.db (in .env) and start the API as usual.

What it needs (column names are set in the mapping file, see docs/csv_import/mapping.example.yaml):
  - customers: id, first name (others optional: last name, city, signup date, marketing consent, language)
  - products:  id, name, category, price AND cost (cost is required: without it no margin, no profit maths)
  - orders:    one row per order LINE: order id, customer id, time, product id, quantity, unit price
               (optional: order discount, delivery fee, status)
  - campaigns: optional past offers with a treatment / holdout group (makes the uplift model trainable)
Missing tables (browsing, carts, wallet, points) stay empty: those features become neutral and the triggers that
need them simply never fire. The engine still decides on orders, habits and margins.

Learning mode: without campaigns, the database is marked as learning mode (engine/learning.py): no uplift model,
a conservative formula, and a seeded random holdout of eligible customers who get no offers, so the shop can learn
what offers really add.
It never writes into an existing database: pick a new --db file (or pass --force to replace it).
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from data.calendar_gen import build_calendar
from db.store import connect, init_schema, insert_many
from engine.clock import to_ts

RULE_CATEGORIES = {"electronics", "fashion", "beauty", "home", "grocery"}
OFFER_TYPES = {"pct_off", "flat_off", "free_delivery", "cashback_to_wallet", "points_multiplier", "early_access"}
YES = {"1", "true", "yes", "y", "subscribed", "opted_in"}


class ImportError_(ValueError):
    """A problem in the input files, explained so a person can fix the CSV or the mapping."""


# ---------------------------------------------------------------------------------------------------
# Reading and converting
# ---------------------------------------------------------------------------------------------------
def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def require_columns(rows: list[dict], columns: dict, required: list[str], file_label: str) -> None:
    """Every required logical field must be mapped and present in the file header."""
    header = set(rows[0]) if rows else set()
    missing = [f"{field} (column '{columns.get(field)}')" for field in required
               if not columns.get(field) or columns[field] not in header]
    if missing:
        raise ImportError_(f"{file_label}: missing required columns: {', '.join(missing)}. "
                           f"Columns found: {', '.join(sorted(header)) or 'none (empty file?)'}.")


class Converter:
    """Turns the brand's text values into our formats: UTC timestamps and integer paise."""

    def __init__(self, mapping: dict):
        sign = -1 if mapping.get("utc_offset", "+05:30").startswith("-") else 1
        hours, minutes = mapping.get("utc_offset", "+05:30").lstrip("+-").split(":")
        self.tz = timezone(sign * timedelta(hours=int(hours), minutes=int(minutes)))
        self.formats = mapping.get("date_formats", ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"])
        self.paise_per_unit = 1 if mapping.get("currency_unit", "rupees") == "paise" else 100

    def ts(self, value: str) -> str:
        value = value.strip()
        for fmt in self.formats:
            try:
                return to_ts(datetime.strptime(value, fmt).replace(tzinfo=self.tz))
            except ValueError:
                continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return to_ts(parsed if parsed.tzinfo else parsed.replace(tzinfo=self.tz))
        except ValueError:
            raise ImportError_(f"Can't read the date '{value}'. Add its format to date_formats in the mapping.")

    def money(self, value: str | None) -> int:
        if value is None or str(value).strip() == "":
            return 0
        cleaned = str(value).replace(",", "").replace("₹", "").replace("Rs", "").strip()
        return round(float(cleaned) * self.paise_per_unit)


class IdMap:
    """The brand's ids (any text) -> our integer ids, remembered in the id_map table."""

    def __init__(self):
        self.ids: dict[str, dict[str, int]] = defaultdict(dict)

    def get(self, entity: str, external: str, create: bool = True) -> int | None:
        external = str(external).strip()
        table = self.ids[entity]
        if external not in table:
            if not create:
                return None
            table[external] = len(table) + 1
        return table[external]

    def rows(self) -> list[tuple]:
        return [(entity, ext, internal) for entity, table in self.ids.items() for ext, internal in table.items()]


# ---------------------------------------------------------------------------------------------------
# The import
# ---------------------------------------------------------------------------------------------------
def import_shop(mapping: dict, customers_csv: Path, products_csv: Path, orders_csv: Path,
                campaigns_csv: Path | None, db_path: Path, force: bool = False) -> dict:
    """Load the files into a new database. Returns a summary (counts, learning mode, holdout size)."""
    if db_path.exists():
        if not force:
            raise ImportError_(f"{db_path} already exists. Choose a new --db file, or pass --force to replace it.")
        db_path.unlink()
    conv, ids = Converter(mapping), IdMap()
    defaults = mapping.get("defaults", {})
    category_map = {str(k).strip().lower(): v for k, v in (mapping.get("category_map") or {}).items()}

    # Products: price AND cost are required (margin is the heart of profit protection)
    pmap = mapping["products"]
    product_rows_in = read_csv(products_csv)
    require_columns(product_rows_in, pmap, ["id", "name", "category", "price", "cost"], "products")
    products, unmapped = [], set()
    for r in product_rows_in:
        raw_category = r[pmap["category"]].strip()
        category = category_map.get(raw_category.lower(), raw_category.lower())
        if category not in RULE_CATEGORIES:
            unmapped.add(raw_category)
            continue
        price, cost = conv.money(r[pmap["price"]]), conv.money(r[pmap["cost"]])
        if price <= 0 or cost < 0 or cost > price:
            raise ImportError_(f"Product '{r[pmap['name']]}': price {price / 100:g} and cost {cost / 100:g} don't "
                               f"make sense (need 0 <= cost <= price, price > 0).")
        pid = ids.get("product", r[pmap["id"]])
        products.append((pid, r[pmap["name"]].strip(), category,
                         (r.get(pmap.get("subcategory", ""), "") or raw_category).strip(),
                         (r.get(pmap.get("brand", ""), "") or mapping.get("brand", "Own brand")).strip(),
                         price, cost, round((price - cost) / price * 100, 1)))
    if unmapped:
        raise ImportError_(f"These product categories have no rule category: {', '.join(sorted(unmapped))}. "
                           f"Add them to category_map in the mapping file (choose from "
                           f"{', '.join(sorted(RULE_CATEGORIES))}).")

    # Customers
    cmap = mapping["customers"]
    customer_rows_in = read_csv(customers_csv)
    require_columns(customer_rows_in, cmap, ["id", "first_name"], "customers")
    customers = []
    for r in customer_rows_in:
        get = lambda field, fallback="": (r.get(cmap.get(field, ""), "") or fallback).strip()  # noqa: E731
        consent_raw = get("marketing_consent")
        consent = consent_raw.lower() in YES if consent_raw else bool(defaults.get("marketing_consent", False))
        signup = get("signup_date")
        customers.append((ids.get("customer", r[cmap["id"]]), get("first_name"), get("last_name"), "U", 0,
                          get("city", defaults.get("city", "Unknown")), 2,
                          conv.ts(signup)[:10] if signup else None, "web",
                          get("language", defaults.get("language", "English")), int(consent), None))

    # Orders: one row per order line; order-level discount / fee taken once per order
    omap = mapping["orders"]
    order_rows_in = read_csv(orders_csv)
    require_columns(order_rows_in, omap, ["id", "customer_id", "ordered_at", "product_id", "quantity", "unit_price"],
                    "orders")
    orders: dict[int, dict] = {}
    items = []
    for r in order_rows_in:
        cid = ids.get("customer", r[omap["customer_id"]], create=False)
        pid = ids.get("product", r[omap["product_id"]], create=False)
        if cid is None or pid is None:
            continue  # a line for an unknown customer or product (e.g. a deleted product): skipped, counted below
        oid = ids.get("order", r[omap["id"]])
        qty = int(float(r[omap["quantity"]] or 1))
        unit = conv.money(r[omap["unit_price"]])
        o = orders.setdefault(oid, {"customer_id": cid, "ts": conv.ts(r[omap["ordered_at"]]), "gross": 0,
                                    "discount": conv.money(r.get(omap.get("discount", ""))),
                                    "fee": conv.money(r.get(omap.get("delivery_fee", ""))),
                                    "status": (r.get(omap.get("status", ""), "") or "delivered").strip().lower()})
        o["gross"] += unit * qty
        items.append((len(items) + 1, oid, pid, qty, unit))
    skipped_lines = len(order_rows_in) - len(items)
    order_rows = [(oid, o["customer_id"], None, o["ts"], o["gross"], min(o["discount"], o["gross"]), o["fee"],
                   o["gross"] - min(o["discount"], o["gross"]) + o["fee"], None, "card",
                   o["status"] if o["status"] in ("delivered", "cancelled", "returned") else "delivered", 0, 0)
                  for oid, o in orders.items()]

    # Signup date: the brand may not export it -> first order date (or today)
    first_order = {}
    for o in orders.values():
        first_order[o["customer_id"]] = min(first_order.get(o["customer_id"], o["ts"]), o["ts"])
    customers = [(*c[:7], c[7] or first_order.get(c[0], to_ts(datetime.now(timezone.utc)))[:10], *c[8:])
                 for c in customers]

    conn = connect(db_path)
    init_schema(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    insert_many(conn, "products", ("id", "name", "category", "subcategory", "brand", "price_paise", "cost_paise",
                                   "margin_pct"), products)
    insert_many(conn, "customers", ("id", "first_name", "last_name", "gender", "age", "city", "city_tier",
                                    "signup_date", "device", "language", "marketing_consent", "telegram_chat_id"),
                customers)
    insert_many(conn, "orders", ("id", "customer_id", "session_id", "ts", "gross_paise", "discount_paise",
                                 "delivery_fee_paise", "net_paise", "offer_id", "payment_method", "status",
                                 "points_earned", "points_redeemed"), order_rows)
    insert_many(conn, "order_items", ("id", "order_id", "product_id", "qty", "unit_price_paise"), items)

    # Calendar (sale days) over the data window. Note: uses OfferPilot's Indian sale calendar; replace the calendar
    # table if the brand has its own sale days.
    times = sorted(o["ts"] for o in orders.values())
    start = date.fromisoformat(times[0][:10]) if times else date.today()
    insert_many(conn, "calendar", ("date", "is_weekend", "is_payday", "is_festival", "festival_name"),
                build_calendar(start, date.today() + timedelta(days=120)))

    # Past campaigns (optional) -> learning mode off; otherwise learning mode with a holdout
    n_campaign_rows = 0
    if campaigns_csv:
        n_campaign_rows = _import_campaigns(conn, mapping, read_csv(campaigns_csv), conv, ids, orders)
    learning = n_campaign_rows == 0
    lm = mapping.get("learning_mode", {})
    holdout_ids = []
    if learning:
        eligible = sorted(c[0] for c in customers if c[10])
        rng = random.Random(lm.get("seed", 7))
        holdout_ids = sorted(rng.sample(eligible, round(len(eligible) * lm.get("holdout_share", 0.10))))
        now = to_ts(datetime.now(timezone.utc))
        held = set(holdout_ids)
        insert_many(conn, "holdout_assignments", ("customer_id", "grp", "assigned_at"),
                    [(cid, "holdout" if cid in held else "eligible", now) for cid in eligible])
    insert_many(conn, "meta", ("key", "value"), [
        ("source", f"csv:{mapping.get('brand', 'brand')}"),
        ("learning_mode", "1" if learning else "0"),
        ("conservative_uplift_factor", str(lm.get("conservative_uplift_factor", 0.7) if learning else 1.0)),
        ("imported_at", to_ts(datetime.now(timezone.utc))),
    ])
    insert_many(conn, "id_map", ("entity", "external_id", "internal_id"), ids.rows())
    conn.commit()
    problems = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.close()
    if problems:
        raise ImportError_(f"{len(problems)} broken links between tables after import.")
    return {"customers": len(customers), "products": len(products), "orders": len(order_rows),
            "order_lines": len(items), "skipped_order_lines": skipped_lines, "campaign_offers": n_campaign_rows,
            "learning_mode": learning, "holdout_customers": len(holdout_ids), "db": str(db_path)}


def _import_campaigns(conn, mapping: dict, rows: list[dict], conv: Converter, ids: IdMap, orders: dict) -> int:
    """Past offers with treatment/holdout groups -> campaigns + offers_history (what the uplift model learns from)."""
    kmap = mapping["campaigns"]
    require_columns(rows, kmap, ["customer_id", "offer_type", "group", "expires_at"], "campaigns")
    campaigns: dict[tuple, int] = {}
    history = []
    by_customer = defaultdict(list)
    for o in orders.values():
        by_customer[o["customer_id"]].append(o["ts"])
    for r in rows:
        cid = ids.get("customer", r[kmap["customer_id"]], create=False)
        offer_type = r[kmap["offer_type"]].strip()
        group = r[kmap["group"]].strip().lower()
        if cid is None or offer_type not in OFFER_TYPES or group not in ("treatment", "holdout"):
            continue
        value = float(r.get(kmap.get("value", ""), "") or 0)
        expires = conv.ts(r[kmap["expires_at"]])
        sent_raw = r.get(kmap.get("sent_at", ""), "")
        sent = conv.ts(sent_raw) if sent_raw and group == "treatment" else None
        start = sent or to_ts(datetime.fromisoformat(expires.replace("Z", "+00:00")) - timedelta(hours=48))
        key = (offer_type, value, start[:10])
        if key not in campaigns:
            campaigns[key] = len(campaigns) + 1
        bought_raw = r.get(kmap.get("purchased_in_window", ""), "")
        bought = (bought_raw.strip().lower() in YES) if bought_raw else any(
            start <= t <= expires for t in by_customer.get(cid, []))
        redeemed = r.get(kmap.get("redeemed_at", ""), "")
        history.append((len(history) + 1, campaigns[key], cid, offer_type, value,
                        (r.get(kmap.get("category", ""), "") or None), 0, group, sent, expires, None, None,
                        conv.ts(redeemed) if redeemed else None, None, int(bought)))
    insert_many(conn, "campaigns", ("id", "name", "start_date", "offer_type", "value", "target"),
                [(cid, f"Imported {t} {v:g} ({d})", d, t, v, "imported") for (t, v, d), cid in campaigns.items()])
    insert_many(conn, "offers_history", ("id", "campaign_id", "customer_id", "offer_type", "value", "category",
                                         "min_order_paise", "group_name", "sent_at", "expires_at", "opened_at",
                                         "clicked_at", "redeemed_at", "order_id", "purchased_in_window"), history)
    return len(history)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Import a shop's CSV export into a new OfferPilot database.")
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--customers", type=Path, required=True)
    parser.add_argument("--products", type=Path, required=True)
    parser.add_argument("--orders", type=Path, required=True)
    parser.add_argument("--campaigns", type=Path)
    parser.add_argument("--db", type=Path, required=True, help="new database file, e.g. data/brand.db")
    parser.add_argument("--force", action="store_true", help="replace --db if it exists")
    args = parser.parse_args()
    mapping = yaml.safe_load(args.mapping.read_text(encoding="utf-8"))
    try:
        summary = import_shop(mapping, args.customers, args.products, args.orders, args.campaigns, args.db, args.force)
    except ImportError_ as exc:
        sys.exit(f"Import stopped: {exc}")
    print("Imported:", ", ".join(f"{k} {v}" for k, v in summary.items()))
    if summary["learning_mode"]:
        print(f"Learning mode ON (no past campaigns): no uplift model, conservative estimates, "
              f"{summary['holdout_customers']} customers kept in a holdout.")
    print(f"Next: set DB_PATH={args.db} in .env and start the API (python -m uvicorn api.main:app --port 8010).")


if __name__ == "__main__":
    main()
