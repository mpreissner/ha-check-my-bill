"""Replay a sanitized snapshot of a real site (``tools/sanitize.py``) through the coordinator.

The fixture covers one closed cycle and the current cycle to date, in America/New_York, across
the end of DST, with a one-hour gap in the HA statistics.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_import_statistics,
)
from homeassistant.config_entries import ConfigEntryState, ConfigFlow
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_config_flow,
    mock_platform,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.check_my_bill.const import (
    CONF_CYCLE_END_ENTITY,
    CONF_CYCLE_START_ENTITY,
    CONF_EXPORT_MODE,
    CONF_EXPORT_STAT,
    CONF_IMPORT_STAT,
    CONF_TARIFF,
    CONF_UTILITY_EXPORT_STAT,
    CONF_UTILITY_IMPORT_STAT,
    DOMAIN,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "one_closed_cycle.json").read_text())
START_ENTITY = "sensor.test_current_bill_start_date"
END_ENTITY = "sensor.test_current_bill_end_date"
TARIFF = [{"effective_from": "2020-01-01", "supply": 0.11, "delivery": 0.07, "fixed_monthly": 8.0}]


class _PlaceholderFlow(ConfigFlow):
    VERSION = 1


@pytest.fixture(autouse=True)
async def placeholder_flow(hass: HomeAssistant) -> AsyncIterator[None]:
    """The config flow lands in phase 3; HA refuses entries of a domain without one."""
    await async_get_integration(hass, DOMAIN)
    mock_platform(hass, f"{DOMAIN}.config_flow", None)
    with mock_config_flow(DOMAIN, _PlaceholderFlow):
        yield


def _rows(changes: list[float | None]) -> list[dict[str, Any]]:
    start = datetime.fromisoformat(FIXTURE["start"])
    rows, total = [], 0.0
    for index, change in enumerate(changes):
        if change is None:
            continue
        total += change
        rows.append({"start": start + timedelta(hours=index), "sum": total, "state": total})
    return rows


async def _seed(hass: HomeAssistant) -> None:
    for statistic_id, data in FIXTURE["statistics"].items():
        meta: Any = {
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": None,
            "source": data["source"],
            "statistic_id": statistic_id,
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        }
        add = (
            async_add_external_statistics
            if data["source"] != "recorder"
            else async_import_statistics
        )
        add(hass, meta, _rows(data["change"]))
    await async_wait_recording_done(hass)
    for entity_id, value in FIXTURE["states"].items():
        hass.states.async_set(entity_id, value)


def _total(statistic_id: str, first: datetime, last: datetime) -> float:
    start = datetime.fromisoformat(FIXTURE["start"])
    return sum(
        change
        for index, change in enumerate(FIXTURE["statistics"][statistic_id]["change"])
        if change is not None and first <= start + timedelta(hours=index) < last
    )


async def test_replay_real_site(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await hass.config.async_set_time_zone(FIXTURE["time_zone"])
    tz = ZoneInfo(FIXTURE["time_zone"])
    freezer.move_to(datetime.fromisoformat(FIXTURE["taken_at"]) + timedelta(minutes=30))
    await _seed(hass)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_IMPORT_STAT: "sensor.test_grid_import",
            CONF_EXPORT_STAT: "sensor.test_grid_export",
            CONF_UTILITY_IMPORT_STAT: "opower:test_elec_0000_energy_consumption",
            CONF_UTILITY_EXPORT_STAT: "opower:test_elec_0000_energy_return",
            CONF_CYCLE_START_ENTITY: START_ENTITY,
            CONF_CYCLE_END_ENTITY: END_ENTITY,
        },
        options={CONF_TARIFF: TARIFF, CONF_EXPORT_MODE: "none"},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    data = entry.runtime_data.data
    utc = datetime.fromisoformat
    local = lambda *a: datetime(*a, tzinfo=tz).astimezone(UTC)  # noqa: E731  UTC math spans DST

    # The closed cycle is backfilled from the first day with data, priced on the utility's
    # figures, and lands inside the band: the meter reads between HA's netted and gross
    # import (spec §2.3). The HA gap on Sep 22 is carried by the next hour's change.
    (closed,) = data.history
    assert (closed.start.isoformat(), closed.end.isoformat()) == ("2026-09-19", "2026-10-19")
    assert closed.source == "backfill"
    assert closed.final
    first, last = local(2026, 9, 19), local(2026, 10, 19)
    assert closed.metered_kwh == pytest.approx(_total("sensor.test_grid_import", first, last))
    assert closed.utility_kwh == pytest.approx(
        _total("opower:test_elec_0000_energy_consumption", first, last)
    )
    assert closed.coverage.imports == 1.0
    assert closed.netted_kwh is not None
    assert closed.netted_kwh < closed.metered_kwh < closed.utility_kwh
    assert closed.status == "ok"
    assert closed.cost_basis == "utility"
    assert closed.cost is not None
    assert closed.cost.total == pytest.approx(closed.utility_kwh * 0.18 + 8.0)

    # The current cycle comes from the utility's dates, with Opower about two days behind.
    current = data.current
    assert (current.start.isoformat(), current.end.isoformat()) == ("2026-10-19", "2026-11-20")
    assert not current.estimated
    now_hour = utc(FIXTURE["taken_at"])
    assert current.metered_kwh == pytest.approx(
        _total("sensor.test_grid_import", local(2026, 10, 19), now_hour)
    )
    assert current.coverage.utility is not None
    assert 0.9 < current.coverage.utility < 1.0
    assert current.status == "in_progress"
    assert current.cost_basis == "meter"

    # The projection scales the metered kWh to the whole 32-day cycle.
    elapsed = (now_hour - local(2026, 10, 19)).total_seconds() / 3600
    whole = (local(2026, 11, 20) - local(2026, 10, 19)).total_seconds() / 3600
    assert data.projected_cost is not None
    assert data.projected_cost.total == pytest.approx(
        current.metered_kwh * whole / elapsed * 0.18 + 8.0, rel=1e-6
    )
