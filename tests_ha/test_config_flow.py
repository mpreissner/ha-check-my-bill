"""Config flow, reconfigure and options flow (docs/specs/config-flow.md §5)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant import config_entries
from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.check_my_bill.config_flow import cycle_day_from
from custom_components.check_my_bill.const import (
    CONF_CYCLE_DAY,
    CONF_CYCLE_END_ENTITY,
    CONF_CYCLE_START_ENTITY,
    CONF_EXPORT_MODE,
    CONF_EXPORT_STAT,
    CONF_IMPORT_STAT,
    CONF_TARIFF,
    CONF_TOLERANCE,
    CONF_UTILITY_EXPORT_STAT,
    CONF_UTILITY_IMPORT_STAT,
    DOMAIN,
)
from custom_components.check_my_bill.discovery import (
    GridPair,
    grid_pairs,
    key_from_statistic,
    key_from_unique_id,
)

IMPORT = "sensor.test_grid_import"
EXPORT = "sensor.test_grid_export"
CONSUMPTION = "opower:test_elec_0000_energy_consumption"
RETURN = "opower:test_elec_0000_energy_return"
GAS = "opower:test_gas_0000_energy_consumption"
START = "sensor.test_current_bill_start_date"
END = "sensor.test_current_bill_end_date"
TODAY = date(2026, 11, 10)
RATES = {"supply": 0.11, "delivery": 0.07, "fixed_monthly": 8.0}


@pytest.fixture(autouse=True)
def no_setup() -> Iterator[AsyncMock]:
    """The flow is under test, not the coordinator."""
    with patch(
        "custom_components.check_my_bill.async_setup_entry", AsyncMock(return_value=True)
    ) as setup:
        yield setup


@pytest.fixture
def reload_opower(hass: HomeAssistant) -> Iterator[MagicMock]:
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        yield reload


@pytest.fixture(autouse=True)
def today(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to("2026-11-10 12:00:00-08:00")


async def _energy_prefs(hass: HomeAssistant) -> None:
    manager = await async_get_manager(hass)
    manager.data = {  # type: ignore[assignment]
        "energy_sources": [{"type": "grid", "stat_energy_from": IMPORT, "stat_energy_to": EXPORT}],
        "device_consumption": [],
    }


async def _opower(
    hass: HomeAssistant, disabled_by: er.RegistryEntryDisabler | None
) -> MockConfigEntry:
    """An Opower entry with electric and gas statistics and its two date sensors."""
    entry = MockConfigEntry(domain="opower", title="Test Utility (user)")
    entry.add_to_hass(hass)
    for statistic_id in (CONSUMPTION, RETURN, GAS):
        meta: Any = {
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": None,
            "source": "opower",
            "statistic_id": statistic_id,
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        }
        async_add_external_statistics(
            hass, meta, [{"start": datetime(2026, 11, 1, tzinfo=UTC), "sum": 1.0, "state": 1.0}]
        )
    await async_wait_recording_done(hass)
    registry = er.async_get(hass)
    for unique_id, object_id in (
        ("test_0000_elec_start_date", "test_current_bill_start_date"),
        ("test_0000_elec_end_date", "test_current_bill_end_date"),
    ):
        registry.async_get_or_create(
            "sensor",
            "opower",
            unique_id,
            config_entry=entry,
            suggested_object_id=object_id,
            disabled_by=disabled_by,
        )
    return entry


async def _start(hass: HomeAssistant) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


def _defaults(result: dict[str, Any]) -> dict[str, Any]:
    """Each field's suggested value or default, as the form would pre-fill it."""
    out = {}
    for marker in result["data_schema"].schema:
        suggested = (marker.description or {}).get("suggested_value")
        default = marker.default() if callable(marker.default) else None
        out[str(marker)] = suggested if suggested is not None else default
    return out


