"""Persisted cycle history (spec §3.2)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORE_MINOR_VERSION, STORE_VERSION
from .core.cycles import Cycle
from .core.record import CycleRecord, Source, upgrade_record_dict

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class StoredState:
    """What survives a restart: the cycle last seen as current, and closed-cycle records."""

    current: Cycle | None = None
    current_source: Source | None = None
    cycles: list[CycleRecord] = field(default_factory=list)
    """Closed cycles, newest first."""
    sources: dict[str, str | None] | None = None
    """The statistic IDs the records were computed from; None before this was stored."""

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form."""
        return {
            "current": None
            if self.current is None or self.current_source is None
            else {
                "start": self.current.start.isoformat(),
                "end": self.current.end.isoformat(),
                "source": self.current_source.value,
            },
            "cycles": [record.to_dict() for record in self.cycles],
            "sources": self.sources,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StoredState:
        """Load, skipping records that no longer parse rather than losing the rest."""
        state = cls()
        if isinstance(sources := data.get("sources"), dict):
            state.sources = sources
        if current := data.get("current"):
            try:
                state.current = Cycle(
                    date.fromisoformat(current["start"]), date.fromisoformat(current["end"])
                )
                state.current_source = Source(current["source"])
            except KeyError, TypeError, ValueError:
                _LOGGER.warning("Ignoring unreadable stored current cycle: %s", current)
                state.current = None
        for raw in data.get("cycles", []):
            try:
                state.cycles.append(CycleRecord.from_dict(raw))
            except KeyError, TypeError, ValueError:
                _LOGGER.warning("Ignoring unreadable stored cycle record: %s", raw)
        state.cycles.sort(key=lambda r: r.start, reverse=True)
        return state


def migrate(minor_version: int, data: dict[str, Any]) -> dict[str, Any]:
    """Stored data from an earlier 1.x minor version, in the current shape."""
    if minor_version < 2:
        data = {
            **data,
            "cycles": [
                upgrade_record_dict(raw) if isinstance(raw, dict) else raw
                for raw in data.get("cycles", [])
            ],
        }
    return data


class _Store(Store[dict[str, Any]]):
    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        if old_major_version != STORE_VERSION:
            raise NotImplementedError
        return migrate(old_minor_version, old_data)


class CycleStore:
    """The ``check_my_bill.<entry_id>`` store."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = _Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry_id}", minor_version=STORE_MINOR_VERSION
        )
        self._saved: dict[str, Any] | None = None

    async def async_load(self) -> StoredState:
        """The stored state, or an empty one on first setup."""
        data = await self._store.async_load()
        self._saved = data
        return StoredState() if data is None else StoredState.from_dict(data)

    async def async_save(self, state: StoredState) -> None:
        """Write the state if it changed since the last load or save."""
        data = state.to_dict()
        if data != self._saved:
            await self._store.async_save(data)
            self._saved = data

    async def async_remove(self) -> None:
        """Delete the file, when the config entry is removed."""
        await self._store.async_remove()
