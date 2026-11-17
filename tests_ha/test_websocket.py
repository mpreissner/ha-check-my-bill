"""The check_my_bill/cycles and check_my_bill/range commands (services-api.md §4)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.check_my_bill.const import CONF_CYCLE_DAY, DOMAIN

from .test_coordinator import ORIGIN, local, seed_import, seed_utility, setup_entry

NOW = local(2026, 11, 10, 12, 30)


@pytest.fixture
def issued(hass_access_token: str) -> datetime:
    """The real time the access token was created at, before any test moves the clock."""
    return dt_util.utcnow() + timedelta(seconds=1)


@pytest.fixture
async def entry_id(hass: HomeAssistant, freezer: FrozenDateTimeFactory, issued: datetime) -> str:
    freezer.move_to(NOW)
    await seed_import(hass, ORIGIN, local(2026, 11, 10, 12))
    await seed_utility(hass, ORIGIN, local(2026, 11, 8))
    entry = await setup_entry(hass, {CONF_CYCLE_DAY: 3})
    return entry.entry_id


@pytest.fixture
async def client(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    freezer: FrozenDateTimeFactory,
    issued: datetime,
    entry_id: str,
) -> Any:
    """Connected after setup, since the HTTP router freezes once a client connects.

    Authentication runs at the real time the auth fixtures were created at; a token checked
    before its refresh token was issued is rejected.
    """
    freezer.move_to(issued)
    connected = await hass_ws_client(hass)
    freezer.move_to(NOW)
    return connected


async def test_cycles(hass: HomeAssistant, client: Any, entry_id: str) -> None:
    await client.send_json_auto_id({"type": f"{DOMAIN}/cycles", "entry_id": entry_id})
    msg = await client.receive_json()
    assert msg["success"], msg
    result = msg["result"]

    assert result["currency"] == hass.config.currency
    assert (result["meter"], result["unit"]) == ("electric", "kWh")
    assert result["export_mode"] == "none"
    assert result["tolerance_pct"] == 2.0
    assert result["has_tariff"] is True

    current = result["current"]
    assert (current["start"], current["end"], current["last_day"]) == (
        "2026-11-03",
        "2026-12-03",
        "2026-12-02",
    )
    assert current["status"] == "in_progress"
    assert current["estimated"] is True
    assert current["projected_cost"]["total"] == pytest.approx(30 * 24 * 0.15 + 9, abs=0.5)

    cycles = result["cycles"]
    assert [c["start"] for c in cycles] == ["2026-10-03", "2026-09-03", "2026-08-03"]
    assert cycles[0]["last_day"] == "2026-11-02"
    assert all(c["source"] == "backfill" and c["estimated"] for c in cycles)
    assert cycles[0]["status"] == "ok"
    assert cycles[0]["expected_cost"]["total"] > 0


async def test_unknown_entry(hass: HomeAssistant, client: Any, entry_id: str) -> None:
    commands = [
        {"type": f"{DOMAIN}/cycles"},
        {"type": f"{DOMAIN}/range", "start": "2026-10-03", "end": "2026-11-03"},
    ]
    for command in commands:
        await client.send_json_auto_id({**command, "entry_id": "nope"})
        msg = await client.receive_json()
        assert not msg["success"]
        assert msg["error"]["code"] == "not_found"


async def test_range_matches_the_stored_cycle(
    hass: HomeAssistant, client: Any, entry_id: str
) -> None:
    stored = hass.config_entries.async_get_entry(entry_id).runtime_data.data.history[0]  # type: ignore[union-attr]
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/range",
            "entry_id": entry_id,
            "start": "2026-10-03",
            "end": "2026-11-03",
        }
    )
    msg = await client.receive_json()
    assert msg["success"], msg

    expected = stored.to_dict()
    del expected["source"], expected["final"]
    expected["last_day"] = "2026-11-02"
    expected |= {"meter": "electric", "unit": "kWh"}
    assert msg["result"] == expected


async def test_range_in_progress(hass: HomeAssistant, client: Any, entry_id: str) -> None:
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/range",
            "entry_id": entry_id,
            "start": "2026-11-01",
            "end": "2026-11-15",
        }
    )
    msg = await client.receive_json()
    assert msg["success"], msg
    assert msg["result"]["status"] == "in_progress"
    assert "source" not in msg["result"]


@pytest.mark.parametrize(
    ("start", "end"),
    [("2026-10-03", "2026-10-03"), ("2026-10-03", "2026-10-01"), ("2025-01-01", "2026-11-01")],
)
async def test_invalid_range(
    hass: HomeAssistant,
    client: Any,
    entry_id: str,
    start: str,
    end: str,
) -> None:
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/range", "entry_id": entry_id, "start": start, "end": end}
    )
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_format"
