"""Find the Energy dashboard's grid statistics and the Opower electric accounts.

See docs/specs/config-flow.md §1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder.statistics import async_list_statistic_ids
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

OPOWER = "opower"
CONSUMPTION = "_energy_consumption"
RETURN = "_energy_return"
START_SUFFIX = "_elec_start_date"
END_SUFFIX = "_elec_end_date"


@dataclass(frozen=True, slots=True)
class GridPair:
    """One grid source's import statistic and its export statistic, if any."""

    import_stat: str
    export_stat: str | None


@dataclass(frozen=True, slots=True)
class OpowerAccount:
    """An Opower electric account and the date sensors that belong to it."""

    key: str
    consumption_stat: str
    return_stat: str | None
    start_entity: str | None
    end_entity: str | None
    config_entry_id: str | None
    title: str


def _normalize(value: str) -> str:
    return value.lower().replace("-", "_")


def key_from_statistic(statistic_id: str) -> str | None:
    """``opower:bge_elec_123_energy_consumption`` -> ``bge_123``; None if not electric."""
    if not statistic_id.startswith(f"{OPOWER}:") or not statistic_id.endswith(CONSUMPTION):
        return None
    prefix = statistic_id.removeprefix(f"{OPOWER}:").removesuffix(CONSUMPTION)
    if "_elec_" not in prefix:
        return None
    return prefix.replace("_elec_", "_", 1)


def key_from_unique_id(unique_id: str) -> tuple[str, str] | None:
    """``bge_123_elec_start_date`` -> (``bge_123``, ``start``); None for other sensors."""
    for suffix, which in ((START_SUFFIX, "start"), (END_SUFFIX, "end")):
        if unique_id.endswith(suffix):
            return _normalize(unique_id.removesuffix(suffix)), which
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


async def grid_statistics(hass: HomeAssistant) -> list[GridPair]:
    """The Energy dashboard's grid import/export statistic pairs."""
    manager = await async_get_manager(hass)
    return grid_pairs(manager.data)  # type: ignore[arg-type]


async def opower_accounts(hass: HomeAssistant) -> list[OpowerAccount]:
    """Opower electric accounts with a consumption statistic."""
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
        if (key := key_from_statistic(statistic_id)) is None:
            continue
        dates = entities.get(key, {})
        entry_id = next((e.config_entry_id for e in dates.values()), None)
        if entry_id is None and len(titles) == 1:
            entry_id = next(iter(titles))
        return_stat = statistic_id.removesuffix(CONSUMPTION) + RETURN
        accounts.append(
            OpowerAccount(
                key=key,
                consumption_stat=statistic_id,
                return_stat=return_stat if return_stat in stat_ids else None,
                start_entity=dates["start"].entity_id if "start" in dates else None,
                end_entity=dates["end"].entity_id if "end" in dates else None,
                config_entry_id=entry_id,
                title=titles.get(entry_id or "", "Opower"),
            )
        )
    return accounts
