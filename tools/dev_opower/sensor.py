"""Sensors replayed from opower_seed.json; enabled with ``sensor: [{platform: opower}]``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

SEED = "opower_seed.json"


class SeededSensor(Entity):
    _attr_should_poll = False

    def __init__(self, seed: dict[str, Any]) -> None:
        attributes = dict(seed.get("attributes") or {})
        self.entity_id = seed["entity_id"]
        self._attr_unique_id = seed["unique_id"]
        self._attr_name = attributes.pop("friendly_name", None)
        self._attr_icon = attributes.pop("icon", None)
        self._attr_extra_state_attributes = attributes
        self._value = seed.get("state")

    @property
    def state(self) -> Any:
        return self._value


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict[str, Any],
    async_add_entities: AddEntitiesCallback,
    discovery_info: dict[str, Any] | None = None,
) -> None:
    path = Path(hass.config.path(SEED))
    if not path.exists():
        return
    seeds = await hass.async_add_executor_job(lambda: json.loads(path.read_text()))
    async_add_entities(SeededSensor(s) for s in seeds if s.get("unique_id"))
