"""
Autopilot: plan the day's offers per category bot, and run each one automatically at its time.

Where it sits: on top of the normal pipeline. api/main.py runs `run_due()` every 30 seconds (the API process, not
the bot, because a decision can call the AI). Each slot runs decide() exactly like the dashboard's "Make offer":
gatekeeper -> AI (within today's Day Plan; free rules beyond it) -> rules checker -> queued -> the category bot sends.

Planning (plan_day):
  - candidates come from `customer_groups` (engine/groups.py): marketing consent, a live trigger, no offer decided
    for them in the last 7 days, not in a learning holdout;
  - each category bot gets customers whose favourite category it is (Rewards gets points-expiring / idle-wallet
    customers), mixed across groups so the day shows variety, strongest reason first;
  - each slot is set at the hour the customer usually shops (their own sessions), inside day_start..day_end,
    otherwise spread evenly over the rest of the day.
Settings: config/autopilot.yaml. The AI may still decide to send nothing; that's shown, not hidden.
"""

from __future__ import annotations

import random
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, time, timedelta
from functools import lru_cache

import yaml

from config.settings import CONFIG_DIR
from engine.clock import IST, ist_date, now_utc, parse_ts, to_ist, to_ts

BOT_KEYS = ["electronics", "fashion", "beauty", "home", "grocery", "rewards"]
REWARD_TRIGGERS = ("points_expiring", "wallet_idle")

SCHEMA = """
CREATE TABLE IF NOT EXISTS autopilot_slots (
    id          INTEGER PRIMARY KEY,
    day         TEXT NOT NULL,             -- IST date the slot belongs to
    bot         TEXT NOT NULL,             -- category bot that planned it
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    slot_at     TEXT NOT NULL,             -- UTC time it runs
    reason      TEXT,                      -- trigger + group, in words
    status      TEXT NOT NULL,             -- 'planned' / 'done' / 'skipped'
    decision_id INTEGER REFERENCES decisions(id),
    ran_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_autopilot_day ON autopilot_slots(day, slot_at);
"""


@lru_cache(maxsize=1)
def settings() -> dict:
    return yaml.safe_load((CONFIG_DIR / "autopilot.yaml").read_text(encoding="utf-8"))


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


def is_enabled(conn: sqlite3.Connection) -> bool:
    """The dashboard's Pause/Resume switch (meta 'autopilot'), defaulting to config enabled."""
    ensure_schema(conn)
    row = conn.execute("SELECT value FROM meta WHERE key = 'autopilot'").fetchone()
    return (row[0] == "on") if row else bool(settings().get("enabled", True))


def set_enabled(conn: sqlite3.Connection, on: bool) -> None:
    ensure_schema(conn)
    conn.execute("INSERT INTO meta (key, value) VALUES ('autopilot', ?) ON CONFLICT(key) DO UPDATE SET "
                 "value = excluded.value", ("on" if on else "off",))
    conn.commit()


def _clock(text: str) -> time:
    return time.fromisoformat(text)


def _window(now: datetime) -> tuple[str, datetime, datetime]:
    """(IST day, first send, last send) for planning: the rest of today, or tomorrow if today is over."""
    cfg = settings()
    local = to_ist(now)
    start = datetime.combine(local.date(), _clock(cfg["day_start"]), tzinfo=IST)
    end = datetime.combine(local.date(), _clock(cfg["day_end"]), tzinfo=IST)
    soonest = local + timedelta(minutes=3)
    if soonest >= end - timedelta(minutes=30):   # today is (nearly) over: plan tomorrow
        start, end = start + timedelta(days=1), end + timedelta(days=1)
    else:
        start = max(start, soonest)
    return start.date().isoformat(), start, end


