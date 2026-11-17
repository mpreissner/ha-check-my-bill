"""Hourly refresh: find the current cycle, close and finalize past ones, price it all."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta

from homeassistant.components.recorder.statistics import async_list_statistic_ids
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import issues
from .const import (
    BACKFILL_MAX_CYCLES,
    BILL_FETCH_INTERVAL,
    CONF_CYCLE_DAY,
    CONF_CYCLE_END_ENTITY,
    CONF_CYCLE_START_ENTITY,
    CONF_EXPORT_MODE,
    CONF_EXPORT_STAT,
    CONF_IMPORT_STAT,
    CONF_METER,
    CONF_TARIFF,
    CONF_TOLERANCE,
    CONF_UNIT,
    CONF_UTILITY_EXPORT_STAT,
    CONF_UTILITY_IMPORT_STAT,
    CONF_UTILITY_UNIT,
    DEFAULT_GAS_TOLERANCE_PCT,
    DEFAULT_TOLERANCE_PCT,
    DOMAIN,
    UPDATE_INTERVAL,
)
from .core.bills import merge_bills
from .core.compute import project
from .core.cycles import Cycle, add_months, backfill, from_cycle_day, from_utility_dates, is_final
from .core.record import (
    BilledAmounts,
    CycleRecord,
    Source,
    build_record,
    closed_by_rollover,
    reprice,
    rescale,
)
from .core.serde import tariff_from_options
from .core.tariff import CostBreakdown, ExportMode, cost
from .core.units import KWH, Meter, scale
from .opower_bills import BillFetchError, async_fetch_bills
from .stats import ENERGY, VOLUME, StatIds, cycle_totals, earliest_data
from .store import CycleStore, StoredState

_LOGGER = logging.getLogger(__name__)

type CheckMyBillConfigEntry = ConfigEntry[CheckMyBillCoordinator]


@dataclass(frozen=True, slots=True)
class BillData:
    """What the sensors read."""

    current: CycleRecord
    """The open cycle, costed to date."""
    projected_cost: CostBreakdown | None
    history: tuple[CycleRecord, ...]
    """Closed cycles, newest first."""

    @property
    def last_closed(self) -> CycleRecord | None:
        """The most recently closed cycle."""
        return self.history[0] if self.history else None


def _state_date(hass: HomeAssistant, entity_id: str) -> date | None:
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        return date.fromisoformat(state.state[:10])
    except ValueError:
        return None


class CheckMyBillCoordinator(DataUpdateCoordinator[BillData]):
    """Keeps the cycle history in the Store and the current cycle fresh."""

    config_entry: CheckMyBillConfigEntry

    def __init__(self, hass: HomeAssistant, entry: CheckMyBillConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        data, options = entry.data, entry.options
        self.meter = Meter(data.get(CONF_METER, Meter.ELECTRIC))
        self.unit: str = data.get(CONF_UNIT, KWH)
        self.utility_unit: str | None = data.get(CONF_UTILITY_UNIT)
        self.ids = StatIds(
            imports=data[CONF_IMPORT_STAT],
            exports=data.get(CONF_EXPORT_STAT),
            utility=data.get(CONF_UTILITY_IMPORT_STAT),
            utility_exports=data.get(CONF_UTILITY_EXPORT_STAT),
        )
        gas = self.meter is Meter.GAS
        self.export_mode = (
            ExportMode.NONE if gas else ExportMode(options.get(CONF_EXPORT_MODE) or ExportMode.NONE)
        )
        self.tariff = tariff_from_options(options.get(CONF_TARIFF), self.export_mode)
        default_tolerance = DEFAULT_GAS_TOLERANCE_PCT if gas else DEFAULT_TOLERANCE_PCT
        self.tolerance = float(options.get(CONF_TOLERANCE, default_tolerance)) / 100
        self.daily_only = gas
        """Gas is compared by day (docs/specs/gas.md §4)."""
        self.store = CycleStore(hass, entry.entry_id)
        self._state: StoredState | None = None
        self._lock = asyncio.Lock()
        """Serialises the refresh and the actions that edit the history."""
        self._bills_fetched: datetime | None = None
        self._bill_error_logged = False

    @callback
    def async_track_cycle_entities(self) -> None:
        """Refresh as soon as the utility's billing dates change, not at the next hour."""
        entities = [
            e
            for e in (
                self.config_entry.data.get(CONF_CYCLE_START_ENTITY),
                self.config_entry.data.get(CONF_CYCLE_END_ENTITY),
            )
            if e
        ]
        if not entities:
            return

        @callback
        def _changed(event: Event[EventStateChangedData]) -> None:
            self.hass.async_create_task(self.async_request_refresh())

        self.config_entry.async_on_unload(
            async_track_state_change_event(self.hass, entities, _changed)
        )

    def _configured_cycle(self, today: date) -> tuple[Cycle, Source] | None:
        data = self.config_entry.data
        if start_entity := data.get(CONF_CYCLE_START_ENTITY):
            start = _state_date(self.hass, start_entity)
            end = _state_date(self.hass, data.get(CONF_CYCLE_END_ENTITY, ""))
            if start is None or end is None or end < start:
                return None
            return from_utility_dates(start, end), Source.UTILITY
        if cycle_day := data.get(CONF_CYCLE_DAY):
            return from_cycle_day(today, int(cycle_day)), Source.MANUAL
        return None

    async def _closed_record(
        self, cycle: Cycle, source: Source, today: date, bill: BilledAmounts | None = None
    ) -> CycleRecord:
        tz = dt_util.get_default_time_zone()
        totals = await cycle_totals(self.hass, self.ids, cycle, tz, daily_only=self.daily_only)
        return build_record(
            cycle,
            totals,
            source=source,
            final=is_final(cycle, today),
            closed=True,
            tariff=self.tariff,
            export_mode=self.export_mode,
            tolerance=self.tolerance,
            bill=bill,
        )

    async def _backfill(self, state: StoredState, current: Cycle, now: datetime) -> None:
        tz = dt_util.get_default_time_zone()
        since = datetime.combine(
            add_months(current.start, -BACKFILL_MAX_CYCLES), time.min, tzinfo=tz
        )
        first_day = await earliest_data(self.hass, self.ids, since, now, tz)
        for cycle in backfill(current, BACKFILL_MAX_CYCLES, first_day):
            state.cycles.append(await self._closed_record(cycle, Source.BACKFILL, now.date()))
        _LOGGER.debug("Backfilled %d cycles from %s", len(state.cycles), first_day)

    async def _merge_bills(self, state: StoredState, current: Cycle, now: datetime) -> None:
        """Replace estimated history with Opower's bill periods, at most once a day."""
        if self._bills_fetched is not None and now - self._bills_fetched < BILL_FETCH_INTERVAL:
            return
        tz = dt_util.get_default_time_zone()
        since = datetime.combine(
            add_months(current.start, -(BACKFILL_MAX_CYCLES + 1)), time.min, tzinfo=tz
        )
        try:
            bills = await async_fetch_bills(self.hass, self.ids.utility, since)
        except BillFetchError as err:
            if not self._bill_error_logged:
                _LOGGER.warning("Could not read past bills from Opower, keeping estimates: %s", err)
                self._bill_error_logged = True
            return
        if bills is None:
            return
        self._bills_fetched, self._bill_error_logged = now, False
        if (factor := self.ids.scale.get(self.ids.utility or "", 1.0)) != 1.0:
            # Bills are in the account's unit, like the utility statistic (gas.md §3.3).
            bills = [
                replace(b, quantity=None if b.quantity is None else b.quantity * factor)
                for b in bills
            ]

        first_day = await earliest_data(self.hass, self.ids, since, now, tz)
        if first_day is None:
            return
        result = merge_bills(
            state.cycles,
            [b for b in bills if b.end > first_day],
            current.start,
            BACKFILL_MAX_CYCLES,
        )
        added = [
            await self._closed_record(b.cycle, Source.UTILITY, now.date(), bill=b.amounts)
            for b in result.compute
        ]
        state.cycles = sorted([*result.keep, *added], key=lambda r: r.start, reverse=True)
        _LOGGER.debug("Merged %d bills from Opower, %d computed", len(bills), len(added))

    async def _async_update_data(self) -> BillData:
        async with self._lock:
            data = await self._update()
        recent = [data.current, *data.history[:1]]
        await issues.async_check(self.hass, self.config_entry, self.ids, self.export_mode, recent)
        return data

    def _scale(self, from_unit: str | None) -> float | None:
        return scale(from_unit, self.unit, volume=VOLUME, energy=ENERGY)

    async def _update_scales(self, state: StoredState) -> None:
        """Scale each statistic from its own unit to the billing unit (gas.md §3.1).

        A statistic whose unit differs from the one stored last time raises a repair issue.
        """
        configured = self.ids.configured()
        listed = await async_list_statistic_ids(self.hass, statistic_ids=configured)
        units: dict[str, str | None] = {
            s["statistic_id"]: s.get("statistics_unit_of_measurement") for s in listed
        }
        factors: dict[str, float] = {}
        for statistic_id in configured:
            if statistic_id not in units:
                continue  # missing_statistic covers it
            unit = units[statistic_id]
            if statistic_id == self.ids.utility and self.utility_unit:
                unit = self.utility_unit
            factor = self._scale(unit)
            if factor is None:
                _LOGGER.warning(
                    "No conversion from %s (%s) to %s; reading it unscaled",
                    statistic_id,
                    unit,
                    self.unit,
                )
                factor = 1.0
            factors[statistic_id] = factor
        self.ids = replace(self.ids, scale=factors)

        changed = [
            s
            for s, unit in units.items()
            if state.stat_units is not None
            and state.stat_units.get(s, unit) is not None
            and state.stat_units.get(s, unit) != unit
        ]
        if changed:
            _LOGGER.warning("Statistic units changed: %s", changed)
            issues.async_unit_changed(self.hass, self.config_entry, changed)
        state.stat_units = {**(state.stat_units or {}), **units}

    def _convert_history(self, state: StoredState) -> StoredState:
        """Stored records in the entry's billing unit, after a change of unit (gas.md §2.1)."""
        old = state.unit or KWH
        if old == self.unit or not state.cycles:
            state.unit = self.unit
            return state
        factor = self._scale(old)
        if factor is None:
            _LOGGER.info("Billing unit changed from %s to %s; discarding history", old, self.unit)
            return StoredState(unit=self.unit)
        _LOGGER.info("Billing unit changed from %s to %s; converting history", old, self.unit)
        # Non-final records are recomputed from the statistics anyway.
        state.cycles = [rescale(r, factor) for r in state.cycles]
        state.unit = self.unit
        return state

    async def _update(self) -> BillData:
        now = dt_util.now()
        today = now.date()
        if self._state is None:
            self._state = await self.store.async_load()
            sources = self.ids.sources()
            if self._state.sources not in (None, sources):
                # Reconfigured onto other statistics: the stored records describe other meters.
                _LOGGER.info("Statistics changed; discarding stored cycle history")
                self._state = StoredState()
            self._state.sources = sources
            self._state = self._convert_history(self._state)
            # Settings may have changed since the records were built (spec services-api §2).
            self._state.cycles = [self._reprice(r) for r in self._state.cycles]
        state = self._state
        await self._update_scales(state)

        found = self._configured_cycle(today)
        if found is not None:
            current, source = found
        elif state.current is not None and state.current_source is not None:
            # The utility's date sensors are briefly unavailable after a restart or re-auth.
            current, source = state.current, state.current_source
        else:
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="no_cycle")

        if state.current is None and not state.cycles:
            await self._backfill(state, current, now)
        elif (closed := closed_by_rollover(state.current, current)) is not None:
            old_source = state.current_source or Source.MANUAL
            record = await self._closed_record(closed, old_source, today)
            state.cycles = [r for r in state.cycles if r.start != closed.start]
            state.cycles.insert(0, record)
            _LOGGER.debug("Cycle %s closed: %s", closed, record.status)

        await self._merge_bills(state, current, now)

        # Opower data lags; closed cycles are recomputed until they are final.
        for index, record in enumerate(state.cycles):
            if not record.final:
                state.cycles[index] = await self._closed_record(
                    record.cycle, record.source, today, record.bill
                )

        state.current, state.current_source = current, source
        current_record, projected = await self._current_record(current, source, now)
        await self.store.async_save(state)
        return BillData(current_record, projected, tuple(state.cycles))

    async def _current_record(
        self, current: Cycle, source: Source, now: datetime
    ) -> tuple[CycleRecord, CostBreakdown | None]:
        tz = dt_util.get_default_time_zone()
        if self.daily_only:
            # Whole days only: today has no utility data yet.
            until = datetime.combine(now.date(), time.min, tzinfo=tz)
            cost_until = now.date()
        else:
            until = now.replace(minute=0, second=0, microsecond=0)
            cost_until = now.date() + timedelta(days=1)
        totals = await cycle_totals(
            self.hass, self.ids, current, tz, until, daily_only=self.daily_only
        )
        record = build_record(
            current,
            totals,
            source=source,
            final=False,
            closed=False,
            tariff=self.tariff,
            export_mode=self.export_mode,
            tolerance=self.tolerance,
            cost_until=cost_until,
        )
        if self.tariff is None:
            return record, None

        # Same-tzinfo subtraction is wall-clock time; UTC counts the DST hour.
        start, end = (bound.astimezone(UTC) for bound in current.local_bounds(tz))
        total_hours = (end - start).total_seconds() / 3600
        elapsed = max(0.0, (min(until.astimezone(UTC), end) - start).total_seconds() / 3600)
        elapsed *= totals.coverage.imports
        projected_import = project(totals.metered, elapsed, total_hours)
        if projected_import is None:
            return record, None
        projected_export = project(totals.export or 0.0, elapsed, total_hours) or 0.0
        projected = cost(
            self.tariff,
            current.start,
            current.end,
            projected_import,
            projected_export,
            period_days=current.days,
        )
        return record, projected

    def _reprice(self, record: CycleRecord) -> CycleRecord:
        return reprice(
            record, tariff=self.tariff, export_mode=self.export_mode, tolerance=self.tolerance
        )

    def _loaded_state(self) -> StoredState:
        if self._state is None:  # the entry sets up only after a first successful refresh
            raise RuntimeError("history not loaded")
        return self._state

    @staticmethod
    def _index(state: StoredState, cycle_start: date) -> int:
        for index, record in enumerate(state.cycles):
            if record.start == cycle_start:
                return index
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="cycle_not_found",
            translation_placeholders={"date": cycle_start.isoformat()},
        )

    async def async_set_cycle_dates(self, cycle_start: date, start: date, last_day: date) -> None:
        """Move a closed cycle's estimated boundaries to the dates on the bill."""
        if last_day < start:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_dates")
        new = Cycle(start, last_day + timedelta(days=1))
        async with self._lock:
            state = self._loaded_state()
            index = self._index(state, cycle_start)
            if state.cycles[index].source is Source.UTILITY:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="cycle_not_editable"
                )
            others = (r for i, r in enumerate(state.cycles) if i != index)
            overlaps = any(r.start < new.end and new.start < r.end for r in others)
            if overlaps or (state.current is not None and new.end > state.current.start):
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="cycle_overlap"
                )
            today = dt_util.now().date()
            state.cycles[index] = await self._closed_record(new, Source.USER, today)
            state.cycles.sort(key=lambda r: r.start, reverse=True)
            await self.store.async_save(state)
        await self.async_refresh()

    async def async_recompute(self, cycle_start: date | None = None) -> None:
        """Read the statistics again for one closed cycle, or all of them."""
        async with self._lock:
            state = self._loaded_state()
            if cycle_start is None:
                indexes: list[int] = list(range(len(state.cycles)))
            else:
                indexes = [self._index(state, cycle_start)]
            today = dt_util.now().date()
            for index in indexes:
                record = state.cycles[index]
                state.cycles[index] = await self._closed_record(
                    record.cycle, record.source, today, record.bill
                )
            await self.store.async_save(state)
        await self.async_refresh()

    async def async_range(self, cycle: Cycle) -> CycleRecord:
        """Any date range computed like a cycle; in progress if it has not ended yet."""
        tz = dt_util.get_default_time_zone()
        now = dt_util.now()
        closed = cycle.local_bounds(tz)[1] <= now
        until = None if closed else now.replace(minute=0, second=0, microsecond=0)
        totals = await cycle_totals(
            self.hass, self.ids, cycle, tz, until, daily_only=self.daily_only
        )
        return build_record(
            cycle,
            totals,
            source=Source.MANUAL,
            final=closed,
            closed=closed,
            tariff=self.tariff,
            export_mode=self.export_mode,
            tolerance=self.tolerance,
            cost_until=None if closed else now.date() + timedelta(days=1),
        )
