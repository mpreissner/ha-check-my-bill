"""The ``set_cycle_dates`` and ``recompute`` actions (docs/specs/services-api.md §3)."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service

from .const import DOMAIN
from .coordinator import CheckMyBillConfigEntry, CheckMyBillCoordinator

SERVICE_SET_CYCLE_DATES = "set_cycle_dates"
SERVICE_RECOMPUTE = "recompute"
ATTR_CYCLE_START = "cycle_start"
ATTR_START = "start"
ATTR_END = "end"

_ENTRY = {vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string}

SET_CYCLE_DATES_SCHEMA = vol.Schema(
    {
        **_ENTRY,
        vol.Required(ATTR_CYCLE_START): cv.date,
        vol.Required(ATTR_START): cv.date,
        vol.Required(ATTR_END): cv.date,
    }
)
RECOMPUTE_SCHEMA = vol.Schema({**_ENTRY, vol.Optional(ATTR_CYCLE_START): cv.date})


def _coordinator(call: ServiceCall) -> CheckMyBillCoordinator:
    entry: CheckMyBillConfigEntry = service.async_get_config_entry(
        call.hass, DOMAIN, call.data.get(ATTR_CONFIG_ENTRY_ID)
    )
    return entry.runtime_data


async def _set_cycle_dates(call: ServiceCall) -> None:
    await _coordinator(call).async_set_cycle_dates(
        call.data[ATTR_CYCLE_START], call.data[ATTR_START], call.data[ATTR_END]
    )


async def _recompute(call: ServiceCall) -> None:
    await _coordinator(call).async_recompute(call.data.get(ATTR_CYCLE_START))


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions once for the domain."""
    hass.services.async_register(
        DOMAIN, SERVICE_SET_CYCLE_DATES, _set_cycle_dates, schema=SET_CYCLE_DATES_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_RECOMPUTE, _recompute, schema=RECOMPUTE_SCHEMA)
