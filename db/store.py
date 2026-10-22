"""
Database helper: opening the SQLite file, creating tables, and bulk inserts.

Where it sits: the bottom layer. data/generate.py writes the company data through it; later the
engine, bot and API read and write through it. Every other file gets a connection from `connect()`
instead of calling sqlite3 itself, so settings (row access by name, foreign keys) are the same everywhere.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path

from config.settings import settings

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """
    Open the database and return a connection.

    Rows come back as sqlite3.Row, so code can write row["first_name"] instead of row[1]; that keeps
    queries readable. Foreign keys are switched on so broken links (e.g. an order for a customer that
    doesn't exist) fail loudly instead of silently.
    """
    path = Path(db_path or settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes from db/schema.sql (safe to run again; uses IF NOT EXISTS)."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def insert_many(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence],
) -> None:
    """
    Insert many rows in one go.

    The data generator creates hundreds of thousands of rows; inserting them one by one would take
    minutes. executemany inside one transaction takes seconds.
    """
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    conn.executemany(sql, rows)
