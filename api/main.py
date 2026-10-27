"""
The dashboard API (FastAPI): everything the company dashboard shows or does goes through here.

Where it sits: STEP 7. It reads the database and calls the engine; it contains no decision logic of its own:
    dashboard (React) -> /api/... -> engine (decide, rules, gatekeeper, testers, simulator) -> SQLite

Run:
    uvicorn api.main:app --port 8010         # API on http://127.0.0.1:8010 (docs at /docs)
If the dashboard has been built (cd dashboard && npm run build), it is also served at http://127.0.0.1:8010/.

What the endpoints cover (CLAUDE.md section 9, Session 5):
  /api/day-plan     greeting + "Plan your day": AI decisions allowed today/tomorrow, used so far, cost estimate
  /api/overview     today's numbers, last 7 days, circuit breaker, model info
  /api/decisions    the feed, and one decision with its full reasoning, rule checks, menu and tool trace
  /api/customers    search, suggested customers (with live triggers), one profile, Run agent, fire an event
  /api/testers      link (personal link + /start code), re-link, list, demo profiles to play (by visible signals)
  /api/rules        active rules, version history, edit with safety bounds (pending offers re-checked)
  /api/followups    resolve "wait" decisions that are due (also done automatically every 5 minutes, see below)
  /api/guardrails   circuit breaker gauges, budgets, what each rule caught, latest harness results
The Claude API key is never used here directly: only engine/agent.py uses it, via decide().
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from collections import defaultdict
from datetime import timedelta

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bot.bots import bot_key_for, bot_token, bots_status
from bot.manager import approve, discard, manager_chat
from bot.testers import add_tester, list_testers, relink_tester
from config.settings import PROJECT_ROOT, settings
from db.store import connect
from engine.catalog import load_catalog
from engine.clock import ist_date, now_utc, parse_ts, start_of_ist_day, to_ist, to_ts
from engine.decide import decide, recheck_scheduled
from engine.features import SENT_STATUSES, build_profile
from engine.followups import run_due_followups
from engine.gatekeeper import (USD_TO_INR, agent_runs_used, average_ai_cost_usd, circuit_breaker, estimated_cost,
                               get_day_plan, set_day_plan)
from engine.autopilot import plan_day, run_due, run_next_now, set_enabled, today_view
from engine.groups import group_counts
from engine.guardrails import build_report
from engine.models import OfferProposal
from engine.rules import RuleState
from engine.rules_store import RulesValidationError, get_active_rules, list_versions, save_rules, with_change
from engine.segments import demo_profiles
from engine.triggers import TRIGGER_TYPES, detect_triggers
from engine.uplift import get_model
from events.simulator import fire_cart, fire_search, fire_views, react

FOLLOWUP_EVERY_SECONDS = 300
AUTOPILOT_EVERY_SECONDS = 30
log = logging.getLogger("uvicorn.error")  # shows up in the uvicorn console


async def _followup_loop() -> None:
    """
    Every 5 minutes, resolve 'wait' decisions whose time has come (engine/followups.py): bought on their own ->
    nothing spent; didn't buy -> a new decision. Without this, a "wait" would only be resolved when someone clicks
    the button or runs the simulator, and the AI's timing plan would never close. It lives here, not in the
    Telegram bot, because a follow-up can call the AI and the bot must never use the Claude API.
    """
    rounds = 0
    while True:
        try:
            if rounds % (FOLLOWUP_EVERY_SECONDS // AUTOPILOT_EVERY_SECONDS) == 0:
                report = await asyncio.to_thread(lambda: run_due_followups(connect()))
                for line in report:
                    log.info("follow-up: %s", line)
            # Autopilot: plan today's offers per category bot (once a day) and run the slots that are due
            ran = await asyncio.to_thread(_autopilot_round)
            for line in ran:
                log.info("autopilot: %s", line)
        except Exception:  # noqa: BLE001 - a failed round must not stop the loop; try again next time
            log.exception("background round failed")
        rounds += 1
        await asyncio.sleep(AUTOPILOT_EVERY_SECONDS)


def _autopilot_round() -> list[dict]:
    conn = connect()
    try:
        plan_day(conn)
        return run_due(conn)
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_followup_loop()) if os.getenv("OFFERPILOT_FOLLOWUP_LOOP", "1") == "1" else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="OfferPilot API", version="1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["*"], allow_headers=["*"])


def db() -> sqlite3.Connection:
    return connect()


# ---------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------
def _loads(value: str | None, default):
    return json.loads(value) if value else default


@lru_cache(maxsize=1)
def _catalog():
    """The product catalog never changes while the API runs, so it's loaded once."""
    return load_catalog(connect())


