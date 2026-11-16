"""Billing-cycle boundary math.

A cycle is a half-open range of local calendar dates, ``[start, end)``. Utilities report
cycles with an inclusive end date; :func:`from_utility_dates` converts.
"""

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

FINALIZE_GRACE_DAYS = 3
"""Days after a cycle ends before its record is frozen (utility data lags about 2 days)."""


@dataclass(frozen=True, slots=True, order=True)
class Cycle:
    """A billing cycle covering local dates ``start`` up to, not including, ``end``."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"cycle end {self.end} must be after start {self.start}")

    @property
    def days(self) -> int:
        """Number of calendar days in the cycle."""
        return (self.end - self.start).days

    @property
    def last_day(self) -> date:
        """The inclusive last day, as a utility would print it on the bill."""
        return self.end - timedelta(days=1)

    def contains(self, day: date) -> bool:
        """Whether ``day`` falls inside the cycle."""
        return self.start <= day < self.end

    def local_bounds(self, tz: tzinfo) -> tuple[datetime, datetime]:
        """Aware datetimes for local midnight at ``start`` and at ``end``."""
        return (
            datetime.combine(self.start, time.min, tzinfo=tz),
            datetime.combine(self.end, time.min, tzinfo=tz),
        )


def from_utility_dates(start_date: date, end_date: date) -> Cycle:
    """Build a cycle from a utility's start and inclusive end date."""
    return Cycle(start_date, end_date + timedelta(days=1))


def add_months(day: date, months: int) -> date:
    """Shift ``day`` by whole months, clamping to the last day of the target month."""
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def from_cycle_day(today: date, cycle_day: int) -> Cycle:
    """The cycle containing ``today`` when the meter is read on ``cycle_day`` each month."""
    if not 1 <= cycle_day <= 28:
        raise ValueError(f"cycle_day must be 1-28, got {cycle_day}")
    start = today.replace(day=cycle_day)
    if today.day < cycle_day:
        start = add_months(start, -1)
    return Cycle(start, add_months(start, 1))


def previous_cycles(current: Cycle, count: int) -> list[Cycle]:
    """Estimate ``count`` cycles before ``current``, newest first.

    Each boundary is ``current.start`` moved back by whole months. Every step is computed
    from the same anchor, so clamping at a short month never drifts later boundaries.
    """
    return [
        Cycle(add_months(current.start, -k), add_months(current.start, -(k - 1)))
        for k in range(1, count + 1)
    ]


def backfill(current: Cycle, max_count: int, earliest_data: date | None) -> list[Cycle]:
    """Previous cycles, newest first, that overlap recorded history.

    Stops at the first cycle that ends on or before ``earliest_data``, the first day with
    any statistics. With no history at all there is nothing to backfill.
    """
    if earliest_data is None:
        return []
    result = []
    for cycle in previous_cycles(current, max_count):
        if cycle.end <= earliest_data:
            break
        result.append(cycle)
    return result


def is_final(cycle: Cycle, today: date, grace_days: int = FINALIZE_GRACE_DAYS) -> bool:
    """Whether a closed cycle's record should stop being recomputed."""
    return today >= cycle.end + timedelta(days=grace_days)
