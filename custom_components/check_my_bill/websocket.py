"""Read-only WebSocket commands for the card (docs/specs/services-api.md §4)."""

from __future__ import annotations

from datetime import date
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinator import CheckMyBillConfigEntry, CheckMyBillCoordinator
from .core.cycles import Cycle
from .core.record import CycleRecord, cost_to_dict

MAX_RANGE_DAYS = 400


@callback
def async_setup_websocket(hass: HomeAssistant) -> None:
    """Register the commands once for the domain."""
    websocket_api.async_register_command(hass, ws_cycles)
    websocket_api.async_register_command(hass, ws_range)


def _record(record: CycleRecord) -> dict[str, Any]:
    return {
        **record.to_dict(),
        "estimated": record.estimated,
        "last_day": record.cycle.last_day.isoformat(),
    }


def _coordinator(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> CheckMyBillCoordinator | None:
    entry: CheckMyBillConfigEntry | None = hass.config_entries.async_get_entry(msg["entry_id"])
    if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
        connection.send_error(
            msg["id"], websocket_api.ERR_NOT_FOUND, "Entry not found or not loaded"
        )
        return None
    return entry.runtime_data


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/cycles", vol.Required("entry_id"): cv.string}
)
@callback
def ws_cycles(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The current cycle and the closed cycles, newest first."""
    if (coordinator := _coordinator(hass, connection, msg)) is None:
        return
    data = coordinator.data
    projected = data.projected_cost
    connection.send_result(
        msg["id"],
        {
            "currency": hass.config.currency,
            "export_mode": coordinator.export_mode.value,
            "tolerance_pct": round(coordinator.tolerance * 100, 4),
            "has_tariff": coordinator.tariff is not None,
            "current": {
                **_record(data.current),
                "projected_cost": None if projected is None else cost_to_dict(projected),
            },
            "cycles": [_record(record) for record in data.history],
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/range",
        vol.Required("entry_id"): cv.string,
        vol.Required("start"): cv.date,
        vol.Required("end"): cv.date,
    }
)
@websocket_api.async_response
async def ws_range(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Any date range, ``end`` exclusive, computed like a cycle."""
    if (coordinator := _coordinator(hass, connection, msg)) is None:
        return
    start: date = msg["start"]
    end: date = msg["end"]
    if not start < end or (end - start).days > MAX_RANGE_DAYS:
        connection.send_error(
            msg["id"],
            websocket_api.ERR_INVALID_FORMAT,
            f"start must be before end, at most {MAX_RANGE_DAYS} days apart",
        )
        return
    record = await coordinator.async_range(Cycle(start, end))
    result = _record(record)
    del result["source"], result["final"], result["estimated"]
    connection.send_result(msg["id"], result)
