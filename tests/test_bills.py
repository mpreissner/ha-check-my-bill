from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from core.bills import UtilityBill, bills_from_reads, merge_bills
from core.compute import Coverage, CycleTotals
from core.cycles import Cycle
from core.record import BilledAmounts, CycleRecord, Source, build_record

TZ = ZoneInfo("America/New_York")


def at(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=TZ)


def test_midnight_and_last_second_ends_agree() -> None:
    bills = bills_from_reads(
        [
            (at(2026, 8, 19), at(2026, 9, 17, 23, 59, 59), 600.0, 90.0),
            (at(2026, 7, 18), at(2026, 8, 19), 650.0, 95.0),
        ]
    )
    assert [(b.start, b.end) for b in bills] == [
        (date(2026, 7, 18), date(2026, 8, 19)),
        (date(2026, 8, 19), date(2026, 9, 18)),
    ]
    assert bills[0].amounts == BilledAmounts(650.0, 95.0)


def test_one_day_gap_snaps_to_the_next_start() -> None:
    # Last day reported at midnight: Jul 18 - Aug 18, then Aug 19 - Sep 17.
    bills = bills_from_reads(
        [
            (at(2026, 7, 18), at(2026, 8, 18), 1.0, 1.0),
            (at(2026, 8, 19), at(2026, 9, 17), 1.0, 1.0),
        ]
    )
    assert bills[0].end == date(2026, 8, 19)
    assert bills[1].end == date(2026, 9, 17)  # nothing after it to snap to


def test_empty_and_duplicate_periods_are_dropped() -> None:
    bills = bills_from_reads(
        [
            (at(2026, 7, 18), at(2026, 7, 18), 0.0, 0.0),
            (at(2026, 8, 19), at(2026, 9, 18), 1.0, 1.0),
            (at(2026, 8, 19), at(2026, 9, 18), 2.0, 2.0),
        ]
    )
    assert len(bills) == 1
    assert bills[0].kwh == 1.0


def record(start: date, end: date, source: Source) -> CycleRecord:
    totals = CycleTotals(
        metered_kwh=1.0,
        export_kwh=None,
        netted_kwh=None,
        utility_kwh=1.0,
        utility_export_kwh=None,
        coverage=Coverage(imports=1.0, utility=1.0),
    )
    return build_record(
        Cycle(start, end), totals, source=source, final=True, closed=True, tariff=None
    )


def bill(start: date, end: date) -> UtilityBill:
    return UtilityBill(start, end, kwh=500.0, cost=80.0)


CURRENT = date(2026, 10, 19)
BILLS = [
    bill(date(2026, 7, 18), date(2026, 8, 19)),
    bill(date(2026, 8, 19), date(2026, 9, 18)),
    bill(date(2026, 9, 18), CURRENT),
]


def backfilled() -> list[CycleRecord]:
    return [
        record(date(2026, 9, 19), CURRENT, Source.BACKFILL),
        record(date(2026, 8, 19), date(2026, 9, 19), Source.BACKFILL),
        record(date(2026, 7, 19), date(2026, 8, 19), Source.BACKFILL),
    ]


def test_estimates_are_replaced_by_bills() -> None:
    result = merge_bills(backfilled(), BILLS, CURRENT, 12)
    assert result.keep == []
    assert result.compute == BILLS


def test_matching_record_is_kept_with_amounts() -> None:
    existing = record(date(2026, 9, 18), CURRENT, Source.UTILITY)
    result = merge_bills([existing], BILLS, CURRENT, 12)
    assert len(result.keep) == 1
    assert result.keep[0].bill == BilledAmounts(500.0, 80.0)
    assert result.keep[0].metered_kwh == existing.metered_kwh
    assert [b.start for b in result.compute] == [date(2026, 7, 18), date(2026, 8, 19)]

    estimated = record(date(2026, 9, 18), CURRENT, Source.BACKFILL)
    assert merge_bills([estimated], BILLS, CURRENT, 12).keep[0].source is Source.UTILITY


def test_user_records_win() -> None:
    user = record(date(2026, 8, 20), date(2026, 9, 18), Source.USER)
    result = merge_bills([user, *backfilled()[2:]], BILLS, CURRENT, 12)
    assert result.keep == [user]
    assert [b.start for b in result.compute] == [date(2026, 7, 18), date(2026, 9, 18)]


def test_unfinished_bills_are_ignored_and_newer_cycles_kept() -> None:
    # The cycle that closed on Oct 19 has no bill yet; the current one started Nov 18.
    closed = record(CURRENT, date(2026, 11, 18), Source.UTILITY)
    running = bill(date(2026, 11, 18), date(2026, 12, 18))
    result = merge_bills([closed], [*BILLS, running], date(2026, 11, 18), 12)
    assert result.keep == [closed]
    assert result.compute == BILLS


def test_only_the_newest_bills_are_used() -> None:
    result = merge_bills([], BILLS, CURRENT, 2)
    assert result.compute == BILLS[1:]
    assert merge_bills([], BILLS, CURRENT, 0).compute == []


def test_records_older_than_the_bills_are_kept() -> None:
    old = record(date(2026, 6, 19), date(2026, 7, 18), Source.BACKFILL)
    result = merge_bills([old], BILLS, CURRENT, 12)
    assert result.keep == [old]
    assert all(b.end - b.start > timedelta(0) for b in result.compute)
