"""
Rules store: keeps every version of the business rules, and hands out the ACTIVE one.

Where it sits: rules.py, gatekeeper.py, fallback.py, the agent's `get_offer_limits` tool and the dashboard Rules
page all get limits from `get_active_rules()`. No other file may hard-code a limit (CLAUDE.md section 4.0).

How versions work:
  - First run: config/rules.yaml is copied into the `rules_versions` table as version 1 (active).
  - The marketing team edits a value on the dashboard -> `save_rules()` validates it, stores the full rule set as
    a NEW version, and makes it active. Old versions stay for the audit trail (who changed what, when).
  - Every decision records the version it was checked against.

Safety bounds: `validate_rules()` refuses edits that could make the company lose money, e.g. a margin floor
below 0%, or a category cap bigger than that category's average margin minus the margin floor.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from dataclasses import dataclass

import yaml

from config.settings import CONFIG_DIR
from engine.catalog import Catalog
from engine.clock import now_utc, to_ts


class RulesValidationError(ValueError):
    """Raised when an edit breaks a safety bound. `problems` lists every reason in plain words."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class ActiveRules:
    """The rule set currently in force, plus which version it is."""

    version: int
    data: dict
    created_at: str
    created_by: str
    note: str | None

    # Short-cuts so code reads like the rulebook: rules.profit["margin_floor_pct"]
    @property
    def profit(self) -> dict:
        return self.data["profit"]

    @property
    def budget(self) -> dict:
        return self.data["budget"]

    @property
    def anti_spam(self) -> dict:
        return self.data["anti_spam"]

    @property
    def integrity(self) -> dict:
        return self.data["integrity"]

    @property
    def fairness(self) -> dict:
        return self.data["fairness"]

    @property
    def message(self) -> dict:
        return self.data["message"]

    @property
    def habit(self) -> dict:
        return self.data["habit"]

    @property
    def retention(self) -> dict:
        return self.data["retention"]

    @property
    def winback(self) -> dict:
        return self.data["winback"]


def with_defaults(data: dict) -> dict:
    """
    Fill in rule sections/keys that didn't exist when an older version was saved (e.g. 'habit', 'retention',
    'winback' were added in v2). The saved version is not changed; new rules start at their default values
    until marketing edits them.
    """
    merged = json.loads(json.dumps(data))
    for section, values in load_default_rules().items():
        if section not in merged:
            merged[section] = values
        elif isinstance(values, dict) and isinstance(merged[section], dict):
            for key, value in values.items():
                merged[section].setdefault(key, value)
    return merged


