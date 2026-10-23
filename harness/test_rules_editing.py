"""
Harness test 7: editing rules is safe and traceable.

Proves: version 1 comes from config/rules.yaml; a valid edit creates a new active version with the change listed;
unsafe edits are refused with a clear reason; offers decided but not yet sent are re-checked against the new
version (and cancelled if they now break a rule); decisions record the version they were checked against.
Run: pytest harness/test_rules_editing.py -v
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from engine.catalog import load_catalog
from engine.decide import decide, recheck_scheduled
from engine.models import OfferProposal
from engine.rules_store import (RulesValidationError, ensure_rules, get_active_rules, list_versions, save_rules,
                                with_change)
from harness.conftest import NOON_IST


def test_version_one_comes_from_yaml(db):
    ensure_rules(db)
    rules = get_active_rules(db)
    assert rules.version == 1
    assert rules.profit["category_cap_pct"]["electronics"] == 8


def test_valid_edit_creates_new_active_version(db):
    catalog = load_catalog(db)
    old = get_active_rules(db)
    version, changes = save_rules(db, with_change(old.data, "profit.category_cap_pct.electronics", 12),
                                  "marketing@test", "electronics margins went up", catalog)
    assert version == 2
    assert changes == [{"path": "profit.category_cap_pct.electronics", "old": 8, "new": 12}]
    active = get_active_rules(db)
    assert active.version == 2 and active.profit["category_cap_pct"]["electronics"] == 12
    history = list_versions(db)
    assert history[0]["version"] == 2 and history[0]["changes"][0]["new"] == 12
    assert history[0]["created_by"] == "marketing@test"


def test_saving_without_changes_keeps_version(db):
    old = get_active_rules(db)
    version, changes = save_rules(db, old.data, "x", None, load_catalog(db))
    assert version == old.version and changes == []


@pytest.mark.parametrize("path, value, expected_words", [
    ("profit.category_cap_pct.electronics", 20, "too high"),        # electronics margin ~22.5% - floor 10%
    ("profit.margin_floor_pct", -5, "can't be below"),
    ("anti_spam.max_offers_per_7_days", 9, "can't be above"),
    ("budget.daily_rupees", -100, "can't be below"),
    ("anti_spam.respect_marketing_consent", False, "consent"),
    ("message.must_match_offer", False, "match"),
    ("anti_spam.quiet_hours_start", "10pm", "HH:MM"),
])
def test_unsafe_edits_are_refused(db, path, value, expected_words):
    old = get_active_rules(db)
    with pytest.raises(RulesValidationError) as err:
        save_rules(db, with_change(old.data, path, value), "x", None, load_catalog(db))
    assert any(expected_words.lower() in p.lower() for p in err.value.problems)
    assert get_active_rules(db).version == old.version  # nothing saved


def test_decisions_record_rules_version(db):
    customer_id = db.execute("SELECT id FROM customers WHERE marketing_consent = 1 LIMIT 1").fetchone()[0]
    outcome = decide(db, customer_id, now=NOON_IST, use_agent=False)
    row = db.execute("SELECT rules_version FROM decisions WHERE id = ?", (outcome.decision_id,)).fetchone()
    assert row["rules_version"] == get_active_rules(db).version


def test_scheduled_offer_cancelled_when_new_rules_forbid_it(db):
    """A 25% fashion offer is fine under v1 (cap 30%) but must not go out after the cap drops to 20%."""
    customer_id = db.execute("SELECT id FROM customers WHERE marketing_consent = 1 LIMIT 1").fetchone()[0]
    send_at = NOON_IST + timedelta(hours=3)
    offer = OfferProposal(offer_type="pct_off", amount=25, category="fashion", min_order_rupees=999,
                          expiry_hours=24, send_at=send_at, message="Hi! Get 25% off on fashion above ₹999.")
    db.execute("""INSERT INTO decisions (customer_id, created_at, trigger_type, decided_by, status, final_offer,
                  message, send_at, reasoning, rule_checks, rules_version, offer_cost_paise)
                  VALUES (?, ?, 'manual', 'agent', 'scheduled', ?, ?, ?, '{}', '[]', 1, 0)""",
               (customer_id, "2026-10-23T06:00:00Z", offer.model_dump_json(), offer.message,
                send_at.strftime("%Y-%m-%dT%H:%M:%SZ")))
    db.commit()

    old = get_active_rules(db)
    save_rules(db, with_change(old.data, "profit.category_cap_pct.fashion", 20), "x", None, load_catalog(db))
    report = recheck_scheduled(db, now=NOON_IST)

    assert report and report[0]["result"] == "cancelled"
    row = db.execute("SELECT status, rules_version, reasoning FROM decisions WHERE status = 'cancelled'").fetchone()
    assert row["rules_version"] == 2
    assert "Cancelled before sending" in json.loads(row["reasoning"])["cancelled_note"]