def plan_day(conn: sqlite3.Connection, now: datetime | None = None, force: bool = False) -> int:
    """Create the day's slots (if not already planned). Returns how many slots were created."""
    ensure_schema(conn)
    now = now or now_utc()
    day, start, end = _window(now)
    if not force and conn.execute("SELECT 1 FROM autopilot_slots WHERE day = ? LIMIT 1", (day,)).fetchone():
        return 0
    if force:
        conn.execute("DELETE FROM autopilot_slots WHERE day = ? AND status = 'planned'", (day,))
    per_bot = settings()["offers_per_bot_per_day"]
    week_ago = to_ts(now - timedelta(days=7))
    busy = {r[0] for r in conn.execute("SELECT DISTINCT customer_id FROM decisions WHERE created_at >= ?", (week_ago,))}
    busy |= {r[0] for r in conn.execute("SELECT customer_id FROM autopilot_slots WHERE day = ?", (day,))}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'holdout_assignments'").fetchone():
        busy |= {r[0] for r in conn.execute("SELECT customer_id FROM holdout_assignments WHERE grp = 'holdout'")}
    candidates = [dict(r) for r in conn.execute(
        """SELECT customer_id, group_key, group_label, favourite_category, usual_hour, trigger_type, trigger_strength
           FROM customer_groups WHERE marketing_consent = 1 AND trigger_type IS NOT NULL
           ORDER BY trigger_strength DESC, customer_id""") if r["customer_id"] not in busy]

    used: set[int] = set()
    rows = []
    for bot in BOT_KEYS:
        if bot == "rewards":
            pool = [c for c in candidates if c["trigger_type"] in REWARD_TRIGGERS]
        else:
            pool = [c for c in candidates if c["favourite_category"] == bot and c["trigger_type"] not in REWARD_TRIGGERS]
        picks = _varied(pool, per_bot, used)
        used |= {c["customer_id"] for c in picks}
        for i, c in enumerate(picks):
            rows.append((day, bot, c["customer_id"], to_ts(_slot_time(c, i, len(picks), start, end)),
                         f"{c['trigger_type'].replace('_', ' ')} · {c['group_label']}", "planned"))
    conn.executemany("INSERT INTO autopilot_slots (day, bot, customer_id, slot_at, reason, status) "
                     "VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    return len(rows)


def _varied(pool: list[dict], n: int, used: set[int]) -> list[dict]:
    """Up to n customers, taking one from each group in turn (strongest reason first), so the day shows variety."""
    by_group: dict[str, list[dict]] = defaultdict(list)
    for c in pool:
        if c["customer_id"] not in used:
            by_group[c["group_key"]].append(c)
    picks: list[dict] = []
    while len(picks) < n and any(by_group.values()):
        for items in list(by_group.values()):
            if items and len(picks) < n:
                picks.append(items.pop(0))
    return picks


def _slot_time(c: dict, i: int, n: int, start: datetime, end: datetime) -> datetime:
    """At the customer's usual shopping hour if it falls in the window, else spread evenly across the window."""
    rng = random.Random(c["customer_id"])
    hour = c.get("usual_hour")
    if hour is not None:
        at = start.replace(hour=hour, minute=rng.randint(0, 50), second=0, microsecond=0)
        if start <= at <= end:
            return at.astimezone(start.tzinfo)
    span = (end - start).total_seconds()
    return start + timedelta(seconds=span * (i + 0.5) / max(n, 1) + rng.randint(-600, 600))


_run_lock = threading.Lock()   # one autopilot run at a time inside a process (loop + button)


def run_due(conn: sqlite3.Connection, now: datetime | None = None, limit: int = 3, agent_client=None) -> list[dict]:
    """Run up to `limit` due slots: one normal decision each. Returns what happened per slot."""
    with _run_lock:
        return _run_due(conn, now, limit, agent_client)


def _run_due(conn: sqlite3.Connection, now: datetime | None, limit: int, agent_client) -> list[dict]:
    ensure_schema(conn)
    now = now or now_utc()
    if not is_enabled(conn):
        return []
    from engine.decide import decide             # local imports: decide pulls in the agent
    from engine.catalog import load_catalog
    from engine.features import build_profile
    from engine.triggers import detect_triggers
    cfg = settings()
    due = conn.execute("SELECT * FROM autopilot_slots WHERE status = 'planned' AND slot_at <= ? ORDER BY slot_at "
                       "LIMIT ?", (to_ts(now), limit)).fetchall()
    catalog = load_catalog(conn) if due else None
    report = []
    for slot in due:
        # Claim the slot first: only the process that flips it from 'planned' to 'running' may run it, so the
        # background loop and the "Run next round now" button can never decide for the same customer twice.
        claimed = conn.execute("UPDATE autopilot_slots SET status = 'running' WHERE id = ? AND status = 'planned'",
                               (slot["id"],))
        conn.commit()
        if claimed.rowcount != 1:
            continue
        found = detect_triggers(build_profile(conn, slot["customer_id"], now, catalog))
        trigger, detail = (found[0].type, found[0].detail) if found else ("manual", None)
        out = decide(conn, slot["customer_id"], trigger, detail, now=now, use_agent=cfg.get("use_ai", True),
                     catalog=catalog, agent_client=agent_client)
        status = "skipped" if out.decided_by == "gatekeeper" else "done"
        conn.execute("UPDATE autopilot_slots SET status = ?, decision_id = ?, ran_at = ? WHERE id = ?",
                     (status, out.decision_id, to_ts(now), slot["id"]))
        conn.commit()
        report.append({"slot": slot["id"], "customer_id": slot["customer_id"], "decision_id": out.decision_id,
                       "status": out.status})
    return report


def run_next_now(conn: sqlite3.Connection, now: datetime | None = None, per_bot: int = 1) -> list[dict]:
    """Demo button: bring the next planned slot of every bot forward to now and run them."""
    ensure_schema(conn)
    now = now or now_utc()
    plan_day(conn, now)
    ids = []
    for bot in BOT_KEYS:
        ids += [r[0] for r in conn.execute("SELECT id FROM autopilot_slots WHERE bot = ? AND status = 'planned' "
                                           "ORDER BY slot_at LIMIT ?", (bot, per_bot))]
    if ids:
        conn.execute(f"UPDATE autopilot_slots SET slot_at = ? WHERE id IN ({','.join('?' * len(ids))})",
                     (to_ts(now), *ids))
        conn.commit()
    return run_due(conn, now, limit=len(ids))


def today_view(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Everything the Autopilot page shows: per-bot cards and the timeline of today's slots."""
    ensure_schema(conn)
    now = now or now_utc()
    day = ist_date(now)
    rows = [dict(r) for r in conn.execute("""
        SELECT s.id, s.bot, s.customer_id, s.slot_at, s.reason, s.status AS slot_status, s.decision_id,
               c.first_name, g.group_label, d.status, d.message, d.final_offer, d.reasoning, d.expected_profit_paise,
               d.offer_cost_paise, d.decided_by, d.offer_kind, d.sent_at, d.sent_via
        FROM autopilot_slots s JOIN customers c ON c.id = s.customer_id
        LEFT JOIN customer_groups g ON g.customer_id = s.customer_id
        LEFT JOIN decisions d ON d.id = s.decision_id
        WHERE s.day = ? ORDER BY s.slot_at""", (day,))]
    return {"day": day, "enabled": is_enabled(conn), "rows": rows}
