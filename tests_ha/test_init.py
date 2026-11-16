"""Home Assistant loads the integration."""

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.check_my_bill.const import DOMAIN


async def test_setup(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
