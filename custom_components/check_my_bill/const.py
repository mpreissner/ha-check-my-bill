"""Constants for the Check My Bill integration."""

from datetime import timedelta

DOMAIN = "check_my_bill"

# Config entry data. Statistics are stored by statistic ID, not entity ID: Opower data
# exists only as external statistics, and Energy prefs reference statistic IDs.
CONF_IMPORT_STAT = "import_stat"
CONF_EXPORT_STAT = "export_stat"
CONF_UTILITY_IMPORT_STAT = "utility_import_stat"
CONF_UTILITY_EXPORT_STAT = "utility_export_stat"
CONF_CYCLE_START_ENTITY = "cycle_start_entity"
CONF_CYCLE_END_ENTITY = "cycle_end_entity"
CONF_CYCLE_DAY = "cycle_day"
CONF_METER = "meter"
"""``electric`` or ``gas``; entries without it are electric (docs/specs/gas.md §2.2)."""
CONF_UNIT = "unit"
"""The billing unit; ``kWh`` when absent."""
CONF_UTILITY_UNIT = "utility_unit"
"""The utility statistic's real unit, when its metadata is wrong (Opower gas)."""

# Config entry options.
CONF_TARIFF = "tariff"
CONF_EXPORT_MODE = "export_mode"
CONF_TOLERANCE = "tolerance"
"""Percent, e.g. 2.0 for 2%."""

DEFAULT_TOLERANCE_PCT = 2.0
DEFAULT_GAS_TOLERANCE_PCT = 5.0

UPDATE_INTERVAL = timedelta(hours=1)
BACKFILL_MAX_CYCLES = 12
BILL_FETCH_INTERVAL = timedelta(days=1)
"""How often past bill periods are read from Opower (docs/specs/opower-bills.md §5)."""
STORE_VERSION = 1
STORE_MINOR_VERSION = 2
"""1.2 renamed the kWh-named record keys (docs/specs/gas.md §1.1)."""
