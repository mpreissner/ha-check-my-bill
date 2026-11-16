"""One billing cycle's persisted result, and how it is built from totals.

Records are what the Store keeps (spec §3.2) and what sensors and the card read. Building
one is pure: totals in, record out. The HA layer only fetches the statistics.
"""

import math
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from .compute import Coverage, CycleTotals, cost_basis
from .cycles import Cycle
from .reconcile import DEFAULT_TOLERANCE, Band, Status, calibration_ratio, reconcile
from .tariff import CostBreakdown, ExportMode, Tariff, cost


class Source(StrEnum):
    """Where a cycle's boundaries came from."""

    UTILITY = "utility"
    """The utility's billing-date sensors."""
    MANUAL = "manual"
    """The configured meter-read day. Utility read dates drift, so these are estimates."""
    BACKFILL = "backfill"
    """Stepped back from the current cycle by whole months. Estimated."""
    USER = "user"
    """Entered by the user from a bill (the ``set_cycle_dates`` action)."""


@dataclass(frozen=True, slots=True)
class CycleRecord:
    """The result for one cycle. ``end`` is exclusive."""

    start: date
    end: date
    source: Source
    final: bool
    """A closed cycle past the grace period; it is no longer recomputed."""
    metered_kwh: float
    export_kwh: float | None
    netted_kwh: float | None
    utility_kwh: float | None
    utility_export_kwh: float | None
    coverage: Coverage
    status: Status
    band: Band | None
    deviation_pct: float | None
    calibration_ratio: float | None
    cost: CostBreakdown | None
    """Expected charges; None without a tariff. To date for the open cycle."""
    cost_basis: str | None
    """``"utility"`` or ``"meter"``: which kWh the cost was computed from."""

    @property
    def cycle(self) -> Cycle:
        """The cycle this record covers."""
        return Cycle(self.start, self.end)

    @property
    def estimated(self) -> bool:
        """Whether the boundaries are estimates rather than dates from the utility or a bill."""
        return self.source in (Source.MANUAL, Source.BACKFILL)

    @property
    def totals(self) -> CycleTotals:
        """The totals this record was built from."""
        return CycleTotals(
            metered_kwh=self.metered_kwh,
            export_kwh=self.export_kwh,
            netted_kwh=self.netted_kwh,
            utility_kwh=self.utility_kwh,
            utility_export_kwh=self.utility_export_kwh,
            coverage=self.coverage,
        )

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe dict for the Store and the WebSocket API."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "source": self.source.value,
            "final": self.final,
            "metered_kwh": self.metered_kwh,
            "export_kwh": self.export_kwh,
            "netted_kwh": self.netted_kwh,
            "utility_kwh": self.utility_kwh,
            "utility_export_kwh": self.utility_export_kwh,
            "coverage": {
                "import": self.coverage.imports,
                "export": self.coverage.exports,
                "utility": self.coverage.utility,
                "utility_export": self.coverage.utility_exports,
            },
            "status": self.status.value,
            "band": None if self.band is None else [self.band.low, self.band.high],
            "deviation_pct": _finite(self.deviation_pct),
            "calibration_ratio": self.calibration_ratio,
            "expected_cost": None if self.cost is None else cost_to_dict(self.cost),
            "cost_basis": self.cost_basis,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CycleRecord:
        """The inverse of :meth:`to_dict`."""
        cov = data["coverage"]
        band = data.get("band")
        expected = data.get("expected_cost")
        return cls(
            start=date.fromisoformat(data["start"]),
            end=date.fromisoformat(data["end"]),
            source=Source(data["source"]),
            final=bool(data["final"]),
            metered_kwh=float(data["metered_kwh"]),
            export_kwh=data.get("export_kwh"),
            netted_kwh=data.get("netted_kwh"),
            utility_kwh=data.get("utility_kwh"),
            utility_export_kwh=data.get("utility_export_kwh"),
            coverage=Coverage(
                imports=float(cov["import"]),
                exports=cov.get("export"),
                utility=cov.get("utility"),
                utility_exports=cov.get("utility_export"),
            ),
            status=Status(data["status"]),
            band=None if band is None else Band(float(band[0]), float(band[1])),
            deviation_pct=data.get("deviation_pct"),
            calibration_ratio=data.get("calibration_ratio"),
            cost=None if expected is None else _cost_from_dict(expected),
            cost_basis=data.get("cost_basis"),
        )


