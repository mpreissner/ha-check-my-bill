"""Gas entries end to end: unit conversion, daily slots and sensors (docs/specs/gas.md)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_import_statistics,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import VolumeConverter
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.check_my_bill.const import (
    CONF_CYCLE_DAY,
    CONF_IMPORT_STAT,
    CONF_METER,
    CONF_TARIFF,
    CONF_UNIT,
    CONF_UTILITY_IMPORT_STAT,
    CONF_UTILITY_UNIT,
    DOMAIN,
)

from .test_coordinator import ORIGIN, hours, local, state

METER = "sensor.test_gas_meter"
UTILITY = "opower:test_gas_0000_energy_consumption"
NOW = local(2026, 11, 10, 12, 30)
FT3_PER_HOUR = 10.0
"""0.1 CCF, which is 0.1 thm."""
THM_PER_HOUR = 0.104
"""The utility's data: in therms, though Opower labels it CCF."""
M3_PER_THM = VolumeConverter.convert(1, "CCF", "m³")
"""1 thm = 1 CCF."""
TARIFF = [{"effective_from": "2020-01-01", "supply": 0.6, "delivery": 0.4, "fixed_monthly": 15.0}]

# Oct 3 - Nov 3 crosses the end of DST: 745 hours.
LAST_CYCLE_HOURS = 745


def _meta(statistic_id: str, source: str, unit: str) -> Any:
    return {
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
        "name": None,
        "source": source,
        "statistic_id": statistic_id,
        "unit_class": "volume",
        "unit_of_measurement": unit,
    }


async def _seed(hass: HomeAssistant) -> None:
    """Hourly meter rows to now; one utility row per local day, two days behind."""
    meter = [
        {"start": h, "sum": (i + 1) * FT3_PER_HOUR, "state": (i + 1) * FT3_PER_HOUR}
        for i, h in enumerate(hours(ORIGIN, local(2026, 11, 10, 12)))
    ]
    async_import_statistics(hass, _meta(METER, "recorder", "ft³"), meter)

    daily, total, day = [], 0.0, ORIGIN
    while day < local(2026, 11, 8):
        following = (day + timedelta(days=1)).replace(hour=0)
        total += len(hours(day, following)) * THM_PER_HOUR
        daily.append({"start": day, "sum": total, "state": total})
        day = following
    async_add_external_statistics(hass, _meta(UTILITY, "opower", "CCF"), daily)
    await async_wait_recording_done(hass)


async def _setup(hass: HomeAssistant, **data: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Gas",
        data={
            CONF_METER: "gas",
            CONF_IMPORT_STAT: METER,
            CONF_UTILITY_IMPORT_STAT: UTILITY,
            CONF_UTILITY_UNIT: "thm",
            CONF_CYCLE_DAY: 3,
            **data,
        },
        options={CONF_TARIFF: TARIFF},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def seeded(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(NOW)
    await _seed(hass)


@pytest.mark.usefixtures("seeded")
async def test_therms_from_cubic_feet_and_utility_therms(hass: HomeAssistant) -> None:
    entry = await _setup(hass, **{CONF_UNIT: "thm"})
    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    assert (coordinator.meter, coordinator.unit) == ("gas", "thm")
    assert coordinator.export_mode == "none"

    # Whole days only: Nov 3 - Nov 9.
    cycle_import = state(hass, entry, "cycle_import")
    assert float(cycle_import.state) == pytest.approx(7 * 24 * 0.1, rel=1e-6)
    assert cycle_import.attributes["unit_of_measurement"] == "thm"
    assert cycle_import.attributes["device_class"] == "energy"

    # The daily utility rows cover the closed cycle in full.
    last = coordinator.data.last_closed
    assert last is not None
    assert last.status == "ok"
    assert last.metered == pytest.approx(LAST_CYCLE_HOURS * 0.1, rel=1e-6)
    assert last.utility == pytest.approx(LAST_CYCLE_HOURS * THM_PER_HOUR, rel=1e-6)
    expected = state(hass, entry, "last_cycle_expected_cost")
    assert float(expected.state) == pytest.approx(LAST_CYCLE_HOURS * THM_PER_HOUR + 15, abs=0.01)


@pytest.mark.usefixtures("seeded")
async def test_billed_by_volume(hass: HomeAssistant) -> None:
    entry = await _setup(hass, **{CONF_UNIT: "CCF"})
    cycle_import = state(hass, entry, "cycle_import")
    assert float(cycle_import.state) == pytest.approx(7 * 24 * 0.1, rel=1e-6)
    assert cycle_import.attributes["unit_of_measurement"] == "CCF"
    assert cycle_import.attributes["device_class"] == "gas"

    last = entry.runtime_data.data.last_closed
    assert last is not None
    assert last.utility == pytest.approx(LAST_CYCLE_HOURS * THM_PER_HOUR, rel=1e-6)


@pytest.mark.usefixtures("seeded")
async def test_changing_the_billing_unit_converts_history(hass: HomeAssistant) -> None:
    entry = await _setup(hass, **{CONF_UNIT: "thm"})
    before = entry.runtime_data.data.history
    assert all(r.final for r in before)

    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_UNIT: "m³"})
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    after = entry.runtime_data.data.history
    assert [r.start for r in after] == [r.start for r in before]
    for old, new in zip(before, after, strict=True):
        assert new.metered == pytest.approx(old.metered * M3_PER_THM, rel=1e-6)
        assert new.utility == pytest.approx(old.utility * M3_PER_THM, rel=1e-6)


async def test_entry_without_a_meter_is_electric(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Entries made before gas support carry no meter or unit."""
    freezer.move_to(NOW)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_IMPORT_STAT: "sensor.test_grid_import", CONF_CYCLE_DAY: 3},
        options={CONF_TARIFF: []},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert (coordinator.meter, coordinator.unit, coordinator.daily_only) == (
        "electric",
        "kWh",
        False,
    )
    assert state(hass, entry, "cycle_import").attributes["unit_of_measurement"] == "kWh"
