"""Home Assistant-level tests: setup, config flow and the card's data feed.

Everything is imported through `custom_components.check_my_bill`, the same module objects Home
Assistant loads.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.recorder import Recorder, migration
from homeassistant.helpers import recorder as recorder_helper
from sqlalchemy.orm.session import Session

# The recorder fixture autospecs these modules' functions, which on Python 3.14 evaluates their
# annotations, and they import these two names for type checking only.
migration.Recorder = Recorder  # type: ignore[attr-defined]
recorder_helper.Session = Session  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock: Any, enable_custom_integrations: None) -> None:
    """The integration depends on the recorder, which has to be set up before `hass` is."""