def load_default_rules() -> dict:
    """The starting rule values from config/rules.yaml."""
    with open(CONFIG_DIR / "rules.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def ensure_rules(conn: sqlite3.Connection) -> None:
    """Create version 1 from config/rules.yaml if the database has no rules yet."""
    if conn.execute("SELECT 1 FROM rules_versions LIMIT 1").fetchone():
        return
    conn.execute(
        "INSERT INTO rules_versions (version, created_at, created_by, note, rules_json, is_active) "
        "VALUES (1, ?, 'system', 'Default rules from config/rules.yaml', ?, 1)",
        (to_ts(now_utc()), json.dumps(load_default_rules())))
    conn.commit()


def get_active_rules(conn: sqlite3.Connection) -> ActiveRules:
    """The ONE way to read limits. Always returns the newest saved version."""
    ensure_rules(conn)
    row = conn.execute("SELECT * FROM rules_versions WHERE is_active = 1 ORDER BY version DESC LIMIT 1").fetchone()
    return ActiveRules(row["version"], with_defaults(json.loads(row["rules_json"])), row["created_at"],
                       row["created_by"], row["note"])


def list_versions(conn: sqlite3.Connection) -> list[dict]:
    """All versions, newest first, with what changed compared to the version before (for the audit trail)."""
    ensure_rules(conn)
    rows = conn.execute("SELECT * FROM rules_versions ORDER BY version").fetchall()
    out, previous = [], None
    for r in rows:
        data = json.loads(r["rules_json"])
        out.append({"version": r["version"], "created_at": r["created_at"], "created_by": r["created_by"],
                    "note": r["note"], "is_active": bool(r["is_active"]),
                    "changes": diff_rules(previous, data) if previous else []})
        previous = data
    return out[::-1]


def diff_rules(old: dict, new: dict, path: str = "") -> list[dict]:
    """List every value that differs: [{'path': 'profit.category_cap_pct.electronics', 'old': 8, 'new': 12}]."""
    changes = []
    for key in sorted(set(old) | set(new)):
        p = f"{path}.{key}" if path else key
        a, b = old.get(key), new.get(key)
        if isinstance(a, dict) and isinstance(b, dict):
            changes.extend(diff_rules(a, b, p))
        elif a != b:
            changes.append({"path": p, "old": a, "new": b})
    return changes


def validate_rules(rules: dict, catalog: Catalog) -> list[str]:
    """
    Check a proposed rule set against the safety bounds. Returns a list of problems (empty = safe).
    These bounds exist so that no edit on the Rules page can make offers lose money.
    """
    problems: list[str] = []
    defaults = load_default_rules()

    # Same structure as the defaults: no sections removed or invented
    for section in ("profit", "budget", "anti_spam", "integrity", "fairness", "message", "habit", "retention",
                    "winback"):
        if not isinstance(rules.get(section), dict):
            problems.append(f"Section '{section}' is missing.")
    if problems:
        return problems

    profit, budget, spam, integrity = rules["profit"], rules["budget"], rules["anti_spam"], rules["integrity"]

    def number(value, name: str, low: float | None = None, high: float | None = None) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            problems.append(f"{name} must be a number.")
            return
        if low is not None and value < low:
            problems.append(f"{name} can't be below {low:g} (got {value:g}).")
        if high is not None and value > high:
            problems.append(f"{name} can't be above {high:g} (got {value:g}).")

    floor = profit.get("margin_floor_pct")
    number(floor, "Margin floor %", 0, 50)
    number(profit.get("min_expected_profit_rupees"), "Minimum expected profit (Rs)", 0)
    number(profit.get("buys_anyway_threshold"), "Buys-anyway threshold", 0, 1)
    number(profit.get("cashback_max_rupees"), "Max cashback (Rs)", 0)
    number(profit.get("points_multiplier_max"), "Max points multiplier", 1, 10)
    number(profit.get("free_delivery_min_order_rupees"), "Free delivery minimum order (Rs)", 0)

    caps = profit.get("category_cap_pct") or {}
    if set(caps) != set(catalog.categories):
        problems.append(f"Category caps must cover exactly these categories: {', '.join(catalog.categories)}.")
    if isinstance(floor, (int, float)):
        for category, cap in caps.items():
            if category not in catalog.category_avg_margin_pct:
                continue
            limit = round(catalog.category_avg_margin_pct[category] - floor, 1)
            number(cap, f"{category.capitalize()} cap %", 0)
            if isinstance(cap, (int, float)) and cap > limit:
                problems.append(
                    f"{category.capitalize()} cap {cap:g}% is too high: average {category} margin is "
                    f"{catalog.category_avg_margin_pct[category]:.1f}% and the margin floor is {floor:g}%, "
                    f"so the cap can be at most {limit:g}%.")

    for key in ("daily_rupees", "monthly_rupees", "per_customer_monthly_rupees"):
        number(budget.get(key), f"Budget '{key}'", 0)
    number(spam.get("max_offers_per_7_days"), "Max offers per 7 days", 0, 7)
    number(spam.get("min_hours_between_offers"), "Min hours between offers", 0, 168)
    for key in ("quiet_hours_start", "quiet_hours_end"):
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(spam.get(key, ""))):
            problems.append(f"{key} must look like HH:MM (e.g. 22:00).")
    number(integrity.get("min_expiry_hours"), "Min expiry hours", 1, 168)
    number(integrity.get("max_expiry_hours"), "Max expiry hours", 1, 168)
    if (isinstance(integrity.get("min_expiry_hours"), (int, float)) and isinstance(integrity.get("max_expiry_hours"),
                                                                                   (int, float))
            and integrity["min_expiry_hours"] > integrity["max_expiry_hours"]):
        problems.append("Min expiry hours can't be bigger than max expiry hours.")
    number(rules["message"].get("max_chars"), "Max message length", 50, 1000)

    # v2: habit protection, retention value, win-back budget
    habit, retention, winback = rules["habit"], rules["retention"], rules["winback"]
    number(habit.get("full_price_threshold"), "Habit protection: full-price threshold", 0.3, 1)
    if habit.get("share_window", "12m") not in ("12m", "90d"):
        problems.append("Habit protection window must be 12m or 90d.")
    number(retention.get("churn_avoidance_share"), "Retention: churn-avoidance share", 0, 0.8)
    number(retention.get("horizon_weeks"), "Retention: horizon (weeks)", 1, 26)
    number(retention.get("min_overdue_ratio"), "Retention: minimum overdue ratio", 1, 10)
    number(retention.get("min_orders"), "Retention: minimum orders", 1, 100)
    number(retention.get("max_future_orders"), "Retention: max future orders", 1, 52)
    number(winback.get("share_of_daily_budget"), "Win-back: share of daily budget", 0, 0.6)
    number(winback.get("monthly_rupees"), "Win-back: monthly budget (Rs)", 0)
    number(winback.get("per_customer_monthly_rupees"), "Win-back: per-customer monthly cap (Rs)", 0)
    number(winback.get("min_immediate_profit_rupees"), "Win-back: minimum immediate profit (Rs)", 0)
    if isinstance(winback.get("monthly_rupees"), (int, float)) and isinstance(budget.get("monthly_rupees"), (int, float)) \
            and winback["monthly_rupees"] > budget["monthly_rupees"]:
        problems.append("Win-back monthly budget can't be bigger than the whole monthly budget.")

    # Never allow the safety switches to be turned off from the Rules page
    if not spam.get("respect_marketing_consent", True):
        problems.append("Marketing consent must always be respected.")
    if not rules["message"].get("must_match_offer", True):
        problems.append("Messages must always match the real offer.")

    unknown = set(rules) - set(defaults)
    if unknown:
        problems.append(f"Unknown sections: {', '.join(sorted(unknown))}.")
    return problems


