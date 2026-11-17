"""Setup, backfill, rollover and finalization against a real recorder."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_import_statistics,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.check_my_bill.const import (
    CONF_CYCLE_DAY,
    CONF_CYCLE_END_ENTITY,
    CONF_CYCLE_START_ENTITY,
    CONF_EXPORT_MODE,
    CONF_IMPORT_STAT,
    CONF_TARIFF,
    CONF_UTILITY_IMPORT_STAT,
    DOMAIN,
)

TZ = ZoneInfo("US/Pacific")  # the pytest-homeassistant default
IMPORT = "sensor.test_grid_import"
UTILITY = "opower:test_elec_0000_energy_consumption"
START_ENTITY = "sensor.test_current_bill_start_date"
END_ENTITY = "sensor.test_current_bill_end_date"
TARIFF = [{"effective_from": "2020-01-01", "supply": 0.10, "delivery": 0.05, "fixed_monthly": 9.0}]

HA_PER_HOUR = 1.0
UTILITY_PER_HOUR = 0.99


def local(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)  # type: ignore[misc]


def hours(start: datetime, end: datetime) -> list[datetime]:
    current = start.astimezone(ZoneInfo("UTC"))
    out = []
    while current < end:
        out.append(current)
        current += timedelta(hours=1)
    return out


def _rows(
    start: datetime, end: datetime, per_hour: float, origin: datetime
) -> list[dict[str, Any]]:
    """Cumulative rows; ``origin`` fixes where the running sum starts, so slices line up."""
    offset = len(hours(origin, start))
    return [
        {"start": h, "sum": (offset + i + 1) * per_hour, "state": (offset + i + 1) * per_hour}
        for i, h in enumerate(hours(start, end))
    ]


ORIGIN = local(2026, 8, 3)


def _meta(statistic_id: str, source: str) -> Any:
    return {
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
        "name": None,
        "source": source,
        "statistic_id": statistic_id,
        "unit_class": "energy",
        "unit_of_measurement": "kWh",
    }


async def seed_import(hass: HomeAssistant, start: datetime, end: datetime) -> None:
    async_import_statistics(hass, _meta(IMPORT, "recorder"), _rows(start, end, HA_PER_HOUR, ORIGIN))
    await async_wait_recording_done(hass)


async def seed_utility(hass: HomeAssistant, start: datetime, end: datetime) -> None:
    async_add_external_statistics(
        hass, _meta(UTILITY, "opower"), _rows(start, end, UTILITY_PER_HOUR, ORIGIN)
    )
    await async_wait_recording_done(hass)


def entity_id(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    found = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
    assert found is not None, key
    return found


def state(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> Any:
    current = hass.states.get(entity_id(hass, entry, key))
    assert current is not None
    return current


async def setup_entry(hass: HomeAssistant, data: dict[str, Any]) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Check My Bill",
        data={CONF_IMPORT_STAT: IMPORT, CONF_UTILITY_IMPORT_STAT: UTILITY, **data},
        options={CONF_TARIFF: TARIFF, CONF_EXPORT_MODE: "none"},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_manual_cycle_backfills_history(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    freezer.move_to(local(2026, 11, 10, 12, 30))
    await seed_import(hass, ORIGIN, local(2026, 11, 10, 12))
    await seed_utility(hass, ORIGIN, local(2026, 11, 8))

    entry = await setup_entry(hass, {CONF_CYCLE_DAY: 3})
    assert entry.state is ConfigEntryState.LOADED

    assert state(hass, entry, "cycle_start").state == "2026-11-03"
    assert state(hass, entry, "cycle_end").state == "2026-12-02"
    assert state(hass, entry, "cycle_status").state == "in_progress"
    assert float(state(hass, entry, "cycle_import").state) == pytest.approx(7 * 24 + 12)
    assert state(hass, entry, "cycle_start").attributes["estimated_dates"] is True

    # Seven and a half days at 1 kWh/h, priced at $0.15, plus 8 of 30 days of $9.
    expected = state(hass, entry, "cycle_expected_cost")
    assert float(expected.state) == pytest.approx(180 * 0.15 + 9 * 8 / 30, abs=0.01)
    assert expected.attributes["unit_of_measurement"] == hass.config.currency
    projected = float(state(hass, entry, "cycle_projected_cost").state)
    assert projected == pytest.approx(30 * 24 * 0.15 + 9, abs=0.5)

    # Oct 3 - Nov 3 crosses the end of DST: 745 hours. Utility basis, 0.99 kWh/h.
    last = state(hass, entry, "last_cycle_expected_cost")
    assert float(last.state) == pytest.approx(745 * 0.99 * 0.15 + 9, abs=0.01)
    assert last.attributes["status"] == "ok"
    assert last.attributes["start"] == "2026-10-03"
    assert last.attributes["end"] == "2026-11-02"

    history = entry.runtime_data.data.history
    assert [r.start.isoformat() for r in history] == ["2026-10-03", "2026-09-03", "2026-08-03"]
    assert all(r.source == "backfill" and r.final for r in history)

    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]
    assert stored["current"] == {"start": "2026-11-03", "end": "2026-12-03", "source": "manual"}
    assert len(stored["cycles"]) == 3

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert state(hass, entry, "cycle_status").state == STATE_UNAVAILABLE


async def test_utility_rollover_then_finalizes(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(local(2026, 11, 1, 12, 30))
    hass.states.async_set(START_ENTITY, "2026-10-03")
    hass.states.async_set(END_ENTITY, "2026-11-02")
    await seed_import(hass, ORIGIN, local(2026, 11, 7))
    await seed_utility(hass, ORIGIN, local(2026, 10, 31))  # Opower lags about two days

    entry = await setup_entry(
        hass, {CONF_CYCLE_START_ENTITY: START_ENTITY, CONF_CYCLE_END_ENTITY: END_ENTITY}
    )
    coordinator = entry.runtime_data
    assert [r.start.isoformat() for r in coordinator.data.history] == ["2026-09-03", "2026-08-03"]
    assert state(hass, entry, "cycle_start").attributes["estimated_dates"] is False

    # The utility issues the bill; its dates move to the next cycle.
    freezer.move_to(local(2026, 11, 3, 10))
    hass.states.async_set(START_ENTITY, "2026-11-03")
    hass.states.async_set(END_ENTITY, "2026-12-02")
    await hass.async_block_till_done()
    # Opower updates the two sensors one after the other; the debouncer runs the second
    # refresh once its cooldown passes.
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    closed = coordinator.data.last_closed
    assert closed is not None
    assert (closed.start.isoformat(), closed.end.isoformat()) == ("2026-10-03", "2026-11-03")
    assert closed.source == "utility"
    assert not closed.final
    assert closed.status == "no_data"
    assert state(hass, entry, "cycle_start").state == "2026-11-03"

    # The utility's data catches up; three days after the cycle ends it is frozen.
    await seed_utility(hass, local(2026, 10, 31), local(2026, 11, 5))
    freezer.move_to(local(2026, 11, 6, 1))
    await coordinator.async_refresh()

    closed = coordinator.data.last_closed
    assert closed is not None
    assert closed.final
    assert closed.status == "ok"
    assert closed.utility == pytest.approx(745 * UTILITY_PER_HOUR)


async def test_setup_retries_without_a_cycle(hass: HomeAssistant) -> None:
    entry = await setup_entry(
        hass, {CONF_CYCLE_START_ENTITY: START_ENTITY, CONF_CYCLE_END_ENTITY: END_ENTITY}
    )
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_reload_keeps_history_without_backfilling_again(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(local(2026, 11, 10, 12, 30))
    await seed_import(hass, ORIGIN, local(2026, 11, 10, 12))
    entry = await setup_entry(hass, {CONF_CYCLE_DAY: 3})
    before = entry.runtime_data.data.history
    assert len(before) == 3
    assert all(r.status == "no_data" for r in before)  # utility statistic has no rows

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.data.history == before


async def _reconfigure(hass: HomeAssistant, entry: MockConfigEntry, data: dict[str, Any]) -> None:
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()


async def test_reconfigured_statistics_discard_history(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(local(2026, 11, 10, 12, 30))
    await seed_import(hass, ORIGIN, local(2026, 11, 10, 12))
    other = "sensor.test_other_import"
    async_import_statistics(
        hass, _meta(other, "recorder"), _rows(ORIGIN, local(2026, 11, 10, 12), 2.0, ORIGIN)
    )
    await async_wait_recording_done(hass)
    entry = await setup_entry(hass, {CONF_CYCLE_DAY: 3})
    before = entry.runtime_data.data.history
    cycle_hours = len(hours(local(2026, 10, 3), local(2026, 11, 3)))  # 745: DST ends Nov 1
    assert before[0].metered == pytest.approx(cycle_hours)

    await _reconfigure(
        hass, entry, {CONF_IMPORT_STAT: other, CONF_UTILITY_IMPORT_STAT: UTILITY, CONF_CYCLE_DAY: 3}
    )
    after = entry.runtime_data.data.history
    assert len(after) == len(before)
    assert after[0].metered == pytest.approx(2 * cycle_hours)  # rebuilt from the new meter


async def test_reconfigured_cycle_keeps_history(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(local(2026, 11, 10, 12, 30))
    await seed_import(hass, ORIGIN, local(2026, 11, 10, 12))
    entry = await setup_entry(hass, {CONF_CYCLE_DAY: 3})
    before = entry.runtime_data.data.history

    await _reconfigure(
        hass,
        entry,
        {CONF_IMPORT_STAT: IMPORT, CONF_UTILITY_IMPORT_STAT: UTILITY, CONF_CYCLE_DAY: 4},
    )
    assert entry.data[CONF_CYCLE_DAY] == 4
    # The move closes Nov 3 as a one-day cycle; the older records are kept, not rebuilt.
    stub, *kept = entry.runtime_data.data.history
    assert (stub.start.isoformat(), stub.end.isoformat()) == ("2026-11-03", "2026-11-04")
    assert kept == list(before)
