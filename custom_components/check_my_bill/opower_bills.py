"""Past bill periods from the Opower integration's own API client.

This reads another integration's internals (``runtime_data.api``), so everything is
guarded: any surprise means no bills, and the history keeps its estimates. See
docs/specs/opower-bills.md §4.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .core.bills import UtilityBill, bills_from_reads

_LOGGER = logging.getLogger(__name__)

OPOWER = "opower"
CONSUMPTION = "_energy_consumption"


class BillFetchError(Exception):
    """Opower was found but the bills could not be read."""


def _prefix(api: Any, account: Any) -> str:
    """The statistic ID prefix the Opower integration builds for an account."""
    raw = f"{api.utility.subdomain()}_{account.meter_type.name}_{account.utility_account_id}"
    return raw.replace("-", "_").lower()


def _find_account(hass: HomeAssistant, prefix: str) -> tuple[Any, Any, Any] | None:
    """The API client, the account, and the coordinator's data value for it."""
    for entry in hass.config_entries.async_entries(OPOWER):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        coordinator = getattr(entry, "runtime_data", None)
        api = getattr(coordinator, "api", None)
        data = getattr(coordinator, "data", None)
        if api is None or not isinstance(data, dict):
            continue
        for value in data.values():
            account = getattr(value, "account", None)
            try:
                if account is not None and _prefix(api, account) == prefix:
                    return api, account, value
            except AttributeError, TypeError:
                continue
    return None


def _stat_prefix(utility_stat: str | None) -> str | None:
    if not utility_stat or not utility_stat.startswith(f"{OPOWER}:"):
        return None
    if not utility_stat.endswith(CONSUMPTION):
        return None
    return utility_stat.removeprefix(f"{OPOWER}:").removesuffix(CONSUMPTION)


_FORECAST_UNITS = {"KWH": "kWh", "THERM": "thm", "CCF": "CCF"}


def account_unit(hass: HomeAssistant, utility_stat: str | None) -> str | None:
    """The account's real unit from the Opower forecast, e.g. ``thm``; None if unknown.

    The Opower integration labels every gas statistic CCF, even when the utility's data
    is in therms (docs/specs/gas.md §2.1).
    """
    if (prefix := _stat_prefix(utility_stat)) is None:
        return None
    if (found := _find_account(hass, prefix)) is None:
        return None
    forecast = getattr(found[2], "forecast", None)
    unit = getattr(getattr(forecast, "unit_of_measure", None), "name", None)
    return _FORECAST_UNITS.get(unit) if isinstance(unit, str) else None


async def async_fetch_bills(
    hass: HomeAssistant, utility_stat: str | None, since: datetime
) -> list[UtilityBill] | None:
    """Bill periods, oldest first; None if no Opower account backs ``utility_stat``.

    Raises :class:`BillFetchError` if the account was found but the call failed.
    """
    if (prefix := _stat_prefix(utility_stat)) is None:
        return None

    found = _find_account(hass, prefix)
    if found is None:
        _LOGGER.debug("No loaded Opower account for %s", utility_stat)
        return None
    api, account, _ = found

    try:
        from opower import AggregateType
    except ImportError:
        _LOGGER.debug("opower package not available")
        return None

    try:
        reads = await api.async_get_cost_reads(account, AggregateType.BILL, since, dt_util.now())
        return bills_from_reads(
            (r.start_time, r.end_time, r.consumption, r.provided_cost) for r in reads
        )
    except Exception as err:
        raise BillFetchError(repr(err)) from err
