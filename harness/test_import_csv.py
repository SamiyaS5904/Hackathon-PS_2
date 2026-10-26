"""
Tests for the real-data CSV importer (data/import_csv.py) and learning mode (engine/learning.py).

Proves: the documented sample imports; a minimal export with only the required columns imports and the engine
still decides (no browsing / cart / wallet / points tables); learning mode is set without campaigns and a seeded
holdout is recorded and respected by the gatekeeper; campaigns switch learning mode off; learning mode turns the
uplift model off and scales the formula down; bad input stops with a message a person can act on.
Run: pytest harness/test_import_csv.py -v
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from config.settings import PROJECT_ROOT
from data.import_csv import ImportError_, import_shop
from db.store import connect
from engine.catalog import load_catalog
from engine.decide import decide
from engine.features import build_profile
from engine.learning import forget_mode, in_holdout, shop_mode
from engine.models import OfferProposal
from engine.profit import estimate_uplift, formula_uplift

SAMPLE = PROJECT_ROOT / "docs" / "csv_import"
NOW = datetime(2026, 10, 21, 6, 30, tzinfo=timezone.utc)


def mapping() -> dict:
    return yaml.safe_load((SAMPLE / "mapping.example.yaml").read_text(encoding="utf-8"))


def write(path: Path, rows: list[dict]) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def import_sample(tmp_path: Path, campaigns: Path | None = None) -> Path:
    db = tmp_path / "brand.db"
    import_shop(mapping(), SAMPLE / "customers.csv", SAMPLE / "products.csv", SAMPLE / "orders.csv", campaigns, db)
    return db


def test_sample_export_imports_in_learning_mode_with_a_holdout(tmp_path):
    db = import_sample(tmp_path)
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 50
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] > 300
    mode = shop_mode(conn)
    assert mode["learning_mode"] and mode["conservative_uplift_factor"] == 0.7
    eligible = conn.execute("SELECT COUNT(*) FROM customers WHERE marketing_consent = 1").fetchone()[0]
    held = [r[0] for r in conn.execute("SELECT customer_id FROM holdout_assignments WHERE grp = 'holdout'")]
    assert len(held) == round(eligible * 0.10) and conn.execute(
        "SELECT COUNT(*) FROM holdout_assignments").fetchone()[0] == eligible
    assert in_holdout(conn, held[0])


def test_the_engine_decides_on_imported_data_and_respects_the_holdout(tmp_path):
    conn = connect(import_sample(tmp_path))
    catalog = load_catalog(conn)
    held = conn.execute("SELECT customer_id FROM holdout_assignments WHERE grp = 'holdout'").fetchone()[0]
    out = decide(conn, held, now=NOW, use_agent=False, catalog=catalog)
    assert out.decided_by == "gatekeeper" and "holdout" in out.reasoning["summary"].lower()
    outcomes = [decide(conn, cid, now=NOW, use_agent=False, dry_run=True, catalog=catalog)
                for cid in range(1, 51)]
    assert any(o.offer is not None for o in outcomes)          # some offers pass the rules on real-looking data
    assert all(o.offer is None or o.rule_checks for o in outcomes)


def test_minimal_export_degrades_gracefully(tmp_path):
    """Only the required columns, no browsing/cart/wallet/points: profile and decision still work."""
    customers = write(tmp_path / "c.csv", [{"customer_id": "A1", "first_name": "Asha"},
                                           {"customer_id": "A2", "first_name": "Bilal"}])
    products = write(tmp_path / "p.csv", [{"sku": "P1", "title": "Tea Tin", "category": "grocery", "price": "500",
                                           "cost": "300"}])
    orders = write(tmp_path / "o.csv", [
        {"order_id": f"O{i}", "customer_id": "A1", "created_at": f"2026-0{m}-10 12:00:00", "sku": "P1",
         "quantity": "1", "unit_price": "500"} for i, m in enumerate([3, 4, 5, 6, 7], start=1)])
    m = mapping()
    m["customers"] = {"id": "customer_id", "first_name": "first_name"}
    m["orders"] = {"id": "order_id", "customer_id": "customer_id", "ordered_at": "created_at", "product_id": "sku",
                   "quantity": "quantity", "unit_price": "unit_price"}
    m["defaults"]["marketing_consent"] = True
    db = tmp_path / "minimal.db"
    summary = import_shop(m, customers, products, orders, None, db)
    assert summary["orders"] == 5 and summary["learning_mode"]
    conn = connect(db)
    profile = build_profile(conn, 1, NOW, load_catalog(conn))
    assert profile.total_orders == 5 and profile.open_cart is None and profile.wallet_balance_rupees == 0
    assert profile.points_balance == 0 and profile.intent_products == [] and profile.overdue_ratio > 2
    out = decide(conn, 1, now=NOW, use_agent=False, dry_run=True)
    assert out.decided_by in ("fallback", "gatekeeper")


def test_learning_mode_switches_off_the_model_and_is_conservative(tmp_path):
    conn = connect(import_sample(tmp_path))
    catalog = load_catalog(conn)
    profile = build_profile(conn, 1, NOW, catalog)
    assert profile.learning_mode and profile.uplift_factor == 0.7
    o = OfferProposal(offer_type="free_delivery", min_order_rupees=499)
    assert estimate_uplift(o, profile, catalog) == round(formula_uplift(o, profile, catalog) * 0.7, 3)


def test_campaigns_turn_learning_mode_off(tmp_path):
    campaigns = write(tmp_path / "k.csv", [
        {"customer_id": "C-1001", "offer_type": "pct_off", "value": "10", "category": "beauty", "group": "treatment",
         "sent_at": "2026-05-01 10:00:00", "expires_at": "2026-05-03 10:00:00", "redeemed_at": "", "purchased": "0"},
        {"customer_id": "C-1002", "offer_type": "pct_off", "value": "10", "category": "beauty", "group": "holdout",
         "sent_at": "", "expires_at": "2026-05-03 10:00:00", "redeemed_at": "", "purchased": ""}])
    conn = connect(import_sample(tmp_path, campaigns))
    forget_mode(conn)
    assert not shop_mode(conn)["learning_mode"]
    assert conn.execute("SELECT COUNT(*) FROM offers_history").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM holdout_assignments").fetchone()[0] == 0


def test_bad_input_stops_with_a_clear_message(tmp_path):
    no_cost = write(tmp_path / "p.csv", [{"sku": "P1", "title": "Tea", "category": "grocery", "price": "500"}])
    with pytest.raises(ImportError_, match="cost"):
        import_shop(mapping(), SAMPLE / "customers.csv", no_cost, SAMPLE / "orders.csv", None, tmp_path / "x.db")
    odd = write(tmp_path / "p2.csv", [{"sku": "P1", "title": "Toy", "category": "Toys", "price": "500", "cost": "200"}])
    with pytest.raises(ImportError_, match="category_map"):
        import_shop(mapping(), SAMPLE / "customers.csv", odd, SAMPLE / "orders.csv", None, tmp_path / "y.db")
    import_sample(tmp_path)
    with pytest.raises(ImportError_, match="already exists"):
        import_sample(tmp_path)
