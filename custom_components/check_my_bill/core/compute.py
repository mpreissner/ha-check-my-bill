"""Turn statistic change series into per-cycle totals.

The HA layer reads ``change`` values from the recorder, keyed by the slot's start time
(hourly where available, daily otherwise), and passes them here with the list of slots
the period should contain. Missing slots lower coverage; they are not treated as zero.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

type Series = Mapping[datetime, float]


def hour_starts(start: datetime, end: datetime) -> list[datetime]:
    """UTC hour starts in ``[start, end)``. Walking in UTC keeps DST days at 23 or 25 hours."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware")
    current = start.astimezone(UTC)
    stop = end.astimezone(UTC)
    slots = []
    while current < stop:
        slots.append(current)
        current += timedelta(hours=1)
    return slots


@dataclass(frozen=True, slots=True)
class Coverage:
    """Fraction of expected slots present for each input; None when not configured."""

    imports: float
    exports: float | None = None
    utility: float | None = None
    utility_exports: float | None = None


@dataclass(frozen=True, slots=True)
class CycleTotals:
    """kWh totals for one period."""

    metered_kwh: float
    """Gross grid import measured by HA."""
    export_kwh: float | None
    """Grid export measured by HA, if an export statistic is configured."""
    netted_kwh: float | None
    """Sum of max(0, import - export) per hour. None when only daily data exists."""
    utility_kwh: float | None
    """Import as metered by the utility, if a utility statistic is configured."""
    utility_export_kwh: float | None
    coverage: Coverage


def _sum(series: Series, slots: frozenset[datetime]) -> tuple[float, int]:
    values = [v for k, v in series.items() if k in slots]
    return sum(values), len(values)


def compute_totals(
    slots: Sequence[datetime],
    imports: Series,
    exports: Series | None = None,
    utility: Series | None = None,
    utility_exports: Series | None = None,
    *,
    hourly: bool = True,
) -> CycleTotals:
    """Totals over ``slots``. Series entries outside ``slots`` are ignored."""
    wanted = frozenset(slots)
    expected = len(wanted)

    def ratio(count: int) -> float:
        return count / expected if expected else 0.0

    metered, n_import = _sum(imports, wanted)

    export_kwh: float | None = None
    export_cov: float | None = None
    if exports is not None:
        export_kwh, n_export = _sum(exports, wanted)
        export_cov = ratio(n_export)

    netted: float | None = None
    if hourly:
        exports_by_slot = exports or {}
        netted = sum(
            max(0.0, value - exports_by_slot.get(slot, 0.0))
            for slot, value in imports.items()
            if slot in wanted
        )

    utility_kwh: float | None = None
    utility_cov: float | None = None
    if utility is not None:
        utility_kwh, n_utility = _sum(utility, wanted)
        utility_cov = ratio(n_utility)

    utility_export_kwh: float | None = None
    utility_export_cov: float | None = None
    if utility_exports is not None:
        utility_export_kwh, n_uexp = _sum(utility_exports, wanted)
        utility_export_cov = ratio(n_uexp)

    return CycleTotals(
        metered_kwh=metered,
        export_kwh=export_kwh,
        netted_kwh=netted,
        utility_kwh=utility_kwh,
        utility_export_kwh=utility_export_kwh,
        coverage=Coverage(
            imports=ratio(n_import),
            exports=export_cov,
            utility=utility_cov,
            utility_exports=utility_export_cov,
        ),
    )


@dataclass(frozen=True, slots=True)
class CostBasis:
    """The kWh an expected cost should be computed from, and where they came from."""

    import_kwh: float
    export_kwh: float
    source: str
    """``"utility"`` when the utility's figures are complete, otherwise ``"meter"``."""


def cost_basis(totals: CycleTotals) -> CostBasis:
    """Prefer complete utility figures; fall back to HA's meter as an estimate."""
    if totals.utility_kwh is not None and totals.coverage.utility == 1.0:
        if totals.utility_export_kwh is not None:
            export = totals.utility_export_kwh
        else:
            export = totals.export_kwh or 0.0
        return CostBasis(totals.utility_kwh, export, "utility")
    return CostBasis(totals.metered_kwh, totals.export_kwh or 0.0, "meter")


def project(value_to_date: float, elapsed: float, total: float) -> float | None:
    """Linear projection of ``value_to_date`` to the end of the period."""
    if elapsed <= 0 or total <= 0:
        return None
    return value_to_date * max(total, elapsed) / elapsed
