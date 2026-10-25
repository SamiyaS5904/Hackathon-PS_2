"""
Tests for Session 4: tester linking, re-linking, delivery and the event simulator (no real Telegram involved).

Proves:
  - a personal link works only for the Telegram username it was made for
  - linking saves the chat on the tester and on the customer; re-linking moves the chat to the new customer
  - queued offers for linked customers are delivered; scheduled ones only when due; unlinked ones -> 'would_send'
  - after re-linking, the OLD customer's pending offer is not sent to the tester
  - simulator events become real data the analyst sees (open cart, interest products) and lead to decisions
Run: pytest harness/test_bot.py -v
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from bot.delivery import due_deliveries, mark_sent
from bot.testers import add_tester, link_chat, list_testers, relink_tester
from engine.catalog import load_catalog
from engine.features import build_profile
from events.simulator import fire_cart, fire_search, fire_views, react, tick
from harness.conftest import NOON_IST

CHAT = 555001


def _two_customers(db) -> tuple[int, int]:
    rows = db.execute("SELECT id FROM customers WHERE marketing_consent = 1 LIMIT 2").fetchall()
    return rows[0][0], rows[1][0]


def _queue_decision(db, customer_id: int, status: str = "queued", send_at: str | None = None) -> int:
    cur = db.execute("""INSERT INTO decisions (customer_id, created_at, trigger_type, decided_by, status, message,
                        send_at, reasoning, rule_checks, rules_version, offer_cost_paise)
                        VALUES (?, '2026-10-23T06:00:00Z', 'manual', 'agent', ?, 'Hi! 2x points for you.', ?,
                        '{}', '[]', 1, 0)""", (customer_id, status, send_at))
    db.commit()
    return cur.lastrowid


# --- linking -----------------------------------------------------------------------------------------
def test_link_only_works_for_the_right_username(db):
    c1, _ = _two_customers(db)
    t = add_tester(db, "@Samiya_S", c1)
    assert t["link"].endswith(t["token"]) and t["username"] == "samiya_s"
    ok, msg = link_chat(db, t["token"], "someone_else", CHAT)
    assert not ok and "different Telegram account" in msg
    ok, msg = link_chat(db, t["token"], "SAMIYA_S", CHAT)
    assert ok and f"#{c1}" in msg
    assert db.execute("SELECT telegram_chat_id FROM customers WHERE id = ?", (c1,)).fetchone()[0] == CHAT


def test_start_without_or_with_bad_token(db):
    assert link_chat(db, None, "x", CHAT)[0] is False
    assert "not valid" in link_chat(db, "nope", "x", CHAT)[1]


def test_relink_moves_the_chat_to_the_new_customer(db):
    c1, c2 = _two_customers(db)
    t = add_tester(db, "samiya", c1)
    link_chat(db, t["token"], "samiya", CHAT)
    relink_tester(db, "samiya", c2)
    chats = dict(db.execute("SELECT id, telegram_chat_id FROM customers WHERE id IN (?, ?)", (c1, c2)).fetchall())
    assert chats[c1] is None and chats[c2] == CHAT
    assert list_testers(db)[0]["customer_id"] == c2


def test_relink_unknown_tester_fails(db):
    with pytest.raises(ValueError):
        relink_tester(db, "ghost", 1)


# --- delivery ------------------------------------------------------------------------------------------
def test_queued_offer_for_linked_customer_is_delivered_once(db):
    c1, _ = _two_customers(db)
    link_chat(db, add_tester(db, "samiya", c1)["token"], "samiya", CHAT)
    decision = _queue_decision(db, c1)
    due = due_deliveries(db, NOON_IST)
    assert [d["decision_id"] for d in due] == [decision] and due[0]["chat_id"] == CHAT
    mark_sent(db, decision, 42, NOON_IST)
    assert due_deliveries(db, NOON_IST) == []
    row = db.execute("SELECT status, telegram_message_id FROM decisions WHERE id = ?", (decision,)).fetchone()
    assert row["status"] == "sent" and row["telegram_message_id"] == 42


def test_scheduled_offer_waits_for_its_time(db):
    c1, _ = _two_customers(db)
    link_chat(db, add_tester(db, "samiya", c1)["token"], "samiya", CHAT)
    later = (NOON_IST + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _queue_decision(db, c1, "scheduled", later)
    assert due_deliveries(db, NOON_IST) == []
    assert len(due_deliveries(db, NOON_IST + timedelta(hours=3))) == 1


def test_expired_offer_is_never_sent(db):
    c1, _ = _two_customers(db)
    link_chat(db, add_tester(db, "samiya", c1)["token"], "samiya", CHAT)
    old = _queue_decision(db, c1, "scheduled", "2026-10-20T06:00:00Z")  # no final_offer -> 24h validity, long gone
    assert due_deliveries(db, NOON_IST) == []
    assert db.execute("SELECT status FROM decisions WHERE id = ?", (old,)).fetchone()[0] == "expired"


def test_unlinked_customer_offer_becomes_would_send(db):
    _, c2 = _two_customers(db)
    decision = _queue_decision(db, c2)
    assert due_deliveries(db, NOON_IST) == []
    assert db.execute("SELECT status FROM decisions WHERE id = ?", (decision,)).fetchone()[0] == "would_send"


def test_after_relink_old_customers_offer_is_not_sent_to_tester(db):
    c1, c2 = _two_customers(db)
    link_chat(db, add_tester(db, "samiya", c1)["token"], "samiya", CHAT)
    old_offer = _queue_decision(db, c1)
    relink_tester(db, "samiya", c2)
    assert due_deliveries(db, NOON_IST) == []
    assert db.execute("SELECT status FROM decisions WHERE id = ?", (old_offer,)).fetchone()[0] == "would_send"


# --- simulator -------------------------------------------------------------------------------------------
def test_fired_cart_becomes_an_open_cart(db):
    c1, _ = _two_customers(db)
    catalog = load_catalog(db)
    info = fire_cart(db, catalog, c1, NOON_IST, ago_hours=3)
    profile = build_profile(db, c1, NOON_IST, catalog)
    assert profile.open_cart and profile.open_cart["items"][0]["name"] == info["product"]


def test_fired_search_and_views_become_interest(db):
    c1, c2 = _two_customers(db)
    catalog = load_catalog(db)
    fire_search(db, catalog, c1, NOON_IST, "lipstick")
    fire_views(db, catalog, c2, NOON_IST, product_id=2)
    p1, p2 = build_profile(db, c1, NOON_IST, catalog), build_profile(db, c2, NOON_IST, catalog)
    assert any("searched 'lipstick'" in s for i in p1.intent_products for s in i["signals"])
    assert any(i["product_id"] == 2 for i in p2.intent_products)


def test_search_with_unknown_query_fails_clearly(db):
    with pytest.raises(ValueError):
        fire_search(db, load_catalog(db), 1, NOON_IST, "spaceship")


def test_cart_event_leads_to_a_decision(db):
    c1, _ = _two_customers(db)
    catalog = load_catalog(db)
    fire_cart(db, catalog, c1, NOON_IST)
    outcome = react(db, catalog, c1, NOON_IST, use_agent=False, preferred="cart_abandoned")
    assert outcome is not None and outcome.trigger_type == "cart_abandoned" and outcome.decision_id


def test_tick_decides_for_linked_testers_and_background_customers(db):
    c1, _ = _two_customers(db)
    catalog = load_catalog(db)
    link_chat(db, add_tester(db, "samiya", c1)["token"], "samiya", CHAT)
    fire_cart(db, catalog, c1, NOON_IST)
    result = tick(db, catalog, n_customers=10, ai_for="none", now=NOON_IST, seed=1)
    assert any(o.customer_id == c1 for o in result["decisions"])
    assert all(o.decided_by in ("fallback", "gatekeeper") for o in result["decisions"])  # no AI in tests
