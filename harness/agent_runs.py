"""
Shared plumbing for the harness checks that use the REAL AI (eval_agent_scenarios.py, simulate_profit.py).

  - temp_database(): a throw-away copy of the real database, so these checks never change your data
  - pick_sample():   N customers per hidden persona (the harness may read customer_traits; the agent never does)
  - decision_context(): profile, rule state and strongest trigger for a customer at a moment (same as the pipeline)
  - recorded_agent(): runs the real agent ONCE and saves its answer in harness/recordings/ (git-ignored); any re-run
                      with the same customer, moment, trigger, model, prompt and tools reuses the saved answer for free
                      (CLAUDE.md decision 1: "save the agent's answers so re-running them later is free")
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from config.settings import PROJECT_ROOT, settings
from db.store import connect
from engine.agent import SYSTEM_PROMPT, AgentContext, build_tools, make_client, run_agent
from engine.catalog import Catalog
from engine.clock import parse_ts, to_ts
from engine.features import build_profile
from engine.models import OfferProposal
from engine.rules import load_rule_state
from engine.rules_store import ActiveRules
from engine.triggers import detect_triggers

RECORDINGS = PROJECT_ROOT / "harness" / "recordings"
# A fixed moment for repeatable runs: the day after the data ends, at noon IST (outside quiet hours)
DEFAULT_NOW = parse_ts("2026-10-22T06:30:00Z")
PERSONAS = ["loyal_regular", "deal_hunter", "vip_at_risk", "window_shopper", "new_user", "cart_abandoner",
            "points_hoarder", "festival_only"]


def temp_database() -> sqlite3.Connection:
    """A private copy of the real database (decisions made here never touch the real one)."""
    target = Path(tempfile.mkdtemp(prefix="offerpilot_eval_")) / "eval.db"
    shutil.copyfile(settings.db_path, target)
    return connect(target)


def pick_sample(conn: sqlite3.Connection, per_persona: int, seed: int = 7) -> dict[str, list[int]]:
    """per_persona customers for each hidden persona, chosen deterministically among those who allow marketing."""
    out = {}
    for persona in PERSONAS:
        ids = [r[0] for r in conn.execute("""SELECT t.customer_id FROM customer_traits t JOIN customers c
                                             ON c.id = t.customer_id WHERE t.persona = ? AND c.marketing_consent = 1
                                             ORDER BY t.customer_id""", (persona,))]
        out[persona] = random.Random(f"{seed}-{persona}").sample(ids, per_persona)
    return out


def decision_context(conn, catalog: Catalog, rules: ActiveRules, customer_id: int, now: datetime):
    """What the pipeline would hand the agent: profile, rule state, strongest trigger (or manual)."""
    profile = build_profile(conn, customer_id, now, catalog)
    state = load_rule_state(conn, customer_id, rules, now)
    found = detect_triggers(profile)
    trigger, detail = (found[0].type, found[0].detail) if found else ("manual", None)
    return profile, state, trigger, detail


def _key(customer_id: int, now: datetime, trigger: str, detail, label: str, catalog: Catalog) -> str:
    stuff = json.dumps([customer_id, to_ts(now), trigger, detail, label, settings.claude_model, SYSTEM_PROMPT,
                        build_tools(catalog)], sort_keys=True, default=str)
    return hashlib.sha1(stuff.encode()).hexdigest()[:16]


def recorded_agent(conn, catalog: Catalog, rules: ActiveRules, customer_id: int, now: datetime,
                   label: str = "", use_cache: bool = True) -> dict:
    """
    One real agent decision (dry run: nothing is saved to the decisions table or sent), cached on disk.
    Returns a plain dict: status, offer (dict or None), reasoning, cost_usd, error, wait_hours, trigger, cached.
    """
    profile, state, trigger, detail = decision_context(conn, catalog, rules, customer_id, now)
    key = _key(customer_id, now, trigger, detail, label, catalog)
    path = RECORDINGS / f"{key}.json"
    if use_cache and path.exists():
        return {**json.loads(path.read_text(encoding="utf-8")), "cached": True}
    out = run_agent(AgentContext(conn, profile, catalog, rules, state, now, trigger, detail), client=make_client())
    record = {"customer_id": customer_id, "now": to_ts(now), "trigger": trigger, "label": label,
              "status": out.status, "offer": out.check.offer.model_dump(mode="json") if out.check else None,
              "reasoning": out.reasoning, "cost_usd": out.cost_usd, "error": out.error, "wait_hours": out.wait_hours,
              "api_calls": out.api_calls}
    RECORDINGS.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
    return {**record, "cached": False}


def offer_from_record(record: dict) -> OfferProposal | None:
    return OfferProposal.model_validate(record["offer"]) if record.get("offer") else None
