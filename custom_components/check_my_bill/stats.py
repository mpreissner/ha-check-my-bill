"""Read statistic change series from the recorder and total them per cycle (spec §3.4)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Literal

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant

from .core.compute import CycleTotals, compute_totals, hour_starts
from .core.cycles import Cycle

type Changes = dict[str, dict[datetime, float]]


@dataclass(frozen=True, slots=True)
class StatIds:
    """The configured statistic IDs. Only the import statistic is required."""

    imports: str
    exports: str | None = None
    utility: str | None = None
    utility_exports: str | None = None

    def configured(self) -> set[str]:
        """Every configured ID."""
        return {s for s in (self.imports, self.exports, self.utility, self.utility_exports) if s}


def _as_utc(value: float | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return datetime.fromtimestamp(value, UTC)


async def fetch_changes(
    hass: HomeAssistant,
    ids: Iterable[str],
    start: datetime,
    end: datetime,
    period: Literal["hour", "day"],
) -> Changes:
    """``change`` per slot start (UTC) for each ID, in kWh. Missing slots are absent."""
    wanted = set(ids)
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        wanted,
        period,
        {"energy": "kWh"},
        {"change"},
    )
    result: Changes = {}
    for statistic_id in wanted:
        series: dict[datetime, float] = {}
        for row in rows.get(statistic_id, []):
            change = row.get("change")
            if change is not None:
                series[_as_utc(row["start"])] = float(change)
        result[statistic_id] = series
    return result


def day_starts(start: datetime, end: datetime) -> list[datetime]:
    """UTC instants of each local midnight in ``[start, end)``; ``start`` sets the zone."""
    tz = start.tzinfo
    day = start.date()
    slots = []
    while (midnight := datetime.combine(day, time.min, tzinfo=tz)) < end:
        if midnight >= start:
            slots.append(midnight.astimezone(UTC))
        day += timedelta(days=1)
    return slots


def _totals(ids: StatIds, slots: list[datetime], changes: Changes, *, hourly: bool) -> CycleTotals:
    def series(statistic_id: str | None) -> dict[datetime, float] | None:
        return None if statistic_id is None else changes.get(statistic_id, {})

    return compute_totals(
        slots,
        changes.get(ids.imports, {}),
        series(ids.exports),
        series(ids.utility),
        series(ids.utility_exports),
        hourly=hourly,
    )


def _min_coverage(totals: CycleTotals) -> float:
    cov = totals.coverage
    return min(
        c for c in (cov.imports, cov.exports, cov.utility, cov.utility_exports) if c is not None
    )


HOURLY_ENOUGH = 0.98
"""Hourly coverage at which a closed cycle keeps its hourly totals."""


async def cycle_totals(
    hass: HomeAssistant,
    ids: StatIds,
    cycle: Cycle,
    tz: tzinfo,
    until: datetime | None = None,
) -> CycleTotals:
    """Totals for ``cycle``, or for its part before ``until`` while it is open.

    Hourly data is preferred. For a closed cycle with many gaps, daily buckets are tried too:
    Opower imports older history as one row per day, which hourly slots cannot cover.
    Daily totals carry no per-hour netting, so a few missing hours (a restart; the next
    hour's change carries their energy anyway) do not trigger the fallback.
    """
    start, end = cycle.local_bounds(tz)
    closed = until is None or until >= end
    if until is not None and not closed:
        end = max(start, until)

    slots = hour_starts(start, end)
    hourly = _totals(
        ids, slots, await fetch_changes(hass, ids.configured(), start, end, "hour"), hourly=True
    )
    if not closed or not slots or _min_coverage(hourly) >= HOURLY_ENOUGH:
        return hourly

    daily_changes = await fetch_changes(hass, ids.configured(), start, end, "day")
    daily = _totals(ids, day_starts(start, end), daily_changes, hourly=False)
    return daily if _min_coverage(daily) > _min_coverage(hourly) else hourly


async def earliest_data(
    hass: HomeAssistant, ids: StatIds, since: datetime, until: datetime, tz: tzinfo
) -> date | None:
    """The first local day with import or utility data after ``since``."""
    wanted = {s for s in (ids.imports, ids.utility) if s}
    changes = await fetch_changes(hass, wanted, since, until, "day")
    firsts = [min(series) for series in changes.values() if series]
    return min(firsts).astimezone(tz).date() if firsts else None
