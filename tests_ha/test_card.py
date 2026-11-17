"""Serving and registering the Lovelace card (card.md §1, §6)."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.check_my_bill.card import CARD_URL
from custom_components.check_my_bill.const import CONF_CYCLE_DAY, DOMAIN

from .test_coordinator import setup_entry


async def test_card_is_served(hass: HomeAssistant, hass_client: ClientSessionGenerator) -> None:
    entry = await setup_entry(hass, {CONF_CYCLE_DAY: 3})
    assert entry.state is ConfigEntryState.LOADED

    client = await hass_client()
    response = await client.get(CARD_URL)
    assert response.status == 200
    body = await response.text()
    assert "customElements.define(CARD, CheckMyBillCard)" in body
    assert "check-my-bill-card" in body


async def test_card_registered_with_the_frontend(hass: HomeAssistant) -> None:
    hass.config.components.add("frontend")
    with patch("custom_components.check_my_bill.card.frontend.add_extra_js_url") as add:
        await setup_entry(hass, {CONF_CYCLE_DAY: 3})

    version = (await async_get_integration(hass, DOMAIN)).version
    add.assert_called_once_with(hass, f"{CARD_URL}?v={version}")


async def test_no_frontend(hass: HomeAssistant) -> None:
    with patch("custom_components.check_my_bill.card.frontend.add_extra_js_url") as add:
        entry = await setup_entry(hass, {CONF_CYCLE_DAY: 3})

    assert entry.state is ConfigEntryState.LOADED
    add.assert_not_called()
