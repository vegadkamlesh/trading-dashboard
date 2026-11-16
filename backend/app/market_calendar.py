"""NSE F&O trading days for the live algos.

Copy of deployment/aws/market_holidays.py (the start gate's list); a test keeps the two equal.
Add the next year's NSE list to both files as soon as NSE publishes it.
"""

from datetime import date, timedelta
from typing import Dict, FrozenSet

NSE_TRADING_HOLIDAYS: Dict[int, FrozenSet[date]] = {
    2026: frozenset(
        {
            date(2026, 1, 15),
            date(2026, 1, 26),
            date(2026, 3, 3),
            date(2026, 3, 26),
            date(2026, 3, 31),
            date(2026, 4, 3),
            date(2026, 4, 14),
            date(2026, 5, 1),
            date(2026, 5, 28),
            date(2026, 6, 26),
            date(2026, 9, 14),
            date(2026, 10, 2),
            date(2026, 10, 20),
            date(2026, 11, 10),
            date(2026, 11, 24),
            date(2026, 12, 25),
        }
    ),
}


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in NSE_TRADING_HOLIDAYS.get(day.year, frozenset())


def next_trading_day(day: date) -> date:
    nxt = day + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt
