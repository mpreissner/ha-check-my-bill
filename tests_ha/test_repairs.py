"""The export_detected and missing_statistic issues (services-api.md §5)."""

from __future__ import annotations

from datetime import datetime

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.check_my_bill.const import (
    CONF_CYCLE_DAY,
    CONF_EXPORT_MODE,
    CONF_IMPORT_STAT,
    CONF_TARIFF,
    CONF_UTILITY_EXPORT_STAT,
    CONF_UTILITY_IMPORT_STAT,
    DOMAIN,
)

from .test_coordinator import (
    IMPORT,
    ORIGIN,
    TARIFF,
    UTILITY,
    _meta,
    _reconfigure,
    _rows,
    local,
    seed_import,
    seed_utility,
    setup_entry,
)

UTILITY_RETURN = "opower:test_elec_0000_energy_return"


async def seed_return(hass: HomeAssistant, start: datetime, end: datetime, per_hour: float) -> None:
    async_add_external_statistics(
        hass, _meta(UTILITY_RETURN, "opower"), _rows(start, end, per_hour, ORIGIN)
    )
    await async_wait_recording_done(hass)


def issue(hass: HomeAssistant, kind: str, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{kind}_{entry.entry_id}")


async def _seeded(hass: HomeAssistant, freezer: FrozenDateTimeFactory, per_hour: float) -> None:
    freezer.move_to(local(2026, 11, 10, 12, 30))
    await seed_import(hass, ORIGIN, local(2026, 11, 10, 12))
    await seed_utility(hass, ORIGIN, local(2026, 11, 8))
    await seed_return(hass, ORIGIN, local(2026, 11, 8), per_hour)


async def test_export_detected_without_net_metering(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await _seeded(hass, freezer, 0.05)
    entry = await setup_entry(hass, {CONF_CYCLE_DAY: 3, CONF_UTILITY_EXPORT_STAT: UTILITY_RETURN})

    found = issue(hass, "export_detected", entry)
    assert found is not None
    assert found.severity is ir.IssueSeverity.WARNING
    assert not found.is_fixable
    assert found.translation_placeholders == {"title": entry.title}
    assert issue(hass, "missing_statistic", entry) is None

    # Switching to net metering clears it.
    hass.config_entries.async_update_entry(
        entry, options={CONF_TARIFF: TARIFF, CONF_EXPORT_MODE: "net_metering"}
    )
    await hass.async_block_till_done()
    assert issue(hass, "export_detected", entry) is None


async def test_no_export_issue_for_zero_return(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await _seeded(hass, freezer, 0.0)
    entry = await setup_entry(hass, {CONF_CYCLE_DAY: 3, CONF_UTILITY_EXPORT_STAT: UTILITY_RETURN})
    assert issue(hass, "export_detected", entry) is None


async def test_no_export_issue_with_net_metering(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await _seeded(hass, freezer, 0.05)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Check My Bill",
        data={
            CONF_IMPORT_STAT: IMPORT,
            CONF_UTILITY_IMPORT_STAT: UTILITY,
            CONF_UTILITY_EXPORT_STAT: UTILITY_RETURN,
            CONF_CYCLE_DAY: 3,
        },
        options={CONF_TARIFF: TARIFF, CONF_EXPORT_MODE: "net_metering"},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert issue(hass, "export_detected", entry) is None


async def test_missing_statistic(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await _seeded(hass, freezer, 0.0)
    missing = "opower:test_elec_0000_typo"
    entry = await setup_entry(hass, {CONF_CYCLE_DAY: 3, CONF_UTILITY_EXPORT_STAT: missing})

    found = issue(hass, "missing_statistic", entry)
    assert found is not None
    assert found.translation_placeholders == {"title": entry.title, "statistics": missing}

    await _reconfigure(
        hass,
        entry,
        {
            CONF_IMPORT_STAT: IMPORT,
            CONF_UTILITY_IMPORT_STAT: UTILITY,
            CONF_UTILITY_EXPORT_STAT: UTILITY_RETURN,
            CONF_CYCLE_DAY: 3,
        },
    )
    assert issue(hass, "missing_statistic", entry) is None


async def test_issues_cleared_on_unload(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await _seeded(hass, freezer, 0.05)
    entry = await setup_entry(
        hass, {CONF_CYCLE_DAY: 3, CONF_UTILITY_EXPORT_STAT: "opower:test_elec_0000_typo"}
    )
    assert issue(hass, "missing_statistic", entry) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert issue(hass, "missing_statistic", entry) is None
    assert issue(hass, "export_detected", entry) is None
