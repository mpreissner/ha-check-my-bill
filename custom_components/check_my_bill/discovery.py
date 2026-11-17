"""Find the Energy dashboard's grid and gas statistics and the Opower accounts.

See docs/specs/config-flow.md §1 and docs/specs/gas.md §2.1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder.statistics import async_list_statistic_ids
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .core.units import Meter

OPOWER = "opower"
CONSUMPTION = "_energy_consumption"
RETURN = "_energy_return"
METER_TAGS = {Meter.ELECTRIC: "elec", Meter.GAS: "gas"}
"""How Opower names each meter type in statistic IDs and sensor unique IDs."""


@dataclass(frozen=True, slots=True)
class GridPair:
    """One grid source's import statistic and its export statistic, if any."""

    import_stat: str
    export_stat: str | None


@dataclass(frozen=True, slots=True)
class OpowerAccount:
    """An Opower account and the date sensors that belong to it."""

    key: str
    meter: Meter
    consumption_stat: str
    return_stat: str | None
    start_entity: str | None
    end_entity: str | None
    config_entry_id: str | None
    title: str


def _normalize(value: str) -> str:
    return value.lower().replace("-", "_")


def key_from_statistic(statistic_id: str) -> str | None:
    """``opower:bge_elec_123_energy_consumption`` -> ``bge_123_elec``; None if not Opower.

    The key ends in the meter tag, so an electric and a gas account with the same number
    differ.
    """
    if not statistic_id.startswith(f"{OPOWER}:") or not statistic_id.endswith(CONSUMPTION):
        return None
    prefix = statistic_id.removeprefix(f"{OPOWER}:").removesuffix(CONSUMPTION)
    for tag in METER_TAGS.values():
        if f"_{tag}_" in prefix:
            return f"{prefix.replace(f'_{tag}_', '_', 1)}_{tag}"
    return None


def meter_of_key(key: str) -> Meter | None:
    """The meter a discovery key belongs to."""
    return next((m for m, tag in METER_TAGS.items() if key.endswith(f"_{tag}")), None)


def key_from_unique_id(unique_id: str) -> tuple[str, str] | None:
    """``bge_123_elec_start_date`` -> (``bge_123_elec``, ``start``); None for other sensors."""
    for tag in METER_TAGS.values():
        for which in ("start", "end"):
            suffix = f"_{tag}_{which}_date"
            if unique_id.endswith(suffix):
                return f"{_normalize(unique_id.removesuffix(suffix))}_{tag}", which
    return None


def grid_pairs(prefs: dict[str, Any] | None) -> list[GridPair]:
    """Grid sources in either the legacy ``flow_from`` layout or the flat one."""
    pairs: list[GridPair] = []
    for source in (prefs or {}).get("energy_sources", []):
        if source.get("type") != "grid":
            continue
        exports = [
            f["stat_energy_to"] for f in source.get("flow_to", []) if f.get("stat_energy_to")
        ]
        if source.get("stat_energy_to"):
            exports.insert(0, source["stat_energy_to"])
        imports = [
            f["stat_energy_from"] for f in source.get("flow_from", []) if f.get("stat_energy_from")
        ]
        if source.get("stat_energy_from"):
            imports.insert(0, source["stat_energy_from"])
        pairs.extend(
            GridPair(stat, exports[i] if i < len(exports) else None)
            for i, stat in enumerate(imports)
        )
    return pairs


def gas_sources(prefs: dict[str, Any] | None) -> list[str]:
    """The gas sources' consumption statistics."""
    return [
        source["stat_energy_from"]
        for source in (prefs or {}).get("energy_sources", [])
        if source.get("type") == "gas" and source.get("stat_energy_from")
    ]


async def gas_statistics(hass: HomeAssistant) -> list[str]:
    """The Energy dashboard's gas consumption statistics."""
    manager = await async_get_manager(hass)
    return gas_sources(manager.data)  # type: ignore[arg-type]


async def grid_statistics(hass: HomeAssistant) -> list[GridPair]:
    """The Energy dashboard's grid import/export statistic pairs."""
    manager = await async_get_manager(hass)
    return grid_pairs(manager.data)  # type: ignore[arg-type]


async def opower_accounts(
    hass: HomeAssistant, meter: Meter = Meter.ELECTRIC
) -> list[OpowerAccount]:
    """Opower accounts of one meter type with a consumption statistic."""
    listed = await async_list_statistic_ids(hass, statistic_type="sum")
    stat_ids = {s["statistic_id"] for s in listed}

    entities: dict[str, dict[str, er.RegistryEntry]] = {}
    for entry in er.async_get(hass).entities.values():
        if entry.platform != OPOWER or (found := key_from_unique_id(entry.unique_id)) is None:
            continue
        key, which = found
        entities.setdefault(key, {})[which] = entry

    titles = {e.entry_id: e.title for e in hass.config_entries.async_entries(OPOWER)}
    accounts = []
    for statistic_id in sorted(stat_ids):
        if (key := key_from_statistic(statistic_id)) is None or meter_of_key(key) is not meter:
            continue
        dates = entities.get(key, {})
        entry_id = next((e.config_entry_id for e in dates.values()), None)
        if entry_id is None and len(titles) == 1:
            entry_id = next(iter(titles))
        return_stat = statistic_id.removesuffix(CONSUMPTION) + RETURN
        accounts.append(
            OpowerAccount(
                key=key,
                meter=meter,
                consumption_stat=statistic_id,
                return_stat=return_stat if return_stat in stat_ids else None,
                start_entity=dates["start"].entity_id if "start" in dates else None,
                end_entity=dates["end"].entity_id if "end" in dates else None,
                config_entry_id=entry_id,
                title=titles.get(entry_id or "", "Opower"),
            )
        )
    return accounts
