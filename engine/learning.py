"""
Learning mode: how OfferPilot behaves on a real shop's data before it has seen any past campaigns.

Where it sits: read by engine/features.py (profile flags), engine/profit.py (uplift) and engine/gatekeeper.py
(holdout). Written by data/import_csv.py when a brand's export is loaded.

Why: the uplift model learns "who buys BECAUSE of an offer" from past campaigns with a random holdout group. A new
shop's export usually has no such data, and our model (trained on synthetic data) must never judge a real shop. So:
  - the uplift model is switched off and the hand-made formula is scaled down by a conservative factor
    (meta 'conservative_uplift_factor', e.g. 0.7): when in doubt, expect less from an offer, so fewer offers pass;
  - a random share of eligible customers (e.g. 10%) is kept in a HOLDOUT that never receives offers. Comparing them
    with everyone else later is what lets the shop train its own uplift model (python -m engine.uplift --train).
The settings live in the database's `meta` table, so each database carries its own mode.
"""

from __future__ import annotations

import sqlite3

_cache: dict[str, dict] = {}


def _db_file(conn: sqlite3.Connection) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2]


def shop_mode(conn: sqlite3.Connection) -> dict:
    """{'learning_mode': bool, 'conservative_uplift_factor': float, 'source': str}, cached per database file."""
    key = _db_file(conn)
    if key not in _cache:
        rows = dict(conn.execute("SELECT key, value FROM meta").fetchall()) if _has_table(conn, "meta") else {}
        _cache[key] = {"learning_mode": rows.get("learning_mode") == "1",
                       "conservative_uplift_factor": float(rows.get("conservative_uplift_factor", 1.0)),
                       "source": rows.get("source", "synthetic")}
    return _cache[key]


def forget_mode(conn: sqlite3.Connection) -> None:
    """Drop the cached mode (after an import writes new settings)."""
    _cache.pop(_db_file(conn), None)


def in_holdout(conn: sqlite3.Connection, customer_id: int) -> bool:
    """True if this customer was kept in the learning holdout (never gets offers)."""
    if not _has_table(conn, "holdout_assignments"):
        return False
    row = conn.execute("SELECT grp FROM holdout_assignments WHERE customer_id = ?", (customer_id,)).fetchone()
    return bool(row and row[0] == "holdout")


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None
