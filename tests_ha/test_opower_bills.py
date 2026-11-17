"""Past bill periods read from the Opower integration (opower-bills.md)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from custom_components.check_my_bill.const import CONF_CYCLE_DAY, DOMAIN

from .test_coordinator import (
    ORIGIN,
    UTILITY_PER_HOUR,
    hours,
    local,
    seed_import,
    seed_utility,
    setup_entry,
)


@dataclass
class Read:
    start_time: datetime
    end_time: datetime
    consumption: float
    provided_cost: float


BILLS = [
    Read(local(2026, 8, 5), local(2026, 9, 3, 23, 59, 59), 700.0, 110.0),
    Read(local(2026, 9, 4), local(2026, 10, 6), 720.0, 112.5),
    Read(local(2026, 10, 6), local(2026, 11, 3), 640.0, 101.25),
]


class FakeApi:
    """The parts of ``opower.Opower`` Check My Bill uses."""

    utility = SimpleNamespace(subdomain=lambda: "test")

    def __init__(self, reads: list[Read] | Exception) -> None:
        self.reads = reads
        self.calls = 0

    async def async_get_cost_reads(self, account: Any, aggregate: Any, *args: Any) -> list[Read]:
        self.calls += 1
        assert aggregate.name == "BILL"
        if isinstance(self.reads, Exception):
            raise self.reads
        return self.reads


def add_opower(hass: HomeAssistant, api: FakeApi) -> None:
    account = SimpleNamespace(meter_type=SimpleNamespace(name="ELEC"), utility_account_id="0000")
    entry = MockConfigEntry(domain="opower", state=ConfigEntryState.LOADED)
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(api=api, data={"0000": SimpleNamespace(account=account)})


async def _seed(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(local(2026, 11, 10, 12, 30))
    await seed_import(hass, ORIGIN, local(2026, 11, 10, 12))
    await seed_utility(hass, ORIGIN, local(2026, 11, 8))


def _periods(entry: MockConfigEntry) -> list[tuple[str, str, str]]:
    return [
        (r.start.isoformat(), r.end.isoformat(), r.source.value)
        for r in entry.runtime_data.data.history
    ]


async def _later(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, entry: MockConfigEntry, delta: timedelta
) -> None:
    """A scheduled refresh ``delta`` from now."""
    freezer.tick(delta)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_bills_replace_backfilled_cycles(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    await _seed(hass, freezer)
    api = FakeApi(BILLS)
    add_opower(hass, api)
    entry = await setup_entry(hass, {CONF_CYCLE_DAY: 3})

    assert _periods(entry) == [
        ("2026-10-06", "2026-11-03", "utility"),
        ("2026-09-04", "2026-10-06", "utility"),
        ("2026-08-05", "2026-09-04", "utility"),
    ]
    newest = entry.runtime_data.data.history[0]
    assert not newest.estimated
    assert newest.bill is not None
    assert (newest.bill.quantity, newest.bill.cost) == (640.0, 101.25)
    cycle_hours = len(hours(local(2026, 10, 6), local(2026, 11, 3)))
    assert newest.metered == pytest.approx(cycle_hours)
    assert newest.utility == pytest.approx(cycle_hours * UTILITY_PER_HOUR)

    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["cycles"][0]
    assert stored["bill"] == {"quantity": 640.0, "cost": 101.25}

    # Once a day, not on every hourly refresh.
    assert api.calls == 1
    await _later(hass, freezer, entry, timedelta(hours=1))
    assert api.calls == 1
    await _later(hass, freezer, entry, timedelta(hours=24))
    assert api.calls == 2
    assert _periods(entry)[0] == ("2026-10-06", "2026-11-03", "utility")


async def test_without_opower_the_estimates_stay(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await _seed(hass, freezer)
    entry = await setup_entry(hass, {CONF_CYCLE_DAY: 3})
    assert entry.state is ConfigEntryState.LOADED
    assert [p[2] for p in _periods(entry)] == ["backfill"] * 3


async def test_failing_api_keeps_estimates_and_retries(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await _seed(hass, freezer)
    api = FakeApi(RuntimeError("session expired"))
    add_opower(hass, api)
    entry = await setup_entry(hass, {CONF_CYCLE_DAY: 3})
    assert entry.state is ConfigEntryState.LOADED
    assert [p[2] for p in _periods(entry)] == ["backfill"] * 3
    assert entry.runtime_data._bill_error_logged

    api.reads = BILLS
    await _later(hass, freezer, entry, timedelta(hours=1))
    assert api.calls == 2
    assert [p[2] for p in _periods(entry)] == ["utility"] * 3


async def test_user_dates_win_over_bills(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await _seed(hass, freezer)
    entry = await setup_entry(hass, {CONF_CYCLE_DAY: 3})
    await hass.services.async_call(
        DOMAIN,
        "set_cycle_dates",
        {"cycle_start": "2026-10-03", "start": "2026-10-07", "end": "2026-11-02"},
        blocking=True,
    )

    add_opower(hass, FakeApi(BILLS))
    await _later(hass, freezer, entry, timedelta(hours=1))
    assert _periods(entry) == [
        ("2026-10-07", "2026-11-03", "user"),
        ("2026-09-04", "2026-10-06", "utility"),
        ("2026-08-05", "2026-09-04", "utility"),
    ]
    assert entry.runtime_data.data.history[0].start == date(2026, 10, 7)