# --- discovery ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statistic_id", "key"),
    [
        ("opower:bge_elec_123_energy_consumption", "bge_123"),
        ("opower:pge_elec_abc_def_energy_consumption", "pge_abc_def"),
        ("opower:bge_gas_123_energy_consumption", None),
        ("opower:bge_elec_123_energy_return", None),
        ("sensor:bge_elec_123_energy_consumption", None),
    ],
)
def test_key_from_statistic(statistic_id: str, key: str | None) -> None:
    assert key_from_statistic(statistic_id) == key


@pytest.mark.parametrize(
    ("unique_id", "found"),
    [
        ("bge_123_elec_start_date", ("bge_123", "start")),
        ("PGE_abc-def_elec_end_date", ("pge_abc_def", "end")),
        ("bge_123_elec_usage_to_date", None),
    ],
)
def test_key_from_unique_id(unique_id: str, found: tuple[str, str] | None) -> None:
    assert key_from_unique_id(unique_id) == found


def test_grid_pairs_reads_both_layouts() -> None:
    legacy = {
        "type": "grid",
        "flow_from": [{"stat_energy_from": "a"}, {"stat_energy_from": "b"}],
        "flow_to": [{"stat_energy_to": "c"}],
    }
    flat = {"type": "grid", "stat_energy_from": "d", "stat_energy_to": None}
    prefs = {"energy_sources": [{"type": "solar", "stat_energy_from": "x"}, legacy, flat]}
    assert grid_pairs(prefs) == [GridPair("a", "c"), GridPair("b", None), GridPair("d", None)]
    assert grid_pairs(None) == []


def test_cycle_day_from_last_bill_end() -> None:
    assert cycle_day_from(date(2026, 10, 18)) == 19
    assert cycle_day_from(date(2026, 10, 31)) == 1
    assert cycle_day_from(date(2026, 10, 28)) == 28  # 29th clamps to 28


# --- config flow -------------------------------------------------------------------------


async def test_opower_with_disabled_date_sensors(
    hass: HomeAssistant, reload_opower: MagicMock
) -> None:
    await _energy_prefs(hass)
    opower = await _opower(hass, er.RegistryEntryDisabler.INTEGRATION)

    result = await _start(hass)
    assert result["step_id"] == "user"
    assert _defaults(result) == {CONF_IMPORT_STAT: IMPORT, CONF_EXPORT_STAT: EXPORT}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_STAT: IMPORT, CONF_EXPORT_STAT: EXPORT}
    )
    assert result["step_id"] == "utility"
    assert _defaults(result) == {"account": CONSUMPTION, "enable_date_sensors": True}
    options = result["data_schema"].schema["account"].config["options"]
    assert [o["value"] for o in options] == [CONSUMPTION, "none"]  # gas is not offered

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"account": CONSUMPTION, "enable_date_sensors": True}
    )
    assert result["step_id"] == "tariff"
    registry = er.async_get(hass)
    assert registry.async_get(START).disabled_by is None
    assert registry.async_get(END).disabled_by is None
    reload_opower.assert_not_called()  # only once the entry exists

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**RATES, CONF_EXPORT_MODE: "net_metering"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Check My Bill (Test Utility (user))"
    assert result["data"] == {
        CONF_IMPORT_STAT: IMPORT,
        CONF_EXPORT_STAT: EXPORT,
        CONF_UTILITY_IMPORT_STAT: CONSUMPTION,
        CONF_UTILITY_EXPORT_STAT: RETURN,
        CONF_CYCLE_START_ENTITY: START,
        CONF_CYCLE_END_ENTITY: END,
    }
    options = result["options"]
    assert options[CONF_EXPORT_MODE] == "net_metering"
    assert options[CONF_TOLERANCE] == 2.0
    (version,) = options[CONF_TARIFF]
    assert version["effective_from"] == TODAY.isoformat()
    assert (version["supply"], version["delivery"], version["fixed_monthly"]) == (0.11, 0.07, 8.0)
    assert result["result"].unique_id == CONSUMPTION
    reload_opower.assert_called_once_with(opower.entry_id)


