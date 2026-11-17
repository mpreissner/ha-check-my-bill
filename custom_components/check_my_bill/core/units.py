"""Meter types and the factors that convert a statistic to the billing unit.

See docs/specs/gas.md §3. HA's unit converters are passed in as :class:`UnitClass`
values, so this module stays free of Home Assistant imports.
"""

from collections.abc import Callable, Collection
from dataclasses import dataclass
from enum import StrEnum


class Meter(StrEnum):
    """What a config entry checks."""

    ELECTRIC = "electric"
    GAS = "gas"


CCF = "CCF"
THERM = "thm"
KWH = "kWh"


@dataclass(frozen=True, slots=True)
class UnitClass:
    """One family of units that convert among themselves by a fixed factor."""

    units: Collection[str]
    ratio: Callable[[str, str], float]
    """Multiply a value in the first unit by this to get the second."""


def unit_class(unit: str | None, *classes: UnitClass) -> UnitClass | None:
    """The class ``unit`` belongs to, if any."""
    return next((c for c in classes if unit in c.units), None)


def scale(
    from_unit: str | None, to_unit: str, *, volume: UnitClass, energy: UnitClass
) -> float | None:
    """The factor that converts ``from_unit`` to ``to_unit``; None if there is none.

    Volume and energy convert at 1 CCF = 1 therm, the way US utilities bill gas.
    """
    source = unit_class(from_unit, volume, energy)
    target = unit_class(to_unit, volume, energy)
    if from_unit is None or source is None or target is None:
        return None
    if source is target:
        return source.ratio(from_unit, to_unit)
    if source is volume:
        return volume.ratio(from_unit, CCF) * energy.ratio(THERM, to_unit)
    return energy.ratio(from_unit, THERM) * volume.ratio(CCF, to_unit)
