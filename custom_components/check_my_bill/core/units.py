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
    from_unit: str | None,
    to_unit: str,
    heat_content: float | None,
    *,
    volume: UnitClass,
    energy: UnitClass,
) -> float | None:
    """The factor that converts ``from_unit`` to ``to_unit``; None if there is none.

    Volume and energy convert through ``heat_content``, therms per CCF.
    """
    source = unit_class(from_unit, volume, energy)
    target = unit_class(to_unit, volume, energy)
    if from_unit is None or source is None or target is None:
        return None
    if source is target:
        return source.ratio(from_unit, to_unit)
    if heat_content is None or heat_content <= 0:
        return None
    if source is volume:
        return volume.ratio(from_unit, CCF) * heat_content * energy.ratio(THERM, to_unit)
    return energy.ratio(from_unit, THERM) / heat_content * volume.ratio(CCF, to_unit)


def mixed(
    units: Collection[str | None], to_unit: str, *, volume: UnitClass, energy: UnitClass
) -> bool:
    """Whether any of ``units`` is in a different class than ``to_unit``."""
    target = unit_class(to_unit, volume, energy)
    return any((c := unit_class(u, volume, energy)) is not None and c is not target for u in units)
