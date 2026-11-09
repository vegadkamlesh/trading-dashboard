"""NSE F&O weekday trading holidays used by the start gate and health monitor.

Source: NSE circular NSE/FAOP/71777 (Dec 12, 2025) and the NSE equities holiday page.
Add the next year's list as soon as NSE publishes it (normally in December); the
start gate sends a Telegram warning on every start while the current year is missing.
"""

from datetime import date

NSE_TRADING_HOLIDAYS: dict[int, frozenset[date]] = {
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


def holiday_list_available(day: date) -> bool:
    return day.year in NSE_TRADING_HOLIDAYS


def is_trading_holiday(day: date) -> bool:
    return day in NSE_TRADING_HOLIDAYS.get(day.year, frozenset())
