from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest
from core.compute import hour_starts
from core.cycles import (
    Cycle,
    add_months,
    backfill,
    from_cycle_day,
    from_utility_dates,
    is_final,
    previous_cycles,
)

NY = ZoneInfo("America/New_York")


def test_utility_dates_have_inclusive_end() -> None:
    cycle = from_utility_dates(date(2026, 10, 3), date(2026, 11, 1))
    assert cycle == Cycle(date(2026, 10, 3), date(2026, 11, 2))
    assert cycle.days == 30
    assert cycle.last_day == date(2026, 11, 1)
    assert cycle.contains(date(2026, 11, 1))
    assert not cycle.contains(date(2026, 11, 2))


def test_cycle_rejects_empty_range() -> None:
    with pytest.raises(ValueError, match="after start"):
        Cycle(date(2026, 1, 1), date(2026, 1, 1))


@pytest.mark.parametrize(
    ("day", "months", "expected"),
    [
        (date(2026, 1, 31), 1, date(2026, 2, 28)),
        (date(2028, 1, 31), 1, date(2028, 2, 29)),
        (date(2026, 3, 31), -1, date(2026, 2, 28)),
        (date(2026, 1, 15), -1, date(2025, 12, 15)),
        (date(2026, 11, 15), 14, date(2028, 1, 15)),
    ],
)
def test_add_months_clamps(day: date, months: int, expected: date) -> None:
    assert add_months(day, months) == expected


def test_cycle_day_on_or_after_read_day() -> None:
    assert from_cycle_day(date(2026, 11, 16), 3) == Cycle(date(2026, 11, 3), date(2026, 12, 3))
    assert from_cycle_day(date(2026, 11, 3), 3) == Cycle(date(2026, 11, 3), date(2026, 12, 3))


def test_cycle_day_before_read_day_wraps_year() -> None:
    assert from_cycle_day(date(2026, 1, 2), 5) == Cycle(date(2025, 12, 5), date(2026, 1, 5))


@pytest.mark.parametrize("bad", [0, 29, 31])
def test_cycle_day_validated(bad: int) -> None:
    with pytest.raises(ValueError, match="1-28"):
        from_cycle_day(date(2026, 1, 1), bad)


def test_previous_cycles_are_contiguous_and_do_not_drift() -> None:
    current = Cycle(date(2026, 3, 31), date(2026, 4, 30))
    cycles = previous_cycles(current, 12)
    assert len(cycles) == 12
    assert cycles[0] == Cycle(date(2026, 2, 28), date(2026, 3, 31))
    assert cycles[1] == Cycle(date(2026, 1, 31), date(2026, 2, 28))
    newer = current
    for cycle in cycles:
        assert cycle.end == newer.start
        newer = cycle
    # Clamping in February does not pull later boundaries off the 31st.
    assert cycles[2].start == date(2025, 12, 31)


def test_backfill_stops_at_history() -> None:
    current = Cycle(date(2026, 11, 3), date(2026, 12, 3))
    # Data starts mid-way through the cycle that began 2026-06-03.
    got = backfill(current, 12, date(2026, 6, 20))
    assert [c.start for c in got] == [
        date(2026, 10, 3),
        date(2026, 9, 3),
        date(2026, 8, 3),
        date(2026, 7, 3),
        date(2026, 6, 3),
    ]


def test_backfill_capped_at_max_count() -> None:
    current = Cycle(date(2026, 11, 3), date(2026, 12, 3))
    assert len(backfill(current, 12, date(2020, 1, 1))) == 12


def test_backfill_without_history_is_empty() -> None:
    current = Cycle(date(2026, 11, 3), date(2026, 12, 3))
    assert backfill(current, 12, None) == []
    assert backfill(current, 12, date(2026, 11, 3)) == []


def test_is_final_after_grace() -> None:
    cycle = Cycle(date(2026, 10, 3), date(2026, 11, 3))
    assert not is_final(cycle, date(2026, 11, 5))
    assert is_final(cycle, date(2026, 11, 6))
    assert is_final(cycle, date(2026, 11, 3), grace_days=0)


def test_local_bounds_across_dst_end() -> None:
    # DST ends 2026-11-01 in New York: that local day is 25 hours long.
    cycle = Cycle(date(2026, 11, 1), date(2026, 11, 2))
    start, end = cycle.local_bounds(NY)
    assert start.utcoffset() == timedelta(hours=-4)
    assert end.utcoffset() == timedelta(hours=-5)
    assert len(hour_starts(start, end)) == 25


def test_local_bounds_across_dst_start() -> None:
    cycle = Cycle(date(2026, 3, 8), date(2026, 3, 9))
    assert len(hour_starts(*cycle.local_bounds(NY))) == 23
