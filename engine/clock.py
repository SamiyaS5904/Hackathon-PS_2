"""
Time helpers: one place that knows "what time is it" and how to convert between UTC and IST.

Where it sits: used by every engine module, the bot and the API.

Key ideas:
  - The database stores every timestamp in UTC ("2026-10-22T09:30:00Z"); people see IST.
  - `now_utc()` normally returns the real current time. For demos and tests you can freeze the clock by
    setting OFFERPILOT_NOW in .env (e.g. OFFERPILOT_NOW=2026-10-22T12:00:00Z), so results are repeatable.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now_utc() -> datetime:
    """Current time in UTC (timezone-aware). Frozen if OFFERPILOT_NOW is set."""
    frozen = os.getenv("OFFERPILOT_NOW", "").strip()
    if frozen:
        return parse_ts(frozen)
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_ts(value: str) -> datetime:
    """Database timestamp string -> timezone-aware UTC datetime."""
    return datetime.strptime(value, TS_FORMAT).replace(tzinfo=timezone.utc)


def to_ts(moment: datetime) -> str:
    """Timezone-aware datetime -> database timestamp string (UTC)."""
    return moment.astimezone(timezone.utc).strftime(TS_FORMAT)


def to_ist(moment: datetime) -> datetime:
    return moment.astimezone(IST)


def ist_date(moment: datetime) -> str:
    """The calendar date in India for this moment, as YYYY-MM-DD (used for daily budgets and Day Plans)."""
    return to_ist(moment).date().isoformat()


def start_of_ist_day(moment: datetime) -> datetime:
    """Midnight IST of the day this moment falls on, returned in UTC (used to count 'today' in queries)."""
    local = to_ist(moment)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def days_between(earlier: datetime, later: datetime) -> float:
    return (later - earlier).total_seconds() / 86400


def friendly_ist(moment: datetime) -> str:
    """Human-friendly IST time for messages: '23 Oct, 9:00 PM'."""
    local = to_ist(moment)
    return local.strftime("%d %b, %I:%M %p").lstrip("0").replace(" 0", " ")
