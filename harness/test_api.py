"""
Tests for the dashboard API (api/main.py), against a small temporary database (never the real one, never the AI).

Proves every dashboard action works end to end: Day Plan (with the .env ceiling), overview numbers, Run agent ->
explained decision, decision feed and detail, customer search and profile, firing a live event, tester link and
re-link, rules editing (new version; unsafe edits refused with reasons), follow-ups.
Run: pytest harness/test_api.py -v
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import api.main
from db.store import connect


@pytest.fixture
def client(small_db, tmp_path, monkeypatch):
    copy = tmp_path / "api.db"
    copy.write_bytes(small_db.read_bytes())
    monkeypatch.setattr(api.main, "db", lambda: connect(copy))
    api.main._suggest_cache.update(at=0.0, data=[])
    return TestClient(api.main.app)


def _customer(client) -> int:
    return next(c["id"] for c in client.get("/api/customers?limit=40").json() if c["marketing_consent"])


def test_health_and_day_plan(client):
    assert client.get("/api/health").json()["ok"] is True
    plan = client.get("/api/day-plan").json()
    assert plan["greeting"].startswith("Good") and plan["today"]["limit"] <= plan["ceiling"]
    saved = client.put("/api/day-plan", json={"date": plan["tomorrow"]["date"], "limit": 999}).json()
    assert saved["capped"] is True and saved["limit"] == plan["ceiling"]


def test_run_agent_returns_explained_decision_and_shows_in_feed(client):
    cid = _customer(client)
    d = client.post(f"/api/customers/{cid}/run", json={"use_agent": False}).json()
    assert d["customer_id"] == cid and d["reasoning"]["summary"] and d["decided_by"] in ("fallback", "gatekeeper")
    feed = client.get("/api/decisions").json()
    assert feed[0]["id"] == d["id"]
    assert client.get(f"/api/decisions/{d['id']}").json()["id"] == d["id"]
    assert client.get("/api/decisions/99999").status_code == 404
    overview = client.get("/api/overview").json()
    assert overview["today"]["decisions"] >= 1 and len(overview["week"]) == 7


def test_customer_search_profile_and_suggestions(client):
    cid = _customer(client)
    assert client.get(f"/api/customers?search={cid}").json()[0]["id"] == cid
    detail = client.get(f"/api/customers/{cid}").json()
    assert detail["profile"]["customer_id"] == cid and "triggers" in detail and "orders" in detail
    assert client.get("/api/customers/99999").status_code == 404
    assert isinstance(client.get("/api/customers/suggested").json(), list)


def test_fire_cart_event_decides(client):
    cid = _customer(client)
    res = client.post(f"/api/customers/{cid}/events", json={"event": "cart", "use_agent": False}).json()
    assert res["event"]["event"] == "cart" and res["decision"]["trigger_type"] == "cart_abandoned"
    bad = client.post(f"/api/customers/{cid}/events", json={"event": "search", "query": "spaceship"})
    assert bad.status_code == 400


def test_testers_link_and_relink(client):
    ids = [c["id"] for c in client.get("/api/customers?limit=5").json()]
    t = client.post("/api/testers", json={"username": "@Demo_User", "customer_id": ids[0]}).json()
    assert t["start_command"].startswith("/start ") and t["link"].endswith(t["token"])
    r = client.post("/api/testers/demo_user/relink", json={"customer_id": ids[1]}).json()
    assert r["customer_id"] == ids[1] and client.get("/api/testers").json()[0]["customer_id"] == ids[1]
    assert client.post("/api/testers/nobody/relink", json={"customer_id": ids[0]}).status_code == 400


def test_rules_edit_creates_version_and_refuses_unsafe(client):
    rules = client.get("/api/rules").json()
    assert rules["version"] == 1 and any(f["path"] == "profit.category_cap_pct.electronics" for f in rules["fields"])
    ok = client.put("/api/rules", json={"changes": {"profit.category_cap_pct.electronics": 12},
                                        "changed_by": "test", "note": "margins up"}).json()
    assert ok["version"] == 2 and ok["changes"][0]["new"] == 12
    bad = client.put("/api/rules", json={"changes": {"profit.category_cap_pct.electronics": 40}})
    assert bad.status_code == 400 and "too high" in str(bad.json())
    assert client.put("/api/rules", json={"changes": {"message.must_match_offer": 0}}).status_code == 400


def test_followups_endpoint(client):
    assert client.post("/api/followups/run").json() == []


def test_tester_profiles_are_grouped_and_offer_ready(client):
    api.main._profiles_cache.update(at=0.0, data=[])
    segments = client.get("/api/testers/profiles?refresh=true").json()
    assert [s["key"] for s in segments][:2] == ["about_to_leave_vip", "points_collector"]
    assert all(s["label"] and s["signals"] and s["expect"] for s in segments)
    linked = {t["customer_id"] for t in client.get("/api/testers").json()}
    picks = [c for s in segments for c in s["customers"]]
    assert len({c["id"] for c in picks}) == len(picks)  # a customer is in one segment only
    assert not linked & {c["id"] for c in picks}       # never a customer already played by a tester
