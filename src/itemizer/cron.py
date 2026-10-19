"""Standard 5-field cron expressions: minute hour day-of-month month day-of-week.

Supports `*`, numbers, ranges (`1-5`), lists (`1,15`), steps (`*/15`, `0-30/10`)
and month/weekday names (`jan`, `mon-fri`). Day-of-week 0 and 7 are both
Sunday. As in cron, when both day-of-month and day-of-week are restricted, a
day matching either one runs.
"""
from __future__ import annotations

from datetime import datetime, timedelta

MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
DAYS = "sun mon tue wed thu fri sat".split()
# (lowest, highest, names) per field
FIELDS = [(0, 59, None), (0, 23, None), (1, 31, None), (1, 12, MONTHS), (0, 7, DAYS)]
NAMES = ["minute", "hour", "day of month", "month", "day of week"]


def _value(text: str, lo: int, names: list[str] | None) -> int:
    if names and text.lower() in names:
        return names.index(text.lower()) + (1 if names is MONTHS else 0)
    if not text.isdigit():
        raise ValueError(f"'{text}' isn't a number")
    return int(text)


def _field(text: str, lo: int, hi: int, names: list[str] | None, label: str) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        part, _, step_text = part.partition("/")
        step = int(step_text) if step_text.isdigit() and int(step_text) > 0 else None
        if step_text and step is None:
            raise ValueError(f"bad step '/{step_text}' in the {label} field")
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _value(a, lo, names), _value(b, lo, names)
        else:
            start = end = _value(part, lo, names)
            if step:
                end = hi
        if not (lo <= start <= hi and lo <= end <= hi) or start > end:
            raise ValueError(f"'{part}' is out of range for the {label} field ({lo}-{hi})")
        values.update(range(start, end + 1, step or 1))
    return values


class Cron:
    def __init__(self, expression: str) -> None:
        parts = expression.split()
        if len(parts) != 5:
            raise ValueError(f"'{expression}' needs 5 fields (minute hour day-of-month month day-of-week)")
        try:
            sets = [_field(p, lo, hi, names, label) for p, (lo, hi, names), label in zip(parts, FIELDS, NAMES)]
        except ValueError as e:
            raise ValueError(f"'{expression}': {e}") from None
        self.expression = expression
        self.minutes, self.hours, self.days, self.months, weekdays = sets
        self.weekdays = {d % 7 for d in weekdays}
        self.any_day, self.any_weekday = parts[2] == "*", parts[4] == "*"

    def _day_matches(self, d: datetime) -> bool:
        if d.month not in self.months:
            return False
        dom, dow = d.day in self.days, (d.weekday() + 1) % 7 in self.weekdays
        if self.any_day or self.any_weekday:
            return dom and dow
        return dom or dow

    def next_after(self, now: datetime) -> datetime:
        """The first scheduled minute strictly after `now`."""
        start = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        day = start.replace(hour=0, minute=0)
        for _ in range(366 * 5):  # covers rare dates like Feb 29
            if self._day_matches(day):
                for h in sorted(self.hours):
                    for m in sorted(self.minutes):
                        at = day.replace(hour=h, minute=m)
                        if at >= start:
                            return at
            day += timedelta(days=1)
        raise ValueError(f"'{self.expression}' never runs")

    def __str__(self) -> str:
        return self.expression
