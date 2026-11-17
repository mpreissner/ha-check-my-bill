"""Compare the utility's metered quantity against what HA measured.

A meter that nets export against import within sub-hourly intervals reads somewhere
between HA's hourly-netted import and its gross import, so with export the check is a
band rather than a point. See docs/specs/bill-validation.md §3.5.
"""

from dataclasses import dataclass
from enum import StrEnum

from .compute import CycleTotals
from .tariff import ExportMode

DEFAULT_TOLERANCE = 0.02
CALIBRATION_MAX_EXPORT = 0.01
"""A cycle with less export than this counts as export-free for the calibration ratio."""


class Status(StrEnum):
    """Outcome of comparing the utility quantity with HA's measurement."""

    OK = "ok"
    WARN = "warn"
    MISMATCH = "mismatch"
    NO_DATA = "no_data"
    IN_PROGRESS = "in_progress"


@dataclass(frozen=True, slots=True)
class Band:
    """An inclusive quantity range."""

    low: float
    high: float

    def contains(self, value: float) -> bool:
        """Whether ``value`` lies inside the band."""
        return self.low <= value <= self.high

    def deviation_pct(self, value: float) -> float:
        """Signed % distance from the nearest edge; zero inside the band."""
        if value < self.low:
            return (value - self.low) / self.low * 100 if self.low else float("-inf")
        if value > self.high:
            return (value - self.high) / self.high * 100 if self.high else float("inf")
        return 0.0


def band(totals: CycleTotals, mode: ExportMode, tolerance: float) -> Band:
    """The range the utility's quantity should fall in."""
    export = totals.export or 0.0
    if mode is ExportMode.NET_METERING:
        center = max(0.0, totals.metered - export)
        return Band(center * (1 - tolerance), center * (1 + tolerance))
    low = totals.netted if totals.netted is not None else totals.metered
    return Band(low * (1 - tolerance), totals.metered * (1 + tolerance))


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """Result for one cycle."""

    status: Status
    band: Band | None
    utility: float | None
    deviation_pct: float | None


def reconcile(
    totals: CycleTotals,
    mode: ExportMode,
    *,
    final: bool,
    tolerance: float = DEFAULT_TOLERANCE,
) -> Reconciliation:
    """Classify a cycle.

    ``final`` says the cycle has closed and its data should be complete. Incomplete data
    in an open cycle is ``in_progress``; in a closed one it is ``no_data``. Incomplete
    data never produces ``mismatch``.
    """
    if not 0 <= tolerance < 1:
        raise ValueError(f"tolerance must be in [0, 1), got {tolerance}")
    if totals.utility is None:
        return Reconciliation(Status.NO_DATA, None, None, None)

    expected = band(totals, mode, tolerance)
    # Missing export hours count as zero, which inflates the netted low edge (and the
    # net-metering center), so export completeness matters in every mode.
    complete = totals.coverage.imports == 1.0 and totals.coverage.utility == 1.0
    if totals.coverage.exports is not None:
        complete = complete and totals.coverage.exports == 1.0
    if not complete:
        status = Status.NO_DATA if final else Status.IN_PROGRESS
        return Reconciliation(status, expected, totals.utility, None)

    value = totals.utility
    if expected.contains(value):
        status = Status.OK
    elif band(totals, mode, min(2 * tolerance, 0.99)).contains(value):
        status = Status.WARN
    else:
        status = Status.MISMATCH
    return Reconciliation(status, expected, value, expected.deviation_pct(value))


def calibration_ratio(totals: CycleTotals) -> float | None:
    """utility quantity / HA quantity for an export-free cycle with complete data, else None.

    A stable ratio away from 1.0 points at CT or meter calibration, not at the bill.
    """
    if totals.utility is None or totals.metered <= 0:
        return None
    if totals.coverage.imports != 1.0 or totals.coverage.utility != 1.0:
        return None
    if (totals.export or 0.0) > CALIBRATION_MAX_EXPORT:
        return None
    return totals.utility / totals.metered
