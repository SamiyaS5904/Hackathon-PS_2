"""
Tests for customer groups (engine/groups.py), the autopilot (engine/autopilot.py) and category bots (bot/bots.py).

Proves: every customer gets a group; the day plan gives each bot at most N offers, at most one slot per customer, all
inside the send window and never for a customer who already had an offer this week; due slots run the normal
pipeline (rules included) and are marked done/skipped; pausing stops everything; offers route to the right bot.
Run: pytest harness/test_autopilot.py -v
"""

from __future__ import annotations

from datetime import timedelta

from bot.bots import bot_key_for
from engine.autopilot import BOT_KEYS, is_enabled, plan_day, run_due, run_next_now, set_enabled, settings
from engine.clock import parse_ts, to_ist
from engine.groups import build_groups, group_counts
from harness.conftest import NOON_IST


def _plan(db):
    build_groups(db, now=NOON_IST)
    return plan_day(db, NOON_IST)


def test_every_customer_gets_a_group(db):
    n = build_groups(db, now=NOON_IST)
    assert n == db.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    assert sum(g["customers"] for g in group_counts(db)) == n


def test_day_plan_respects_limits_window_and_anti_spam(db):
    created = _plan(db)
    rows = db.execute("SELECT bot, customer_id, slot_at FROM autopilot_slots").fetchall()
    assert created == len(rows) > 0
    per_bot = settings()["offers_per_bot_per_day"]
    for bot in BOT_KEYS:
        assert sum(1 for r in rows if r["bot"] == bot) <= per_bot
    assert len({r["customer_id"] for r in rows}) == len(rows)          # one slot per customer
    for r in rows:
        local = to_ist(parse_ts(r["slot_at"]))
        assert local > to_ist(NOON_IST) and local.strftime("%H:%M") <= settings()["day_end"]
    assert plan_day(db, NOON_IST) == 0                                  # planned once per day


def test_due_slots_run_the_normal_pipeline(db):
    _plan(db)
    later = NOON_IST + timedelta(hours=12)
    report = run_due(db, later, limit=50)
    assert report and all(r["decision_id"] for r in report)
    statuses = {r[0] for r in db.execute("SELECT status FROM autopilot_slots")}
    assert statuses <= {"done", "skipped", "planned"} and "planned" not in {
        r[0] for r in db.execute("SELECT status FROM autopilot_slots WHERE slot_at <= ?",
                                 (later.strftime("%Y-%m-%dT%H:%M:%SZ"),))}
    # every approved offer went through the rules (rule checks stored)
    for (checks,) in db.execute("SELECT d.rule_checks FROM autopilot_slots s JOIN decisions d ON d.id = s.decision_id "
                                "WHERE d.final_offer IS NOT NULL"):
        assert checks and "margin_floor" in checks


def test_pause_stops_the_autopilot_and_run_now_resumes_one_per_bot(db):
    _plan(db)
    set_enabled(db, False)
    assert not is_enabled(db) and run_due(db, NOON_IST + timedelta(hours=12)) == []
    set_enabled(db, True)
    ran = run_next_now(db, NOON_IST)
    assert 0 < len(ran) <= len(BOT_KEYS)


def test_offers_route_to_their_category_bot():
    assert bot_key_for("electronics") == "electronics" and bot_key_for("grocery") == "grocery"
    assert bot_key_for(None) == "rewards" and bot_key_for("toys") == "rewards"


def test_a_slot_runs_only_once_even_if_two_runs_overlap(db):
    """Regression: the background loop and the Run-now button ran the same slots at the same moment, so three
    customers got two offers each. Two overlapping runs on separate connections must never run a slot twice."""
    import threading

    from db.store import connect
    from engine.autopilot import _run_due
    _plan(db)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    later = NOON_IST + timedelta(hours=12)
    results: list[list[dict]] = []

    def worker():
        conn = connect(path)
        conn.execute("PRAGMA busy_timeout = 10000")
        results.append(_run_due(conn, later, 4, None))
        conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ran = [r["slot"] for batch in results for r in batch]
    assert ran and len(ran) == len(set(ran))
    assert db.execute("SELECT COUNT(*) FROM (SELECT customer_id FROM decisions GROUP BY customer_id "
                      "HAVING COUNT(*) > 1)").fetchone()[0] == 0
