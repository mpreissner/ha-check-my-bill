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


def _find_account(hass: HomeAssistant, prefix: str) -> tuple[Any, Any] | None:
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
                    return api, account
            except AttributeError, TypeError:
                continue
    return None


async def async_fetch_bills(
    hass: HomeAssistant, utility_stat: str | None, since: datetime
) -> list[UtilityBill] | None:
    """Bill periods, oldest first; None if no Opower account backs ``utility_stat``.

    Raises :class:`BillFetchError` if the account was found but the call failed.
    """
    if not utility_stat or not utility_stat.startswith(f"{OPOWER}:"):
        return None
    if not utility_stat.endswith(CONSUMPTION):
        return None
    prefix = utility_stat.removeprefix(f"{OPOWER}:").removesuffix(CONSUMPTION)

    found = _find_account(hass, prefix)
    if found is None:
        _LOGGER.debug("No loaded Opower account for %s", utility_stat)
        return None
    api, account = found

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
