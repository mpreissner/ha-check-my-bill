from datetime import UTC, datetime, timedelta

import pytest
from core.compute import Coverage, CycleTotals, compute_totals, cost_basis, hour_starts, project

T0 = datetime(2026, 10, 1, tzinfo=UTC)
SLOTS = hour_starts(T0, T0 + timedelta(hours=4))


def h(i: int) -> datetime:
    return T0 + timedelta(hours=i)


def test_hour_starts_requires_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        hour_starts(datetime(2026, 1, 1), datetime(2026, 1, 2))


def test_gross_netted_and_export() -> None:
    imports = {h(0): 1.0, h(1): 0.5, h(2): 0.2, h(3): 2.0}
    exports = {h(0): 0.0, h(1): 0.8, h(2): 0.1, h(3): 0.0}
    totals = compute_totals(SLOTS, imports, exports)
    assert totals.metered == pytest.approx(3.7)
    assert totals.export == pytest.approx(0.9)
    # Hour 1 nets to zero, hour 2 to 0.1.
    assert totals.netted == pytest.approx(1.0 + 0.0 + 0.1 + 2.0)
    assert totals.coverage == Coverage(imports=1.0, exports=1.0)


def test_netted_equals_gross_without_export_stat() -> None:
    totals = compute_totals(SLOTS, dict.fromkeys(SLOTS, 1.0))
    assert totals.netted == totals.metered == 4.0
    assert totals.export is None


def test_missing_slots_lower_coverage_and_outside_slots_ignored() -> None:
    utility = {h(0): 1.0, h(1): 1.0, h(9): 50.0}
    totals = compute_totals(SLOTS, dict.fromkeys(SLOTS, 1.0), utility=utility)
    assert totals.utility == 2.0
    assert totals.coverage.utility == 0.5
    assert totals.coverage.imports == 1.0


def test_interior_gap_keeps_full_coverage() -> None:
    # The recorder folds a missed hour's energy into the next row's change.
    imports = {h(0): 1.0, h(2): 2.0, h(3): 1.0}
    totals = compute_totals(SLOTS, imports, utility={h(1): 1.0, h(2): 1.0})
    assert totals.metered == 4.0
    assert totals.coverage.imports == 1.0
    assert totals.coverage.utility == 0.5  # leading and trailing gaps are missing data


def test_daily_data_has_no_netted_value() -> None:
    days = [T0, T0 + timedelta(days=1)]
    totals = compute_totals(days, {T0: 10.0, days[1]: 12.0}, {T0: 1.0}, hourly=False)
    assert totals.metered == 22.0
    assert totals.netted is None
    assert totals.coverage.exports == 0.5


def test_empty_period() -> None:
    totals = compute_totals([], {})
    assert totals.metered == 0.0
    assert totals.coverage.imports == 0.0


def totals(utility_cov: float | None, utility_export: float | None = None) -> CycleTotals:
    return CycleTotals(
        metered=100.0,
        export=5.0,
        netted=97.0,
        utility=None if utility_cov is None else 99.0,
        utility_export=utility_export,
        coverage=Coverage(imports=1.0, exports=1.0, utility=utility_cov),
    )


def test_cost_basis_prefers_complete_utility() -> None:
    basis = cost_basis(totals(1.0))
    assert (basis.imports, basis.exports, basis.source) == (99.0, 5.0, "utility")
    assert cost_basis(totals(1.0, utility_export=0.0)).exports == 0.0


def test_cost_basis_falls_back_to_meter() -> None:
    assert cost_basis(totals(0.9)).source == "meter"
    basis = cost_basis(totals(None))
    assert (basis.imports, basis.source) == (100.0, "meter")


def test_project() -> None:
    assert project(50.0, 10, 30) == pytest.approx(150.0)
    assert project(50.0, 0, 30) is None
    assert project(50.0, 40, 30) == pytest.approx(50.0)