def save_rules(conn: sqlite3.Connection, new_rules: dict, changed_by: str, note: str | None,
               catalog: Catalog) -> tuple[int, list[dict]]:
    """
    Validate and store a new rule set as the next version, and make it active.
    Returns (new version number, list of changes). Raises RulesValidationError if a safety bound is broken.
    The caller should then re-check offers that are decided but not yet sent (decide.recheck_scheduled).
    """
    problems = validate_rules(new_rules, catalog)
    if problems:
        raise RulesValidationError(problems)
    current = get_active_rules(conn)
    changes = diff_rules(current.data, new_rules)
    if not changes:
        return current.version, []
    version = current.version + 1
    conn.execute("UPDATE rules_versions SET is_active = 0")
    conn.execute(
        "INSERT INTO rules_versions (version, created_at, created_by, note, rules_json, is_active) "
        "VALUES (?, ?, ?, ?, ?, 1)", (version, to_ts(now_utc()), changed_by, note, json.dumps(new_rules)))
    conn.commit()
    return version, changes


def with_change(rules: dict, path: str, value) -> dict:
    """Copy of `rules` with one value changed, e.g. with_change(r, 'profit.category_cap_pct.electronics', 12)."""
    updated = copy.deepcopy(rules)
    node = updated
    *parents, last = path.split(".")
    for key in parents:
        node = node[key]
    node[last] = value
    return updated