async def test_declining_the_date_sensors_goes_to_cycle(
    hass: HomeAssistant, reload_opower: MagicMock
) -> None:
    await _opower(hass, er.RegistryEntryDisabler.INTEGRATION)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_STAT: IMPORT}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"account": CONSUMPTION, "enable_date_sensors": False}
    )
    assert result["step_id"] == "cycle"
    assert er.async_get(hass).async_get(START).disabled_by is er.RegistryEntryDisabler.INTEGRATION

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CYCLE_DAY: 19})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_IMPORT_STAT: IMPORT,
        CONF_UTILITY_IMPORT_STAT: CONSUMPTION,
        CONF_UTILITY_EXPORT_STAT: RETURN,
        CONF_CYCLE_DAY: 19,
    }
    reload_opower.assert_not_called()


async def test_without_opower_grid_cycle_tariff(hass: HomeAssistant) -> None:
    result = await _start(hass)
    assert _defaults(result) == {CONF_IMPORT_STAT: None, CONF_EXPORT_STAT: None}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_STAT: IMPORT}
    )
    assert result["step_id"] == "cycle"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "cycle_one"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CYCLE_DAY: 3, "last_bill_end": "2026-10-18"}
    )
    assert result["errors"] == {"base": "cycle_one"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"last_bill_end": "2026-10-18"}
    )
    assert result["step_id"] == "tariff"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"supply": 0.11, CONF_EXPORT_MODE: "none"}
    )
    assert result["errors"] == {"base": "rates_both"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EXPORT_MODE: "none"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Check My Bill"
    assert result["data"] == {CONF_IMPORT_STAT: IMPORT, CONF_CYCLE_DAY: 19}
    assert result["options"][CONF_TARIFF] == []
    assert result["result"].unique_id == IMPORT


async def test_skipping_utility_data_goes_to_cycle(hass: HomeAssistant) -> None:
    await _opower(hass, None)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_STAT: IMPORT}
    )
    assert "enable_date_sensors" not in _defaults(result)  # nothing to enable
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"account": "none"})
    assert result["step_id"] == "cycle"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CYCLE_DAY: 3})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], RATES)
    assert result["data"] == {CONF_IMPORT_STAT: IMPORT, CONF_CYCLE_DAY: 3}
    assert result["title"] == "Check My Bill"


async def test_user_disabled_date_sensors_are_left_alone(
    hass: HomeAssistant, reload_opower: MagicMock
) -> None:
    await _opower(hass, er.RegistryEntryDisabler.USER)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_STAT: IMPORT}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"account": CONSUMPTION}
    )
    assert result["step_id"] == "cycle"
    assert er.async_get(hass).async_get(START).disabled_by is er.RegistryEntryDisabler.USER
    reload_opower.assert_not_called()


async def test_duplicate_account_aborts(hass: HomeAssistant) -> None:
    await _opower(hass, None)
    MockConfigEntry(domain=DOMAIN, unique_id=CONSUMPTION, data={}).add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_STAT: IMPORT}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"account": CONSUMPTION}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], RATES)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- reconfigure -------------------------------------------------------------------------


def _entry(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=CONSUMPTION,
        data={
            CONF_IMPORT_STAT: IMPORT,
            CONF_UTILITY_IMPORT_STAT: CONSUMPTION,
            CONF_CYCLE_START_ENTITY: START,
            CONF_CYCLE_END_ENTITY: END,
        },
        options={CONF_TARIFF: [], CONF_EXPORT_MODE: "none", CONF_TOLERANCE: 2.0, **options},
    )
    entry.add_to_hass(hass)
    return entry


async def test_reconfigure(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    assert _defaults(result)[CONF_CYCLE_START_ENTITY] == START

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_STAT: IMPORT, CONF_CYCLE_START_ENTITY: START}
    )
    assert result["errors"] == {"base": "cycle_pair"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_STAT: IMPORT}
    )
    assert result["errors"] == {"base": "cycle_one"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_STAT: EXPORT, CONF_CYCLE_DAY: 5}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert dict(entry.data) == {CONF_IMPORT_STAT: EXPORT, CONF_CYCLE_DAY: 5}
    assert entry.unique_id == EXPORT


