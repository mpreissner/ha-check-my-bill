"""Check My Bill: validate utility bills against Energy dashboard statistics.

The design lives in docs/specs/bill-validation.md.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinator import CheckMyBillConfigEntry, CheckMyBillCoordinator
from .store import CycleStore

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: CheckMyBillConfigEntry) -> bool:
    """Backfill or load the history, compute the current cycle, add the sensors."""
    coordinator = CheckMyBillCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_track_cycle_entities()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CheckMyBillConfigEntry) -> bool:
    """Remove the sensors."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: CheckMyBillConfigEntry) -> None:
    """Delete the stored history with the entry."""
    await CycleStore(hass, entry.entry_id).async_remove()


async def _async_reload(hass: HomeAssistant, entry: CheckMyBillConfigEntry) -> None:
    """Options (tariff, tolerance, export mode) change the computation; start over."""
    await hass.config_entries.async_reload(entry.entry_id)
