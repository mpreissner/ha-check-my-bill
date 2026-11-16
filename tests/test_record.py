import json
from datetime import date

import pytest
from core.compute import Coverage, CycleTotals
from core.cycles import Cycle
from core.reconcile import Status
from core.record import CycleRecord, Source, build_record, closed_by_rollover
from core.tariff import RateSpec, Tariff, TariffVersion

CYCLE = Cycle(date(2026, 10, 3), date(2026, 11, 2))  # 30 days
TARIFF = Tariff(
    (TariffVersion(date(2020, 1, 1), RateSpec(0.10), RateSpec(0.05), fixed_monthly=9.0),)
)


def totals(utility: float | None = 300.0, utility_cov: float | None = 1.0) -> CycleTotals:
    return CycleTotals(
        metered_kwh=305.0,
        export_kwh=2.0,
        netted_kwh=303.5,
        utility_kwh=utility,
        utility_export_kwh=None,
        coverage=Coverage(imports=1.0, exports=1.0, utility=utility_cov),
    )


def test_closed_cycle_record() -> None:
    record = build_record(
        CYCLE, totals(), source=Source.UTILITY, final=True, closed=True, tariff=TARIFF
    )
    assert record.status is Status.OK
    assert record.cost_basis == "utility"
    assert record.cost is not None
    assert record.cost.total == pytest.approx(300 * 0.15 + 9.0)
    assert not record.estimated
    assert record.cycle == CYCLE


def test_open_cycle_prices_to_date_from_meter() -> None:
    record = build_record(
        CYCLE,
        totals(utility=100.0, utility_cov=0.6),
        source=Source.MANUAL,
        final=False,
        closed=False,
        tariff=TARIFF,
        cost_until=date(2026, 10, 18),  # 15 of 30 days
    )
    assert record.status is Status.IN_PROGRESS
    assert record.cost_basis == "meter"
    assert record.cost is not None
    assert record.cost.fixed == pytest.approx(4.5)
    assert record.cost.supply == pytest.approx(30.5)
    assert record.estimated


def test_cost_until_on_first_day_has_no_cost() -> None:
    record = build_record(
        CYCLE,
        totals(),
        source=Source.UTILITY,
        final=False,
        closed=False,
        tariff=TARIFF,
        cost_until=CYCLE.start,
    )
    assert record.cost is None
    assert record.cost_basis is None


def test_without_tariff_there_is_no_cost() -> None:
    record = build_record(
        CYCLE, totals(), source=Source.BACKFILL, final=True, closed=True, tariff=None
    )
    assert record.cost is None
    assert record.status is Status.OK


def test_round_trip_through_json() -> None:
    record = build_record(
        CYCLE, totals(), source=Source.UTILITY, final=True, closed=True, tariff=TARIFF
    )
    data = json.loads(json.dumps(record.to_dict()))
    assert CycleRecord.from_dict(data) == record
    assert data["expected_cost"]["total"] == pytest.approx(record.cost.total)  # type: ignore[union-attr]


def test_round_trip_no_data() -> None:
    record = build_record(
        CYCLE,
        totals(utility=None, utility_cov=None),
        source=Source.BACKFILL,
        final=True,
        closed=True,
        tariff=None,
    )
    assert record.status is Status.NO_DATA
    assert CycleRecord.from_dict(json.loads(json.dumps(record.to_dict()))) == record


def test_infinite_deviation_is_stored_as_null() -> None:
    zero = CycleTotals(0.0, 0.0, 0.0, 5.0, None, Coverage(1.0, 1.0, 1.0))
    record = build_record(CYCLE, zero, source=Source.UTILITY, final=True, closed=True, tariff=None)
    assert record.status is Status.MISMATCH
    assert record.to_dict()["deviation_pct"] is None


def test_rollover_closes_at_new_start() -> None:
    previous = Cycle(date(2026, 10, 3), date(2026, 11, 3))  # utility said Nov 2 inclusive
    current = Cycle(date(2026, 11, 2), date(2026, 12, 2))
    assert closed_by_rollover(previous, current) == Cycle(date(2026, 10, 3), date(2026, 11, 2))


def test_rollover_after_missed_cycles_keeps_previous_end() -> None:
    previous = Cycle(date(2026, 8, 3), date(2026, 9, 2))
    current = Cycle(date(2026, 11, 2), date(2026, 12, 2))
    assert closed_by_rollover(previous, current) == previous


def test_no_rollover() -> None:
    current = Cycle(date(2026, 11, 2), date(2026, 12, 2))
    assert closed_by_rollover(None, current) is None
    assert closed_by_rollover(current, current) is None