def _offer_text(final_offer: str | None) -> str | None:
    """'10% off on Glowveda Lipstick Neo (min order Rs 499)' instead of 'product #139'."""
    if not final_offer:
        return None
    offer = OfferProposal.model_validate_json(final_offer)
    text = offer.describe()
    product = _catalog().get(offer.product_id)
    return text.replace(f"product #{offer.product_id}", product.name) if product else text


def _decision_row(r: sqlite3.Row) -> dict:
    """One decision, compact, for the feed."""
    reasoning = _loads(r["reasoning"], {})
    return {
        "id": r["id"], "created_at": r["created_at"], "customer_id": r["customer_id"], "first_name": r["first_name"],
        "trigger_type": r["trigger_type"], "decided_by": r["decided_by"], "status": r["status"],
        "offer": _offer_text(r["final_offer"]), "message": r["message"], "send_at": r["send_at"],
        "sent_at": r["sent_at"], "summary": reasoning.get("summary"),
        "expected_profit_rupees": r["expected_profit_paise"] // 100 if r["expected_profit_paise"] is not None else None,
        "offer_cost_rupees": r["offer_cost_paise"] // 100 if r["offer_cost_paise"] is not None else None,
        "ai_cost_inr": round(r["cost_usd"] * USD_TO_INR, 2) if r["cost_usd"] else None,
        "rules_version": r["rules_version"],
        "offer_kind": r["offer_kind"] if "offer_kind" in r.keys() else "normal",
    }


def _decision_detail(conn: sqlite3.Connection, decision_id: int) -> dict:
    r = conn.execute("""SELECT d.*, c.first_name, c.last_name, c.city FROM decisions d
                        JOIN customers c ON c.id = d.customer_id WHERE d.id = ?""", (decision_id,)).fetchone()
    if r is None:
        raise HTTPException(404, f"Decision #{decision_id} not found")
    reasoning = _loads(r["reasoning"], {})
    return {
        **_decision_row(r),
        "customer": {"id": r["customer_id"], "name": f"{r['first_name']} {r['last_name']}", "city": r["city"]},
        "trigger_detail": _loads(r["trigger_detail"], None),
        "final_offer": _loads(r["final_offer"], None),
        "proposal": _loads(r["proposal"], None),
        "reasoning": {k: v for k, v in reasoning.items() if k != "candidates_prepared_by_code"},
        "candidates": reasoning.get("candidates_prepared_by_code", []),
        "rule_checks": _loads(r["rule_checks"], []),
        "tool_trace": _loads(r["tool_trace"], []),
        "tokens": {"input": r["input_tokens"], "output": r["output_tokens"]},
        "ai_cost_usd": r["cost_usd"],
        "variant_id": r["variant_id"],
        "variants": _loads(r["variants"], []),
    }


# ---------------------------------------------------------------------------------------------------
# Health, Day Plan, Overview
# ---------------------------------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict:
    model = get_model()
    return {"ok": True, "ai_enabled": settings.agent_enabled, "model": settings.claude_model,
            "bot": settings.telegram_bot_username, "uplift_model": model.info if model else None,
            "now_ist": to_ist(now_utc()).strftime("%d %b %Y, %H:%M")}


def _greeting(hour: int) -> str:
    return "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"