def _finite(value: float | None) -> float | None:
    return value if value is None or math.isfinite(value) else None


def cost_to_dict(c: CostBreakdown) -> dict[str, float]:
    """A JSON-safe cost breakdown."""
    return {
        "billed_kwh": c.billed_kwh,
        "supply": c.supply,
        "delivery": c.delivery,
        "riders": c.riders,
        "fixed": c.fixed,
        "tax": c.tax,
        "export_credit": c.export_credit,
        "total": c.total,
    }


def _cost_from_dict(data: dict[str, Any]) -> CostBreakdown:
    return CostBreakdown(
        billed_kwh=float(data["billed_kwh"]),
        supply=float(data["supply"]),
        delivery=float(data["delivery"]),
        riders=float(data["riders"]),
        fixed=float(data["fixed"]),
        tax=float(data["tax"]),
        export_credit=float(data["export_credit"]),
    )


def build_record(
    cycle: Cycle,
    totals: CycleTotals,
    *,
    source: Source,
    final: bool,
    closed: bool,
    tariff: Tariff | None,
    export_mode: ExportMode = ExportMode.NONE,
    tolerance: float = DEFAULT_TOLERANCE,
    cost_until: date | None = None,
) -> CycleRecord:
    """Reconcile and price one cycle.

    ``closed`` says the cycle has ended; incomplete data is then ``no_data`` rather than
    ``in_progress``. ``cost_until`` (exclusive) prices only the days so far, for the open
    cycle; the monthly fixed charge is still prorated over the whole cycle.
    """
    result = reconcile(totals, export_mode, final=closed, tolerance=tolerance)

    expected: CostBreakdown | None = None
    basis_source: str | None = None
    if tariff is not None:
        basis = cost_basis(totals)
        end = cycle.end if cost_until is None else max(min(cost_until, cycle.end), cycle.start)
        if end > cycle.start:
            expected = cost(
                tariff,
                cycle.start,
                end,
                basis.import_kwh,
                basis.export_kwh,
                period_days=cycle.days,
            )
            basis_source = basis.source

    return CycleRecord(
        start=cycle.start,
        end=cycle.end,
        source=source,
        final=final,
        metered_kwh=totals.metered_kwh,
        export_kwh=totals.export_kwh,
        netted_kwh=totals.netted_kwh,
        utility_kwh=totals.utility_kwh,
        utility_export_kwh=totals.utility_export_kwh,
        coverage=totals.coverage,
        status=result.status,
        band=result.band,
        deviation_pct=result.deviation_pct,
        calibration_ratio=calibration_ratio(totals),
        cost=expected,
        cost_basis=basis_source,
    )


def reprice(
    record: CycleRecord,
    *,
    tariff: Tariff | None,
    export_mode: ExportMode = ExportMode.NONE,
    tolerance: float = DEFAULT_TOLERANCE,
) -> CycleRecord:
    """A closed record rebuilt from its own totals with other settings.

    Source and finality are kept; no statistics are needed.
    """
    return build_record(
        record.cycle,
        record.totals,
        source=record.source,
        final=record.final,
        closed=True,
        tariff=tariff,
        export_mode=export_mode,
        tolerance=tolerance,
    )


def closed_by_rollover(
    previous: Cycle | None, current: Cycle, max_gap_days: int = 7
) -> Cycle | None:
    """The cycle that closed when the current cycle moved from ``previous`` to ``current``.

    The closed cycle ends where the new one starts (spec §3.2). If the dates jumped by
    more than a short gap, HA missed at least one whole cycle; the previous cycle then
    keeps its own end rather than swallowing the cycles in between.
    """
    if previous is None or current.start <= previous.start:
        return None
    if (current.start - previous.end).days > max_gap_days:
        return previous
    return Cycle(previous.start, current.start)
