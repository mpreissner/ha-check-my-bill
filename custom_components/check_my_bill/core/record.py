"""One billing cycle's persisted result, and how it is built from totals.

Records are what the Store keeps (spec §3.2) and what sensors and the card read. Building
one is pure: totals in, record out. The HA layer only fetches the statistics.
"""

import math
from dataclasses import dataclass, replace
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
class BilledAmounts:
    """What the utility billed for a cycle, as Opower reports it."""

    quantity: float | None
    cost: float | None
    """Opower's provided cost; depending on the utility it may leave out taxes and fees."""


@dataclass(frozen=True, slots=True)
class CycleRecord:
    """The result for one cycle. ``end`` is exclusive."""

    start: date
    end: date
    source: Source
    final: bool
    """A closed cycle past the grace period; it is no longer recomputed."""
    metered: float
    export: float | None
    netted: float | None
    utility: float | None
    utility_export: float | None
    coverage: Coverage
    status: Status
    band: Band | None
    deviation_pct: float | None
    calibration_ratio: float | None
    cost: CostBreakdown | None
    """Expected charges; None without a tariff. To date for the open cycle."""
    cost_basis: str | None
    """``"utility"`` or ``"meter"``: which quantities the cost was computed from."""
    bill: BilledAmounts | None = None
    """The utility's bill for this period, when Opower reported one."""

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
            metered=self.metered,
            export=self.export,
            netted=self.netted,
            utility=self.utility,
            utility_export=self.utility_export,
            coverage=self.coverage,
        )

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe dict for the Store and the WebSocket API."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "source": self.source.value,
            "final": self.final,
            "metered": self.metered,
            "export": self.export,
            "netted": self.netted,
            "utility": self.utility,
            "utility_export": self.utility_export,
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
            "bill": None
            if self.bill is None
            else {"quantity": self.bill.quantity, "cost": self.bill.cost},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CycleRecord:
        """The inverse of :meth:`to_dict`. Also reads records saved with legacy keys."""
        data = upgrade_record_dict(data)
        cov = data["coverage"]
        band = data.get("band")
        expected = data.get("expected_cost")
        billed = data.get("bill")
        return cls(
            start=date.fromisoformat(data["start"]),
            end=date.fromisoformat(data["end"]),
            source=Source(data["source"]),
            final=bool(data["final"]),
            metered=float(data["metered"]),
            export=data.get("export"),
            netted=data.get("netted"),
            utility=data.get("utility"),
            utility_export=data.get("utility_export"),
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
            bill=None
            if billed is None
            else BilledAmounts(billed.get("quantity"), billed.get("cost")),
        )


_LEGACY_RECORD_KEYS = {
    "metered_kwh": "metered",
    "export_kwh": "export",
    "netted_kwh": "netted",
    "utility_kwh": "utility",
    "utility_export_kwh": "utility_export",
}


def _renamed(data: dict[str, Any], keys: dict[str, str]) -> dict[str, Any]:
    """``data`` with old keys renamed; a new key already present wins."""
    out = {k: v for k, v in data.items() if k not in keys}
    for old, new in keys.items():
        if old in data and new not in out:
            out[new] = data[old]
    return out


def upgrade_record_dict(data: dict[str, Any]) -> dict[str, Any]:
    """A stored record dict with the kWh-named keys of 0.1.0b3 and earlier renamed.

    Returns a new dict; ``data`` is not changed. Current dicts come back equal.
    """
    out = _renamed(data, _LEGACY_RECORD_KEYS)
    if isinstance(bill := out.get("bill"), dict):
        out["bill"] = _renamed(bill, {"kwh": "quantity"})
    if isinstance(expected := out.get("expected_cost"), dict):
        out["expected_cost"] = _renamed(expected, {"billed_kwh": "billed_quantity"})
    return out


def _finite(value: float | None) -> float | None:
    return value if value is None or math.isfinite(value) else None


def cost_to_dict(c: CostBreakdown) -> dict[str, float]:
    """A JSON-safe cost breakdown."""
    return {
        "billed_quantity": c.billed_quantity,
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
        billed_quantity=float(data["billed_quantity"]),
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
    bill: BilledAmounts | None = None,
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
                basis.imports,
                basis.exports,
                period_days=cycle.days,
            )
            basis_source = basis.source

    return CycleRecord(
        start=cycle.start,
        end=cycle.end,
        source=source,
        final=final,
        metered=totals.metered,
        export=totals.export,
        netted=totals.netted,
        utility=totals.utility,
        utility_export=totals.utility_export,
        coverage=totals.coverage,
        status=result.status,
        band=result.band,
        deviation_pct=result.deviation_pct,
        calibration_ratio=calibration_ratio(totals),
        cost=expected,
        cost_basis=basis_source,
        bill=bill,
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
        bill=record.bill,
    )


def _times(value: float | None, factor: float) -> float | None:
    return None if value is None else value * factor


def rescale(record: CycleRecord, factor: float) -> CycleRecord:
    """``record`` with its quantities multiplied by ``factor``, for a billing-unit change.

    Status, band and cost are left as they were; :func:`reprice` rebuilds them.
    """
    return replace(
        record,
        metered=record.metered * factor,
        export=_times(record.export, factor),
        netted=_times(record.netted, factor),
        utility=_times(record.utility, factor),
        utility_export=_times(record.utility_export, factor),
        bill=None
        if record.bill is None
        else replace(record.bill, quantity=_times(record.bill.quantity, factor)),
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