@app.get("/api/day-plan")
def day_plan() -> dict:
    conn, now = db(), now_utc()
    today, tomorrow = ist_date(now), ist_date(now + timedelta(days=1))
    per = average_ai_cost_usd(conn)
    plan_today, plan_tomorrow = get_day_plan(conn, today), get_day_plan(conn, tomorrow)
    used = agent_runs_used(conn, now)
    spent = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM decisions WHERE created_at >= ?",
                         (to_ts(start_of_ist_day(now)),)).fetchone()[0]
    return {
        "greeting": _greeting(to_ist(now).hour), "now_ist": to_ist(now).strftime("%A, %d %b · %H:%M"),
        "ceiling": settings.max_agent_runs_per_day, "ai_enabled": settings.agent_enabled,
        "today": {"date": today, "limit": plan_today, "used": used, "remaining": max(plan_today - used, 0),
                  "spent_inr": round(spent * USD_TO_INR, 2), "estimate": estimated_cost(plan_today, per)},
        "tomorrow": {"date": tomorrow, "limit": plan_tomorrow, "estimate": estimated_cost(plan_tomorrow, per)},
    }


class DayPlanIn(BaseModel):
    date: str
    limit: int = Field(ge=0)


@app.put("/api/day-plan")
def update_day_plan(body: DayPlanIn) -> dict:
    saved = set_day_plan(db(), body.date, body.limit)
    return {"date": body.date, "limit": saved, "capped": saved != body.limit}


