"""Check My Bill: validate utility bills against Energy dashboard statistics.

Scaffold only. The design lives in docs/specs/ and lands through feature branches.
"""

from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration (no-op until the config flow lands)."""
    return True
