"""Past bill periods reported by the utility, and how they replace estimated history.

See docs/specs/opower-bills.md §2-3.
"""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

from .cycles import Cycle
from .record import BilledAmounts, CycleRecord, Source


@dataclass(frozen=True, slots=True)
class UtilityBill:
    """One bill period. ``end`` is exclusive."""

    start: date
    end: date
    kwh: float | None
    cost: float | None

    @property
    def cycle(self) -> Cycle:
        """The bill's period as a cycle."""
        return Cycle(self.start, self.end)

    @property
    def amounts(self) -> BilledAmounts:
        """What the utility billed for the period."""
        return BilledAmounts(kwh=self.kwh, cost=self.cost)


def _exclusive_end(end: datetime) -> date:
    return end.date() if end.time() == time.min else end.date() + timedelta(days=1)


def bills_from_reads(
    reads: Iterable[tuple[datetime, datetime, float | None, float | None]],
) -> list[UtilityBill]:
    """Bill periods, oldest first, from ``(start, end, kwh, cost)`` with aware datetimes.

    Times keep their own time zone: bill dates are the utility's local dates.
    """
    by_start: dict[date, UtilityBill] = {}
    for start_time, end_time, kwh, cost in reads:
        start, end = start_time.date(), _exclusive_end(end_time)
        if end > start and start not in by_start:
            by_start[start] = UtilityBill(start, end, kwh, cost)
    bills = sorted(by_start.values(), key=lambda b: b.start)
    # Bills are contiguous; a one-day gap is a last day reported at midnight.
    for index in range(len(bills) - 1):
        bill, following = bills[index], bills[index + 1]
        if following.start - bill.end == timedelta(days=1):
            bills[index] = replace(bill, end=following.start)
    return bills


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Stored records to keep (some updated) and bill periods still to compute."""

    keep: list[CycleRecord]
    compute: list[UtilityBill]


def _overlaps(a: Cycle | CycleRecord | UtilityBill, b: Cycle | CycleRecord | UtilityBill) -> bool:
    return a.start < b.end and b.start < a.end


def merge_bills(
    records: list[CycleRecord],
    bills: list[UtilityBill],
    current_start: date,
    max_count: int,
) -> MergeResult:
    """Replace estimated and stale closed records with the utility's bill periods."""
    ended = [b for b in bills if b.end <= current_start]
    ended = sorted(ended, key=lambda b: b.start)[-max_count:] if max_count > 0 else []
    user = [r for r in records if r.source is Source.USER]
    used = [b for b in ended if not any(_overlaps(b, r) for r in user)]

    keep: list[CycleRecord] = list(user)
    matched: set[date] = set()
    for record in records:
        if record.source is Source.USER:
            continue
        same = next((b for b in used if (b.start, b.end) == (record.start, record.end)), None)
        if same is not None:
            keep.append(replace(record, source=Source.UTILITY, bill=same.amounts))
            matched.add(same.start)
        elif not any(_overlaps(record, b) for b in used):
            keep.append(record)

    compute = [b for b in used if b.start not in matched]
    keep.sort(key=lambda r: r.start, reverse=True)
    return MergeResult(keep=keep, compute=compute)