async def test_reconfigure_onto_another_entrys_meter_aborts(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    MockConfigEntry(domain=DOMAIN, unique_id=EXPORT, data={}).add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_STAT: EXPORT, CONF_CYCLE_DAY: 5}
    )
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_IMPORT_STAT] == IMPORT


# --- options -----------------------------------------------------------------------------


async def _options(hass: HomeAssistant, entry: MockConfigEntry, step: str) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


SEASONAL = {
    "effective_from": "2026-01-01",
    "supply": {"default": 0.11, "seasonal": [{"months": [6, 7, 8, 9], "rate": 0.13}]},
    "delivery": 0.07,
    "tiers": [{"up_to": 500, "supply_rate": None, "delivery_rate": 0.05}],
    "riders": [{"name": "EmPOWER", "per_unit": 0.004}],
    "fixed_monthly": 8.0,
}


async def test_rates_adds_a_version_and_carries_over_extras(hass: HomeAssistant) -> None:
    entry = _entry(hass, **{CONF_TARIFF: [SEASONAL]})
    result = await _options(hass, entry, "rates")
    defaults = _defaults(result)
    assert defaults["effective_from"] == TODAY.isoformat()
    assert (defaults["supply"], defaults["delivery"]) == (0.11, 0.07)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"effective_from": "2026-11-01", "supply": 0.11, "delivery": 0.08, "fixed_monthly": 8.0},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    old, new = entry.options[CONF_TARIFF]
    assert old["effective_from"] == "2026-01-01"
    assert new["effective_from"] == "2026-11-01"
    assert new["supply"] == SEASONAL["supply"]  # unchanged default keeps its summer rate
    assert new["delivery"] == 0.08
    assert new["tiers"] == SEASONAL["tiers"]
    assert new["riders"] == SEASONAL["riders"]


async def test_rates_replaces_a_same_day_version(hass: HomeAssistant) -> None:
    entry = _entry(hass, **{CONF_TARIFF: [SEASONAL]})
    result = await _options(hass, entry, "rates")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"effective_from": "2026-01-01", "supply": 0.12, "delivery": 0.07}
    )
    (version,) = entry.options[CONF_TARIFF]
    assert version["supply"] == 0.12  # a changed default drops the seasonal override
    assert version["fixed_monthly"] == 0.0


async def test_rates_needs_both_rates(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    result = await _options(hass, entry, "rates")
    assert _defaults(result)["supply"] is None
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"effective_from": "2026-11-01", "delivery": 0.07}
    )
    assert result["errors"] == {"base": "rates_both"}


@pytest.mark.parametrize(
    "versions",
    [
        {"effective_from": "2026-01-01"},
        [{"effective_from": "2026-01-01", "supply": 0.1}],
        [{"effective_from": "not a date", "supply": 0.1, "delivery": 0.1}],
        ["just text"],
    ],
)
async def test_rate_versions_rejects_bad_yaml(hass: HomeAssistant, versions: Any) -> None:
    entry = _entry(hass)
    result = await _options(hass, entry, "rate_versions")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"versions": versions}
    )
    assert result["errors"] == {"base": "invalid_tariff"}


async def test_rate_versions_saves_and_clears(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    result = await _options(hass, entry, "rate_versions")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"versions": [SEASONAL]}
    )
    (version,) = entry.options[CONF_TARIFF]
    assert version["riders"] == SEASONAL["riders"]

    result = await _options(hass, entry, "rate_versions")
    assert _defaults(result)["versions"] == [version]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"versions": []})
    assert entry.options[CONF_TARIFF] == []


async def test_settings(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    result = await _options(hass, entry, "settings")
    assert _defaults(result) == {CONF_TOLERANCE: 2.0, CONF_EXPORT_MODE: "none"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TOLERANCE: 3.5, CONF_EXPORT_MODE: "separate_rate"}
    )
    assert entry.options[CONF_TOLERANCE] == 3.5
    assert entry.options[CONF_EXPORT_MODE] == "separate_rate"
    assert entry.options[CONF_TARIFF] == []
