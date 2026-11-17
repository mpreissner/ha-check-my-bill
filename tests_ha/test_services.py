"""The set_cycle_dates and recompute actions, and repricing on load (services-api.md §2-3)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.check_my_bill.const import (
    CONF_CYCLE_DAY,
    CONF_CYCLE_END_ENTITY,
    CONF_CYCLE_START_ENTITY,
    CONF_EXPORT_MODE,
    CONF_TARIFF,
    CONF_TOLERANCE,
    DOMAIN,
)

from .test_coordinator import (
    END_ENTITY,
    ORIGIN,
    START_ENTITY,
    TARIFF,
    UTILITY_PER_HOUR,
    hours,
    local,
    seed_import,
    seed_utility,
    setup_entry,
    state,
)


async def _backfilled(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> MockConfigEntry:
    """Manual cycles on the 3rd; Oct 3, Sep 3 and Aug 3 backfilled with both meters."""
    freezer.move_to(local(2026, 11, 10, 12, 30))
    await seed_import(hass, ORIGIN, local(2026, 11, 10, 12))
    await seed_utility(hass, ORIGIN, local(2026, 11, 8))
    return await setup_entry(hass, {CONF_CYCLE_DAY: 3})


async def _call(hass: HomeAssistant, service: str, data: dict[str, Any]) -> None:
    await hass.services.async_call(DOMAIN, service, data, blocking=True)
    await hass.async_block_till_done()


def _starts(entry: MockConfigEntry) -> list[str]:
    return [r.start.isoformat() for r in entry.runtime_data.data.history]


async def test_set_cycle_dates_moves_a_backfilled_cycle(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    entry = await _backfilled(hass, freezer)

    await _call(
        hass,
        "set_cycle_dates",
        {
            "config_entry_id": entry.entry_id,
            "cycle_start": "2026-10-03",
            "start": "2026-10-05",
            "end": "2026-11-02",
        },
    )

    edited = entry.runtime_data.data.history[0]
    assert (edited.start, edited.end) == (date(2026, 10, 5), date(2026, 11, 3))
    assert edited.source == "user"
    assert not edited.estimated
    cycle_hours = len(hours(local(2026, 10, 5), local(2026, 11, 3)))
    assert edited.metered == pytest.approx(cycle_hours)
    assert edited.utility == pytest.approx(cycle_hours * UTILITY_PER_HOUR)
    assert _starts(entry) == ["2026-10-05", "2026-09-03", "2026-08-03"]

    last = state(hass, entry, "last_cycle_expected_cost")
    assert last.attributes["start"] == "2026-10-05"
    assert last.attributes["end"] == "2026-11-02"
    assert float(last.state) == pytest.approx(cycle_hours * UTILITY_PER_HOUR * 0.15 + 9, abs=0.01)

    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["cycles"][0]
    assert (stored["start"], stored["end"], stored["source"]) == (
        "2026-10-05",
        "2026-11-03",
        "user",
    )

    # Without config_entry_id the single loaded entry is used; a user record can be edited again.
    await _call(
        hass,
        "set_cycle_dates",
        {"cycle_start": "2026-10-05", "start": "2026-10-04", "end": "2026-11-02"},
    )
    assert _starts(entry)[0] == "2026-10-04"


@pytest.mark.parametrize(
    ("data", "key"),
    [
        (
            {"cycle_start": "2026-10-04", "start": "2026-10-04", "end": "2026-11-02"},
            "cycle_not_found",
        ),
        (
            {"cycle_start": "2026-10-03", "start": "2026-10-10", "end": "2026-10-09"},
            "invalid_dates",
        ),
        (
            {"cycle_start": "2026-10-03", "start": "2026-09-20", "end": "2026-11-02"},
            "cycle_overlap",
        ),
        (
            {"cycle_start": "2026-10-03", "start": "2026-10-03", "end": "2026-11-05"},
            "cycle_overlap",
        ),
    ],
)
async def test_set_cycle_dates_validation(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, data: dict[str, str], key: str
) -> None:
    entry = await _backfilled(hass, freezer)
    before = entry.runtime_data.data.history

    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "set_cycle_dates", data)
    assert err.value.translation_key == key
    assert entry.runtime_data.data.history == before


async def test_set_cycle_dates_rejects_utility_cycles(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(local(2026, 11, 1, 12, 30))
    hass.states.async_set(START_ENTITY, "2026-10-03")
    hass.states.async_set(END_ENTITY, "2026-11-02")
    await seed_import(hass, ORIGIN, local(2026, 11, 4))
    entry = await setup_entry(
        hass, {CONF_CYCLE_START_ENTITY: START_ENTITY, CONF_CYCLE_END_ENTITY: END_ENTITY}
    )
    freezer.move_to(local(2026, 11, 3, 10))
    hass.states.async_set(START_ENTITY, "2026-11-03")
    hass.states.async_set(END_ENTITY, "2026-12-02")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    closed = entry.runtime_data.data.last_closed
    assert closed is not None
    assert closed.source == "utility"

    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass,
            "set_cycle_dates",
            {"cycle_start": "2026-10-03", "start": "2026-10-04", "end": "2026-11-02"},
        )
    assert err.value.translation_key == "cycle_not_editable"


async def test_recompute_reads_late_statistics(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(local(2026, 11, 10, 12, 30))
    await seed_import(hass, ORIGIN, local(2026, 11, 10, 12))
    entry = await setup_entry(hass, {CONF_CYCLE_DAY: 3})
    assert all(r.final and r.status == "no_data" for r in entry.runtime_data.data.history)

    # The utility imports its history after the records became final.
    await seed_utility(hass, ORIGIN, local(2026, 11, 8))
    await entry.runtime_data.async_refresh()
    assert all(r.status == "no_data" for r in entry.runtime_data.data.history)

    await _call(hass, "recompute", {"cycle_start": "2026-10-03"})
    newest, *older = entry.runtime_data.data.history
    assert newest.status == "ok"
    assert newest.utility == pytest.approx(745 * UTILITY_PER_HOUR)  # DST ends Nov 1
    assert all(r.status == "no_data" for r in older)

    await _call(hass, "recompute", {})
    assert all(r.status == "ok" for r in entry.runtime_data.data.history)

    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "recompute", {"cycle_start": "2026-10-04"})
    assert err.value.translation_key == "cycle_not_found"


async def test_options_change_reprices_history(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    entry = await _backfilled(hass, freezer)
    before = entry.runtime_data.data.history[0]
    assert before.status == "ok"  # 1% apart, within the default 2%
    assert before.cost is not None

    doubled = [{**TARIFF[0], "supply": 0.25}]
    hass.config_entries.async_update_entry(
        entry, options={CONF_TARIFF: doubled, CONF_EXPORT_MODE: "none", CONF_TOLERANCE: 0.5}
    )
    await hass.async_block_till_done()

    after = entry.runtime_data.data.history[0]
    assert (after.source, after.final) == (before.source, before.final)
    assert after.metered == before.metered
    assert after.status != "ok"
    assert after.cost is not None
    kwh = 745 * UTILITY_PER_HOUR
    assert after.cost.total == pytest.approx(kwh * 0.30 + 9, abs=0.01)
    assert before.cost.total == pytest.approx(kwh * 0.15 + 9, abs=0.01)


async def test_store_from_earlier_beta_is_migrated(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    """A 1.1 store with kWh-named record keys loads and is rewritten as 1.2 (gas.md §1.1)."""
    entry = await _backfilled(hass, freezer)
    key = f"{DOMAIN}.{entry.entry_id}"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    renames = {
        "metered": "metered_kwh",
        "export": "export_kwh",
        "netted": "netted_kwh",
        "utility": "utility_kwh",
        "utility_export": "utility_export_kwh",
    }
    legacy_cycles = []
    for cycle in hass_storage[key]["data"]["cycles"]:
        old = {renames.get(k, k): v for k, v in cycle.items()}
        if old.get("expected_cost") is not None:
            old["expected_cost"]["billed_kwh"] = old["expected_cost"].pop("billed_quantity")
        legacy_cycles.append(old)
    legacy_cycles[0]["metered_kwh"] = 123.0  # final cycles are not recomputed
    hass_storage[key] = {
        **hass_storage[key],
        "version": 1,
        "minor_version": 1,
        "data": {**hass_storage[key]["data"], "cycles": legacy_cycles},
    }

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    history = entry.runtime_data.data.history
    assert _starts(entry) == ["2026-10-03", "2026-09-03", "2026-08-03"]
    assert history[0].metered == 123.0
    assert history[0].utility is not None
    assert history[0].cost is not None

    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    stored = hass_storage[key]
    assert stored["minor_version"] == 2
    first = stored["data"]["cycles"][0]
    assert first["metered"] == 123.0
    assert "billed_quantity" in first["expected_cost"]
    assert not any(k.endswith("_kwh") for c in stored["data"]["cycles"] for k in c)
