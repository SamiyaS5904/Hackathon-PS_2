"""
Shopping calendar: festivals/sales, paydays and weekends.

Where it sits: used by data/generate.py (to make buying spike on the right days and to fill the
`calendar` table) and later by the analyst and simulator ("is today a payday?").

Key idea: real Indian e-commerce has strong seasonal spikes. Deal hunters and festival-only buyers
almost only buy during sales; almost everyone spends a bit more right after payday and on weekends.

Festival dates are fixed approximations that repeat every year (real dates like Diwali move a little
each year). That is fine for synthetic data.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta

# (name, start month, start day, length in days)
FESTIVAL_SALES: list[tuple[str, int, int, int]] = [
    ("Republic Day Sale", 1, 17, 4),
    ("Valentine's Week", 2, 7, 8),
    ("Holi Sale", 3, 1, 4),
    ("Summer Sale", 5, 10, 5),
    ("End of Season Sale", 6, 12, 9),
    ("Independence Day Sale", 8, 8, 8),
    ("Big Billion Days", 9, 23, 10),
    ("Diwali Sale", 10, 12, 9),
    ("Black Friday Sale", 11, 27, 4),
    ("Year End Sale", 12, 20, 12),
]


def festival_name_on(day: date) -> str | None:
    """Return the sale running on this date, or None."""
    for name, month, start_day, length in FESTIVAL_SALES:
        start = date(day.year, month, start_day)
        if start <= day < start + timedelta(days=length):
            return name
    return None


def is_payday(day: date) -> bool:
    """
    Salary days: the last day of the month and the first 3 days of the next.
    Spending goes up right after people get paid.
    """
    last_day = monthrange(day.year, day.month)[1]
    return day.day <= 3 or day.day == last_day


def is_weekend(day: date) -> bool:
    return day.weekday() >= 5  # Saturday = 5, Sunday = 6


def build_calendar(start: date, end: date) -> list[tuple]:
    """
    One row per date from start to end (inclusive), ready to insert into the `calendar` table:
    (date, is_weekend, is_payday, is_festival, festival_name)
    """
    rows = []
    day = start
    while day <= end:
        festival = festival_name_on(day)
        rows.append(
            (day.isoformat(), int(is_weekend(day)), int(is_payday(day)), int(festival is not None), festival)
        )
        day += timedelta(days=1)
    return rows