@app.get("/api/overview")
def overview() -> dict:
    conn, now = db(), now_utc()
    rules = get_active_rules(conn)
    start = to_ts(start_of_ist_day(now))
    today = conn.execute("SELECT decided_by, status, offer_cost_paise, expected_profit_paise, cost_usd FROM decisions "
                         "WHERE created_at >= ?", (start,)).fetchall()
    out_today = [r for r in today if r["status"] in SENT_STATUSES or r["status"] == "sent"]
    spent_paise = sum(r["offer_cost_paise"] or 0 for r in out_today)

    week: dict[str, dict] = {}
    for i in range(6, -1, -1):
        day = ist_date(now - timedelta(days=i))
        week[day] = {"date": day, "agent": 0, "fallback": 0, "gatekeeper": 0, "offers_out": 0, "ai_cost_inr": 0.0}
    for r in conn.execute("SELECT created_at, decided_by, status, cost_usd FROM decisions WHERE created_at >= ?",
                          (to_ts(start_of_ist_day(now - timedelta(days=6))),)):
        day = ist_date(parse_ts(r["created_at"]))
        if day in week:
            week[day][r["decided_by"]] = week[day].get(r["decided_by"], 0) + 1
            week[day]["offers_out"] += r["status"] in (*SENT_STATUSES, "sent")
            week[day]["ai_cost_inr"] = round(week[day]["ai_cost_inr"] + (r["cost_usd"] or 0) * USD_TO_INR, 2)

    by_status: dict[str, int] = defaultdict(int)
    for (status, n) in conn.execute("SELECT status, COUNT(*) FROM decisions GROUP BY status"):
        by_status[status] = n
    paused = circuit_breaker(conn, rules, RuleState(spent_today_paise=spent_paise), now)
    return {
        "today": {
            "decisions": len(today),
            "by_decider": {k: sum(1 for r in today if r["decided_by"] == k) for k in ("agent", "fallback", "gatekeeper")},
            "offers_out": len(out_today),
            "offers_delivered_telegram": sum(1 for r in today if r["status"] == "sent"),
            "expected_profit_rupees": sum(r["expected_profit_paise"] or 0 for r in out_today) // 100,
            "offer_budget": {"used_rupees": spent_paise // 100, "limit_rupees": rules.budget["daily_rupees"]},
            "ai_cost_inr": round(sum(r["cost_usd"] or 0 for r in today) * USD_TO_INR, 2),
        },
        "all_time_status": dict(by_status),
        "week": list(week.values()),
        "circuit_breaker": paused,
        "rules_version": rules.version,
        "uplift_model": get_model().info if get_model() else None,
    }


# ---------------------------------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------------------------------
@app.get("/api/decisions")
def decisions(limit: int = 50, status: str | None = None, decided_by: str | None = None,
              customer_id: int | None = None) -> list[dict]:
    sql = "SELECT d.*, c.first_name FROM decisions d JOIN customers c ON c.id = d.customer_id WHERE 1=1"
    args: list = []
    for col, val in (("d.status", status), ("d.decided_by", decided_by), ("d.customer_id", customer_id)):
        if val is not None:
            sql += f" AND {col} = ?"
            args.append(val)
    sql += " ORDER BY d.id DESC LIMIT ?"
    args.append(min(limit, 500))
    return [_decision_row(r) for r in db().execute(sql, args)]


@app.get("/api/autopilot")
def autopilot() -> dict:
    """
    The Autopilot page: today's planned and sent offers per category bot, in time order, each with the customer,
    their group, the offer, the exact Telegram message, why, and the money. Plus one summary card per bot.
    """
    conn = db()
    plan_day(conn)
    view = today_view(conn)
    status = {b["key"]: b for b in bots_status()}
    rows = []
    for r in view["rows"]:
        reasoning = _loads(r["reasoning"], {})
        offer = _loads(r["final_offer"], None)
        bot = r["sent_via"] if r["sent_via"] and r["sent_via"] != "main" else (
            bot_key_for(offer.get("category")) if offer else r["bot"])
        rows.append({
            "slot_id": r["id"], "time": r["sent_at"] or r["slot_at"], "planned_at": r["slot_at"],
            "bot": bot, "planned_bot": r["bot"], "customer_id": r["customer_id"], "name": r["first_name"],
            "group": r["group_label"], "reason": r["reason"], "slot_status": r["slot_status"],
            "decision_id": r["decision_id"], "status": r["status"] or "planned",
            "offer": _offer_text(r["final_offer"]), "offer_kind": r["offer_kind"], "message": r["message"],
            "why": reasoning.get("summary"), "decided_by": r["decided_by"],
            "expected_profit_rupees": r["expected_profit_paise"] // 100 if r["expected_profit_paise"] is not None else None,
            "offer_cost_rupees": r["offer_cost_paise"] // 100 if r["offer_cost_paise"] is not None else None,
        })
    started = {r[0].removeprefix("bot_started_") for r in conn.execute(
        "SELECT key FROM meta WHERE key LIKE 'bot_started_%'")} if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'meta'").fetchone() else set()
    main_token = settings.telegram_bot_token
    cards = []
    for key, info in status.items():
        # ready = its own token works AND the manager pressed Start in it (the Rewards bot is the main bot: always ready)
        info = {**info, "ready": bool(info["connected"]) and (key in started or bot_token(key) == main_token)}
        mine = [x for x in rows if x["bot"] == key]
        upcoming = [x for x in rows if x["planned_bot"] == key and x["slot_status"] == "planned"]
        cards.append({**info,
                      "planned": len([x for x in rows if x["planned_bot"] == key]),
                      "sent": sum(1 for x in mine if x["status"] == "sent"),
                      "no_offer": sum(1 for x in rows if x["planned_bot"] == key and x["slot_status"] != "planned"
                                      and not x["offer"]),
                      "expected_profit_rupees": sum(x["expected_profit_rupees"] or 0 for x in mine
                                                    if x["status"] in ("sent", "queued", "scheduled")),
                      "next_at": upcoming[0]["planned_at"] if upcoming else None})
    sent = [x for x in rows if x["status"] == "sent"]
    return {"day": view["day"], "enabled": view["enabled"], "bots": cards, "rows": rows,
            "groups": group_counts(conn),
            "totals": {"planned": len(rows), "sent": len(sent),
                       "waiting": sum(1 for x in rows if x["slot_status"] == "planned"),
                       "no_offer": sum(1 for x in rows if x["slot_status"] != "planned" and not x["offer"]),
                       "expected_profit_rupees": sum(x["expected_profit_rupees"] or 0 for x in sent),
                       "ai_decisions": sum(1 for x in rows if x["decided_by"] == "agent")}}


class AutopilotIn(BaseModel):
    on: bool


@app.post("/api/autopilot/toggle")
def autopilot_toggle(body: AutopilotIn) -> dict:
    set_enabled(db(), body.on)
    return {"enabled": body.on}


@app.post("/api/autopilot/run-now")
def autopilot_run_now() -> dict:
    """Demo button: run the next planned offer of every bot right now (one decision per bot)."""
    conn = db()
    set_enabled(conn, True)
    return {"ran": run_next_now(conn)}


@app.post("/api/autopilot/replan")
def autopilot_replan() -> dict:
    """Re-plan the rest of today (keeps offers already decided)."""
    return {"planned": plan_day(db(), force=True)}


@app.get("/api/outbox")
def outbox(limit: int = 40) -> dict:
    """
    The offer schedule: who gets which offer, when, and through which channel (a tester's Telegram or
    'would send' when nobody plays that customer), with expected profit and cost. Newest first.
    """
    conn = db()
    rows = conn.execute("""
        SELECT d.*, c.first_name, c.telegram_chat_id, t.telegram_username FROM decisions d
        JOIN customers c ON c.id = d.customer_id
        LEFT JOIN testers t ON t.customer_id = d.customer_id
        WHERE d.final_offer IS NOT NULL
          AND d.status IN ('review', 'queued', 'scheduled', 'sent', 'would_send', 'expired', 'cancelled')
        ORDER BY COALESCE(d.sent_at, d.send_at, d.created_at) DESC LIMIT ?""", (limit,)).fetchall()
    items = []
    manager = manager_chat(conn)
    for r in rows:
        linked = r["telegram_chat_id"] is not None and r["telegram_username"]
        channel = (f"Telegram @{manager[0]}" if manager else
                   f"Telegram @{r['telegram_username']}" if linked else "Not linked (would send)")
        items.append({**_decision_row(r), "goes_at": r["sent_at"] or r["send_at"] or r["created_at"],
                      "channel": channel})
    live = [i for i in items if i["status"] in ("queued", "scheduled", "sent", "would_send", "review")]
    return {"items": items,
            "totals": {"offers": len(live),
                       "on_telegram": sum(1 for i in live if i["status"] in ("queued", "scheduled", "sent")),
                       "expected_profit_rupees": sum(i["expected_profit_rupees"] or 0 for i in live),
                       "offer_cost_rupees": sum(i["offer_cost_rupees"] or 0 for i in live)}}


@app.get("/api/decisions/{decision_id}")
def decision(decision_id: int) -> dict:
    return _decision_detail(db(), decision_id)


# ---------------------------------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------------------------------
@app.get("/api/customers")
def customers(search: str = "", limit: int = 25) -> list[dict]:
    conn = db()
    sql = """SELECT c.id, c.first_name, c.last_name, c.city, c.marketing_consent, c.telegram_chat_id IS NOT NULL linked,
                    (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id AND o.status != 'cancelled') orders,
                    (SELECT MAX(ts) FROM orders o WHERE o.customer_id = c.id) last_order
             FROM customers c"""
    args: list = []
    if search.strip():
        if search.strip().isdigit():
            sql += " WHERE c.id = ?"
            args.append(int(search))
        else:
            sql += " WHERE c.first_name || ' ' || c.last_name LIKE ? OR c.city LIKE ?"
            args += [f"%{search.strip()}%"] * 2
    sql += " ORDER BY c.id LIMIT ?"
    args.append(min(limit, 100))
    return [dict(r) for r in conn.execute(sql, args)]


_suggest_cache: dict = {"at": 0.0, "data": []}


@app.get("/api/customers/suggested")
def suggested_customers(refresh: bool = False) -> list[dict]:
    """Customers with a live trigger right now (cart, interest, habit break...), strongest first: good demo picks."""
    if not refresh and time.time() - _suggest_cache["at"] < 120:
        return _suggest_cache["data"]
    conn, now = db(), now_utc()
    catalog = load_catalog(conn)
    recent = {r[0] for r in conn.execute("SELECT DISTINCT customer_id FROM decisions WHERE created_at >= ?",
                                         (to_ts(now - timedelta(days=2)),))}
    ids = [r[0] for r in conn.execute("SELECT id FROM customers WHERE marketing_consent = 1 ORDER BY id")]
    out = []
    for cid in ids[::10]:  # every 10th customer: a fast, spread-out sample of 300
        if cid in recent:
            continue
        p = build_profile(conn, cid, now, catalog)
        found = detect_triggers(p)
        if found:
            t = found[0]
            out.append({"id": cid, "name": p.first_name, "city": p.city, "trigger": t.type,
                        "trigger_label": TRIGGER_TYPES[t.type], "strength": t.strength, "detail": t.detail,
                        "habit_status": p.habit_status, "orders": p.total_orders,
                        "buys_anyway": p.p_buy_48h_no_offer})
    # Mix the trigger types (strongest of each type in turn), so the list shows different kinds of moments
    by_type: dict[str, list] = defaultdict(list)
    for item in sorted(out, key=lambda x: -x["strength"]):
        by_type[item["trigger"]].append(item)
    mixed = []
    while len(mixed) < 15 and any(by_type.values()):
        for items in by_type.values():
            if items and len(mixed) < 15:
                mixed.append(items.pop(0))
    _suggest_cache.update(at=time.time(), data=mixed)
    return _suggest_cache["data"]


@app.get("/api/customers/{customer_id}")
def customer(customer_id: int) -> dict:
    conn, now = db(), now_utc()
    catalog = load_catalog(conn)
    try:
        p = build_profile(conn, customer_id, now, catalog)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    orders = []
    for o in conn.execute("SELECT id, ts, net_paise, discount_paise, status FROM orders WHERE customer_id = ? "
                          "ORDER BY ts DESC LIMIT 6", (customer_id,)):
        items = [f"{r[0]} x{r[1]}" for r in conn.execute(
            "SELECT p.name, oi.qty FROM order_items oi JOIN products p ON p.id = oi.product_id WHERE oi.order_id = ?",
            (o["id"],))]
        orders.append({"date": o["ts"][:10], "paid_rupees": o["net_paise"] // 100,
                       "discount_rupees": o["discount_paise"] // 100, "status": o["status"], "items": items})
    tester = conn.execute("SELECT telegram_username FROM testers WHERE customer_id = ?", (customer_id,)).fetchone()
    return {
        "profile": p.to_dict(),
        "triggers": [{"type": t.type, "label": TRIGGER_TYPES[t.type], "strength": t.strength, "detail": t.detail,
                      "suggested_delay_hours": t.suggested_delay_hours} for t in detect_triggers(p)],
        "orders": orders,
        "decisions": decisions(limit=10, customer_id=customer_id),
        "tester": (manager_chat(conn) or tester or (None,))[0],
    }


class RunIn(BaseModel):
    use_agent: bool = True
    trigger: str = "auto"   # 'auto' = strongest detected trigger, else 'manual'
    draft: bool = False     # True = save an approved offer as a draft; the manager clicks Send to release it


@app.post("/api/customers/{customer_id}/run")
def run_agent(customer_id: int, body: RunIn) -> dict:
    """The dashboard's "Run agent" button: one full decision (AI or fallback), saved and explained."""
    conn, now = db(), now_utc()
    catalog = load_catalog(conn)
    trigger, detail = "manual", None
    if body.trigger == "auto":
        found = detect_triggers(build_profile(conn, customer_id, now, catalog))
        if found:
            trigger, detail = found[0].type, found[0].detail
    outcome = decide(conn, customer_id, trigger, detail, now=now, use_agent=body.use_agent, catalog=catalog,
                     hold=body.draft)
    return _decision_detail(conn, outcome.decision_id)


@app.post("/api/decisions/{decision_id}/send")
def send_decision(decision_id: int) -> dict:
    """The manager's Send button: release a draft; the bot delivers it within ~15 s (after quiet hours if needed)."""
    conn = db()
    if approve(conn, decision_id, now_utc()) is None:
        raise HTTPException(400, "Only a draft offer can be sent (it may already be sent or discarded).")
    return _decision_detail(conn, decision_id)


@app.post("/api/decisions/{decision_id}/discard")
def discard_decision(decision_id: int) -> dict:
    """The manager's Discard button: cancel a draft before it is sent."""
    conn = db()
    if not discard(conn, decision_id):
        raise HTTPException(400, "Only a draft offer can be discarded.")
    return _decision_detail(conn, decision_id)


class EventIn(BaseModel):
    event: str               # 'cart' | 'search' | 'view'
    query: str = "lipstick"
    product_id: int | None = None
    ago_hours: float = 3.0
    use_agent: bool = True


@app.post("/api/customers/{customer_id}/events")
def fire_event(customer_id: int, body: EventIn) -> dict:
    """Simulate a live moment (cart left, search, repeat views) as real data, then decide for it."""
    conn, now = db(), now_utc()
    catalog = load_catalog(conn)
    try:
        if body.event == "cart":
            info, preferred = fire_cart(conn, catalog, customer_id, now, body.product_id, body.ago_hours), "cart_abandoned"
        elif body.event == "search":
            info, preferred = fire_search(conn, catalog, customer_id, now, body.query), "product_interest"
        elif body.event == "view":
            info, preferred = fire_views(conn, catalog, customer_id, now, body.product_id), "product_interest"
        else:
            raise HTTPException(400, "event must be cart, search or view")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    outcome = react(conn, catalog, customer_id, now, use_agent=body.use_agent, preferred=preferred)
    return {"event": info, "decision": _decision_detail(conn, outcome.decision_id) if outcome else None}


# ---------------------------------------------------------------------------------------------------
# Testers
# ---------------------------------------------------------------------------------------------------
@app.get("/api/testers")
def testers() -> list[dict]:
    return list_testers(db())


class TesterIn(BaseModel):
    username: str
    customer_id: int


@app.post("/api/testers")
def create_tester(body: TesterIn) -> dict:
    try:
        t = add_tester(db(), body.username, body.customer_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {**t, "start_command": f"/start {t['token']}", "bot": settings.telegram_bot_username}


class RelinkIn(BaseModel):
    customer_id: int


_profiles_cache: dict = {"at": 0.0, "data": []}


@app.get("/api/testers/profiles")
def tester_profiles(refresh: bool = False) -> list[dict]:
    """Good customers for a tester to play, grouped by visible signals (engine/segments.py). Cached 10 minutes:
    building ~400 profiles takes a few seconds."""
    if refresh or time.time() - _profiles_cache["at"] > 600 or not _profiles_cache["data"]:
        conn = db()
        _profiles_cache.update(at=time.time(), data=demo_profiles(conn, load_catalog(conn), now_utc(), sample_step=8))
    return _profiles_cache["data"]


@app.post("/api/testers/{username}/relink")
def relink(username: str, body: RelinkIn) -> dict:
    try:
        return relink_tester(db(), username, body.customer_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ---------------------------------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------------------------------
EDITABLE_RULES = [
    ("Profit protection", "profit.min_expected_profit_rupees", "Minimum expected profit per offer", "₹"),
    ("Profit protection", "profit.margin_floor_pct", "Margin floor after the offer", "%"),
    *[("Category discount caps", f"profit.category_cap_pct.{c}", f"{c.capitalize()} cap", "%")
      for c in ("electronics", "fashion", "beauty", "home", "grocery")],
    ("Profit protection", "profit.buys_anyway_threshold", "'Buys anyway' threshold (0-1)", ""),
    ("Profit protection", "profit.cashback_max_rupees", "Max wallet cashback", "₹"),
    ("Profit protection", "profit.points_multiplier_max", "Max points multiplier", "x"),
    ("Profit protection", "profit.free_delivery_min_order_rupees", "Free delivery minimum order", "₹"),
    ("Budget", "budget.daily_rupees", "Daily offer budget", "₹"),
    ("Budget", "budget.monthly_rupees", "Monthly offer budget", "₹"),
    ("Budget", "budget.per_customer_monthly_rupees", "Per-customer monthly cap", "₹"),
    ("Anti-spam", "anti_spam.max_offers_per_7_days", "Max offers per customer per 7 days", ""),
    ("Anti-spam", "anti_spam.min_hours_between_offers", "Min hours between offers", "h"),
    ("Anti-spam", "anti_spam.quiet_hours_start", "Quiet hours start (IST)", ""),
    ("Anti-spam", "anti_spam.quiet_hours_end", "Quiet hours end (IST)", ""),
    ("Habit protection", "habit.full_price_threshold", "No coupons from this full-price share (0-1)", ""),
    ("Retention and win-back", "retention.churn_avoidance_share", "Share of won-back customers who stay (0-1)", ""),
    ("Retention and win-back", "retention.horizon_weeks", "Retention horizon", "weeks"),
    ("Retention and win-back", "winback.share_of_daily_budget", "Win-back share of daily budget (0-1)", ""),
    ("Retention and win-back", "winback.monthly_rupees", "Win-back monthly budget", "₹"),
    ("Retention and win-back", "winback.per_customer_monthly_rupees", "Win-back per-customer monthly cap", "₹"),
    ("Retention and win-back", "winback.min_immediate_profit_rupees", "Win-back minimum immediate profit", "₹"),
]


def _get_path(data: dict, path: str):
    for key in path.split("."):
        data = data[key]
    return data


@app.get("/api/rules")
def rules() -> dict:
    conn = db()
    active = get_active_rules(conn)
    catalog = load_catalog(conn)
    return {
        "version": active.version, "created_at": active.created_at, "created_by": active.created_by,
        "note": active.note,
        "fields": [{"group": g, "path": p, "label": label, "unit": u, "value": _get_path(active.data, p)}
                   for g, p, label, u in EDITABLE_RULES],
        "category_margins": catalog.category_avg_margin_pct,
        "versions": list_versions(conn)[:20],
    }


class RulesIn(BaseModel):
    changes: dict[str, float | int | str]
    changed_by: str = "marketing team"
    note: str | None = None


@app.put("/api/rules")
def update_rules(body: RulesIn) -> dict:
    """Save edited rules as a new version (safety bounds checked), then re-check offers not sent yet."""
    conn = db()
    allowed = {p for _, p, _, _ in EDITABLE_RULES}
    data = get_active_rules(conn).data
    for path, value in body.changes.items():
        if path not in allowed:
            raise HTTPException(400, f"'{path}' can't be edited here")
        data = with_change(data, path, value)
    try:
        version, changes = save_rules(conn, data, body.changed_by, body.note, load_catalog(conn))
    except RulesValidationError as exc:
        raise HTTPException(400, {"problems": exc.problems})
    return {"version": version, "changes": changes, "rechecked": recheck_scheduled(conn) if changes else []}


# ---------------------------------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------------------------------
@app.get("/api/guardrails")
def guardrails() -> dict:
    """Everything the Guardrails page shows (read-only; see engine/guardrails.py)."""
    conn = db()
    return build_report(conn, get_active_rules(conn), now_utc())


@app.post("/api/followups/run")
def followups() -> list[dict]:
    return run_due_followups(db())


# ---------------------------------------------------------------------------------------------------
# Serve the built dashboard (if `npm run build` was done) at /
# ---------------------------------------------------------------------------------------------------
DIST = PROJECT_ROOT / "dashboard" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        target = DIST / full_path
        return FileResponse(target if full_path and target.is_file() else DIST / "index.html")
