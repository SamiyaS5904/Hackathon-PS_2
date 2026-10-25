"""
Shared test fixtures for the harness.

Most tests use small hand-made objects (a tiny catalog, a profile with chosen values, the default rules), so a
test shows exactly which situation it checks and runs in milliseconds without the 170 MB database.
Tests that need a real database get `small_db`: a fresh 40-customer dataset generated into a temp folder.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from data.generate import generate
from db.store import connect
from engine.catalog import Catalog, Product
from engine.features import CustomerProfile
from engine.models import OfferProposal
from engine.rules import RuleState
from engine.rules_store import ActiveRules, load_default_rules

# 12:00 IST on 23 Oct 2026: outside quiet hours, so tests that don't care about time aren't affected by it
NOON_IST = datetime(2026, 10, 23, 6, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_real_api_key(monkeypatch):
    """
    Tests must NEVER call the real Claude API (cost, speed, repeatability). This hides the key from every module
    that reads it, so code behaves as if there's no key. Tests that need the agent pass a fake client instead.
    """
    import dataclasses

    import engine.agent
    import engine.gatekeeper

    no_key = dataclasses.replace(engine.gatekeeper.settings, anthropic_api_key="")
    monkeypatch.setattr(engine.gatekeeper, "settings", no_key)
    monkeypatch.setattr(engine.agent, "settings", no_key)


@pytest.fixture(autouse=True)
def heuristics_not_trained_model(monkeypatch):
    """
    Tests use the fixed hand-made formulas, not the trained uplift model, so results never change when the model
    is retrained. The model has its own tests (harness/test_uplift.py) and evaluation (harness/eval_uplift.py).
    """
    import engine.uplift

    monkeypatch.setenv("USE_UPLIFT_MODEL", "0")
    engine.uplift.get_model.cache_clear()
    yield
    engine.uplift.get_model.cache_clear()


@pytest.fixture
def now() -> datetime:
    return NOON_IST


@pytest.fixture
def catalog() -> Catalog:
    """A tiny catalog. Category averages match the real generated catalog closely."""
    products = {
        1: Product(1, "Voltix Earphones Pro", "electronics", "Earphones", "Voltix", 2000_00, 1560_00, 22.0),
        2: Product(2, "Urban Thread Kurta Classic", "fashion", "Kurta", "Urban Thread", 1500_00, 750_00, 50.0),
        3: Product(3, "FreshFarm Basmati Rice Plus", "grocery", "Basmati Rice", "FreshFarm", 500_00, 410_00, 18.0),
        4: Product(4, "Glowveda Serum Neo", "beauty", "Serum", "Glowveda", 900_00, 480_00, 46.7),
        5: Product(5, "Casa Nova Table Lamp Max", "home", "Table Lamp", "Casa Nova", 1800_00, 1150_00, 36.1),
    }
    return Catalog(
        products=products,
        category_avg_margin_pct={"electronics": 22.5, "fashion": 50.3, "beauty": 46.2, "home": 36.2, "grocery": 22.7},
        category_avg_price_paise={"electronics": 1827_00, "fashion": 1775_00, "beauty": 639_00, "home": 1404_00,
                                  "grocery": 341_00},
    )


@pytest.fixture
def rules() -> ActiveRules:
    return ActiveRules(version=1, data=load_default_rules(), created_at="2026-10-22T00:00:00Z",
                       created_by="test", note=None)


@pytest.fixture
def state() -> RuleState:
    return RuleState()


def make_profile(**overrides) -> CustomerProfile:
    """A normal, persuadable customer: orders every ~20 days, 20 days since last, low 'buys anyway' chance."""
    values = dict(
        customer_id=1, first_name="Riya", city="Pune", city_tier=1, age=28, language="English",
        marketing_consent=True, telegram_linked=False, days_since_signup=800,
        total_orders=10, total_spend_rupees=15000, avg_order_value_rupees=1500, days_since_last_order=20.0,
        usual_gap_days=20.0, overdue_ratio=1.0, habit_strength=0.5, habit_status="due now", orders_last_30d=1,
        orders_last_90d=4, return_rate=0.0, high_value=False, sale_order_share=0.3, deal_seeker_score=0.2,
        favourite_items=[], favourite_categories=["fashion"], category_spend_share={"fashion": 1.0},
        p_buy_48h_no_offer=0.05,
    )
    values.update(overrides)
    return CustomerProfile(**values)


@pytest.fixture
def profile() -> CustomerProfile:
    return make_profile()


def offer(**fields) -> OfferProposal:
    """Shortcut: offer(offer_type='pct_off', amount=15, category='fashion', message='...')."""
    return OfferProposal(**fields)


@pytest.fixture(scope="session")
def small_db(tmp_path_factory) -> Path:
    """A real but small database: 40 customers, 12 months, ending 22 Oct 2026."""
    path = tmp_path_factory.mktemp("db") / "small.db"
    generate(n_customers=40, months=12, end_date=date(2026, 10, 22), seed=11, db_path=path)
    return path


@pytest.fixture
def db(small_db: Path, tmp_path: Path):
    """A private copy of the small database for one test, so tests can't affect each other."""
    copy = tmp_path / "test.db"
    copy.write_bytes(small_db.read_bytes())
    conn = connect(copy)
    yield conn
    conn.close()


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """After a full harness run, save the counts so the dashboard's Guardrails page can show the proof."""
    stats = terminalreporter.stats
    passed, failed = len(stats.get("passed", [])), len(stats.get("failed", [])) + len(stats.get("error", []))
    if passed + failed < 100:  # a single file or test was run: not the full proof, keep the last full result
        return
    from engine.guardrails import save_harness_result
    save_harness_result("pytest", {"passed": passed, "failed": failed, "ok": failed == 0})
