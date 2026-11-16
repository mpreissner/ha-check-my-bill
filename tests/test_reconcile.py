import pytest
from core.compute import Coverage, CycleTotals
from core.reconcile import Band, Status, band, calibration_ratio, reconcile
from core.tariff import ExportMode

FULL = Coverage(imports=1.0, exports=1.0, utility=1.0)


def make(
    utility: float | None,
    *,
    metered: float = 82.6,
    netted: float | None = 60.4,
    export: float | None = 30.0,
    coverage: Coverage = FULL,
) -> CycleTotals:
    return CycleTotals(
        metered_kwh=metered,
        export_kwh=export,
        netted_kwh=netted,
        utility_kwh=utility,
        utility_export_kwh=None,
        coverage=coverage,
    )


def test_sub_hourly_netting_meter_is_ok() -> None:
    # The observed pattern: the meter lands between hourly-netted and gross import.
    result = reconcile(make(74.3), ExportMode.NONE, final=True)
    assert result.status is Status.OK
    assert result.deviation_pct == 0.0
    assert result.band == Band(pytest.approx(60.4 * 0.98), pytest.approx(82.6 * 1.02))


@pytest.mark.parametrize(
    ("utility", "status"),
    [
        (82.6 * 1.03, Status.WARN),
        (82.6 * 1.05, Status.MISMATCH),
        (60.4 * 0.97, Status.WARN),
        (60.4 * 0.90, Status.MISMATCH),
    ],
)
def test_outside_band(utility: float, status: Status) -> None:
    result = reconcile(make(utility), ExportMode.NONE, final=True)
    assert result.status is status
    assert result.deviation_pct != 0.0


def test_deviation_is_signed() -> None:
    assert reconcile(make(90.0), ExportMode.NONE, final=True).deviation_pct > 0
    assert reconcile(make(50.0), ExportMode.NONE, final=True).deviation_pct < 0


def test_no_export_band_is_tight() -> None:
    totals = make(103.0, metered=100.0, netted=100.0, export=0.0)
    assert reconcile(totals, ExportMode.NONE, final=True).status is Status.WARN


def test_daily_only_data_uses_gross_for_low_edge() -> None:
    assert band(make(None, netted=None), ExportMode.NONE, 0.0) == Band(82.6, 82.6)


def test_net_metering_band_is_centered_on_net() -> None:
    totals = make(52.6, metered=82.6, export=30.0)
    result = reconcile(totals, ExportMode.NET_METERING, final=True)
    assert result.status is Status.OK
    assert result.band == Band(pytest.approx(52.6 * 0.98), pytest.approx(52.6 * 1.02))


def test_no_utility_statistic() -> None:
    result = reconcile(make(None), ExportMode.NONE, final=True)
    assert result.status is Status.NO_DATA
    assert result.band is None


def test_lagging_utility_is_in_progress_never_mismatch() -> None:
    coverage = Coverage(imports=1.0, exports=1.0, utility=0.9)
    result = reconcile(make(10.0, coverage=coverage), ExportMode.NONE, final=False)
    assert result.status is Status.IN_PROGRESS
    assert result.deviation_pct is None


def test_incomplete_closed_cycle_is_no_data() -> None:
    coverage = Coverage(imports=0.95, exports=1.0, utility=1.0)
    result = reconcile(make(10.0, coverage=coverage), ExportMode.NONE, final=True)
    assert result.status is Status.NO_DATA


@pytest.mark.parametrize("mode", list(ExportMode))
def test_incomplete_export_is_never_mismatch(mode: ExportMode) -> None:
    coverage = Coverage(imports=1.0, exports=0.5, utility=1.0)
    totals = make(52.6, coverage=coverage)
    assert reconcile(totals, mode, final=True).status is Status.NO_DATA
    assert reconcile(totals, mode, final=False).status is Status.IN_PROGRESS


@pytest.mark.parametrize("bad", [-0.1, 1.0])
def test_tolerance_validated(bad: float) -> None:
    with pytest.raises(ValueError, match="tolerance"):
        reconcile(make(74.3), ExportMode.NONE, final=True, tolerance=bad)


def test_calibration_ratio() -> None:
    totals = make(101.9, metered=100.0, netted=100.0, export=0.0)
    assert calibration_ratio(totals) == pytest.approx(1.019)
    assert calibration_ratio(make(101.9, metered=100.0, export=None)) == pytest.approx(1.019)


def test_calibration_ratio_needs_clean_cycle() -> None:
    assert calibration_ratio(make(74.3)) is None  # has export
    assert calibration_ratio(make(None, export=0.0)) is None
    partial = Coverage(imports=1.0, utility=0.5)
    assert calibration_ratio(make(50.0, export=0.0, coverage=partial)) is None
    assert calibration_ratio(make(1.0, metered=0.0, export=0.0)) is None
