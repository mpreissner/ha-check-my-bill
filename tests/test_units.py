import pytest
from core.units import Meter, UnitClass, scale

# Factors to a base unit (m³, kWh), as HA's converters define them.
_VOLUME = {"m³": 1.0, "ft³": 0.0283168, "CCF": 2.83168, "MCF": 28.3168}
_ENERGY = {"kWh": 1.0, "MJ": 1 / 3.6, "thm": 29.3071}

VOLUME = UnitClass(_VOLUME, lambda a, b: _VOLUME[a] / _VOLUME[b])
ENERGY = UnitClass(_ENERGY, lambda a, b: _ENERGY[a] / _ENERGY[b])


def s(a: str | None, b: str) -> float | None:
    return scale(a, b, volume=VOLUME, energy=ENERGY)


def test_meter_values() -> None:
    assert Meter("gas") is Meter.GAS
    assert Meter.ELECTRIC == "electric"


def test_same_class() -> None:
    assert s("m³", "CCF") == pytest.approx(0.353147, rel=1e-5)
    assert s("kWh", "thm") == pytest.approx(0.034121, rel=1e-4)
    assert s("ft³", "CCF") == pytest.approx(0.01)
    assert s("kWh", "kWh") == 1.0


def test_one_ccf_is_one_therm() -> None:
    assert s("CCF", "thm") == pytest.approx(1.0)
    assert s("thm", "CCF") == pytest.approx(1.0)
    assert s("ft³", "thm") == pytest.approx(0.01)
    # m³ -> CCF -> thm -> kWh
    assert s("m³", "kWh") == pytest.approx(0.353147 * 29.3071, rel=1e-5)
    assert s("kWh", "ft³") == pytest.approx(100 / 29.3071, rel=1e-5)


def test_no_conversion() -> None:
    assert s("W", "kWh") is None
    assert s(None, "kWh") is None
    assert s("kWh", "gal") is None
