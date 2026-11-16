"""Repair issues raised from the refreshed data (docs/specs/services-api.md §5)."""

from __future__ import annotations

from homeassistant.components.recorder.statistics import async_list_statistic_ids
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .core.record import CycleRecord
from .core.tariff import ExportMode
from .stats import StatIds

EXPORT_DETECTED = "export_detected"
MISSING_STATISTIC = "missing_statistic"
EXPORT_THRESHOLD_KWH = 0.1
"""Utility-recorded export above this, in one cycle, raises the export issue."""


def issue_id(kind: str, entry_id: str) -> str:
    """One issue of each kind per entry."""
    return f"{kind}_{entry_id}"


def _set(hass: HomeAssistant, kind: str, entry: ConfigEntry, placeholders: dict[str, str]) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(kind, entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=kind,
        translation_placeholders={"title": entry.title, **placeholders},
    )


def utility_records_export(
    ids: StatIds, export_mode: ExportMode, records: list[CycleRecord]
) -> bool:
    """The utility recorded export while the tariff says export is not compensated."""
    if export_mode is not ExportMode.NONE or ids.utility_exports is None:
        return False
    return any((r.utility_export_kwh or 0.0) > EXPORT_THRESHOLD_KWH for r in records)


async def async_check(
    hass: HomeAssistant,
    entry: ConfigEntry,
    ids: StatIds,
    export_mode: ExportMode,
    recent: list[CycleRecord],
) -> None:
    """Raise or clear both issues for ``entry``. ``recent``: current and last closed cycle."""
    if utility_records_export(ids, export_mode, recent):
        _set(hass, EXPORT_DETECTED, entry, {})
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id(EXPORT_DETECTED, entry.entry_id))

    configured = ids.configured()
    listed = await async_list_statistic_ids(hass, statistic_ids=configured)
    missing = sorted(configured - {s["statistic_id"] for s in listed})
    if missing:
        _set(hass, MISSING_STATISTIC, entry, {"statistics": ", ".join(missing)})
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id(MISSING_STATISTIC, entry.entry_id))


def async_clear(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the entry's issues, when it is unloaded or removed."""
    for kind in (EXPORT_DETECTED, MISSING_STATISTIC):
        ir.async_delete_issue(hass, DOMAIN, issue_id(kind, entry_id))
