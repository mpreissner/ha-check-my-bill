"""Serve the Lovelace card and load it on every dashboard (docs/specs/card.md §1)."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

URL_BASE = f"/{DOMAIN}_static"
CARD_FILE = "check-my-bill-card.js"
CARD_URL = f"{URL_BASE}/{CARD_FILE}"


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve frontend/ and, when the frontend is loaded, add the card to every page."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(URL_BASE, str(Path(__file__).parent / "frontend"), cache_headers=True)]
    )
    if "frontend" not in hass.config.components:
        return
    integration = await async_get_integration(hass, DOMAIN)
    frontend.add_extra_js_url(hass, f"{CARD_URL}?v={integration.version}")
