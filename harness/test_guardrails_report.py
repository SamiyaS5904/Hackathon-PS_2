"""
Tests for the Guardrails page data (engine/guardrails.py + /api/guardrails), on the small test database.

Proves the page tells the truth: AI proposals the rules rejected are counted per rule, repairs are counted,
menu options blocked before the AI saw them are counted, budgets add up, the circuit-breaker gauge counts blocked
decisions, and gatekeeper / fallback reasons are grouped into categories a manager can read.
Run: pytest harness/test_guardrails_report.py -v
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

import api.main
from db.store import connect
from engine.clock import to_ts
from engine.guardrails import RULE_LABELS, build_report
from engine.rules_store import get_active_rules
from harness.conftest import NOON_IST


def _insert(conn, customer_id: int, **fields) -> None:
    row = {"customer_id": customer_id, "created_at": to_ts(NOON_IST), "trigger_type": "manual",
           "decided_by": "agent", "status": "would_send", **fields}
    for key in ("reasoning", "rule_checks", "tool_trace"):
        if key in row:
            row[key] = json.dumps(row[key])
    conn.execute(f"INSERT INTO decisions ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    conn.commit()


def _customer(conn) -> int:
    return conn.execute("SELECT id FROM customers LIMIT 1").fetchone()[0]


def test_every_rule_has_a_plain_label():
    assert all(label and not label.startswith("profit.") for label in RULE_LABELS.values())


def test_three_layers_are_counted_per_rule(db):
    cid = _customer(db)
    _insert(db, cid, offer_cost_paise=15000,
            reasoning={"summary": "x", "candidates_prepared_by_code": [
                {"id": "C1", "allowed_by_rules": True},
                {"id": "C2", "allowed_by_rules": False, "blocked_because": ["Expected profit Rs 13 is below the minimum Rs 20."]},
            ]},
            tool_trace=[{"tool": "estimate_profit", "result": "..."},
                        {"tool": "propose_offer", "result": "rejected: message.no_fake_urgency: 'hurry' is pressure; "
                                                            "profit.category_cap: too big"},
                        {"tool": "propose_offer", "result": "repaired: 10% off"}],
            rule_checks=[{"rule": "anti_spam.quiet_hours", "result": "repaired", "detail": "moved to 08:00"},
                         {"rule": "integrity", "result": "passed", "detail": "ok"}])
    report = build_report(db, get_active_rules(db), NOON_IST)
    act = report["activity"]
    assert (act["menu_options"], act["menu_blocked"], act["ai_proposals"], act["ai_rejected"], act["repairs"]) == (2, 1, 2, 1, 1)
    by_rule = {r["rule"]: r for r in act["by_rule"]}
    assert by_rule["profit.expected_profit"]["menu_blocked"] == 1
    assert by_rule["message.no_fake_urgency"]["ai_rejected"] == 1 and by_rule["profit.category_cap"]["ai_rejected"] == 1
    assert by_rule["anti_spam.quiet_hours"]["repaired"] == 1
    assert report["budgets"]["daily"]["used_rupees"] == 150 and report["budgets"]["monthly"]["used_rupees"] == 150
    assert report["budgets"]["top_customers"][0]["customer_id"] == cid


def test_breaker_gauge_and_reason_groups(db):
    cid = _customer(db)
    rules = get_active_rules(db)
    window = rules.budget["circuit_breaker"]["blocked_rate_window"]
    for _ in range(window):
        _insert(db, cid, status="blocked", decided_by="fallback", offer_cost_paise=0,
                reasoning={"summary": "x", "decided_by_note": "Decided by the fallback rules because: "
                                                              "2 proposals were rejected by the rules."})
    _insert(db, cid, status="nothing", decided_by="gatekeeper",
            reasoning={"summary": "Skipped before any offer was considered: Already got 2 offers this week (max 2)."})
    report = build_report(db, rules, NOON_IST)
    breaker = report["breaker"]
    assert breaker["recent_count"] == window and breaker["blocked_recent"] == window - 1
    assert breaker["paused"] and "blocked" in breaker["paused"]   # 95% blocked > 50% -> offers paused
    assert report["activity"]["fallback_reasons"] == [{"reason": "AI proposals rejected twice", "count": window}]
    assert report["activity"]["skip_reasons"] == [{"reason": "Weekly offer limit reached", "count": 1}]


def test_api_serves_the_report(small_db, tmp_path, monkeypatch):
    copy = tmp_path / "api.db"
    copy.write_bytes(small_db.read_bytes())
    monkeypatch.setattr(api.main, "db", lambda: connect(copy))
    data = TestClient(api.main.app).get("/api/guardrails").json()
    assert {"breaker", "budgets", "activity", "limits", "harness"} <= set(data)
    assert data["breaker"]["paused"] is None and data["limits"][0]["value"].startswith("Rs ")
