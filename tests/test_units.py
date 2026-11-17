import pytest
from core.units import Meter, UnitClass, mixed, scale

# Factors to a base unit (m³, kWh), as HA's converters define them.
_VOLUME = {"m³": 1.0, "ft³": 0.0283168, "CCF": 2.83168, "MCF": 28.3168}
_ENERGY = {"kWh": 1.0, "MJ": 1 / 3.6, "thm": 29.3071}

VOLUME = UnitClass(_VOLUME, lambda a, b: _VOLUME[a] / _VOLUME[b])
ENERGY = UnitClass(_ENERGY, lambda a, b: _ENERGY[a] / _ENERGY[b])


def s(a: str | None, b: str, hc: float | None = None) -> float | None:
    return scale(a, b, hc, volume=VOLUME, energy=ENERGY)


def test_meter_values() -> None:
    assert Meter("gas") is Meter.GAS
    assert Meter.ELECTRIC == "electric"


def test_same_class() -> None:
    assert s("m³", "CCF") == pytest.approx(0.353147, rel=1e-5)
    assert s("kWh", "thm") == pytest.approx(0.034121, rel=1e-4)
    assert s("ft³", "CCF") == pytest.approx(0.01)
    assert s("kWh", "kWh") == 1.0


def test_same_class_ignores_heat_content() -> None:
    assert s("ft³", "CCF", 1.037) == pytest.approx(0.01)


def test_cross_class() -> None:
    assert s("CCF", "thm", 1.037) == pytest.approx(1.037)
    assert s("ft³", "thm", 1.037) == pytest.approx(0.01037)
    assert s("thm", "CCF", 1.037) == pytest.approx(1 / 1.037)
    # m³ -> CCF -> thm -> kWh
    assert s("m³", "kWh", 1.037) == pytest.approx(0.353147 * 1.037 * 29.3071, rel=1e-5)


def test_no_conversion() -> None:
    assert s("CCF", "thm") is None
    assert s("CCF", "thm", 0.0) is None
    assert s("W", "kWh") is None
    assert s(None, "kWh") is None
    assert s("kWh", "gal") is None


def test_mixed() -> None:
    assert mixed(["ft³", "CCF"], "thm", volume=VOLUME, energy=ENERGY)
    assert not mixed(["ft³", "CCF"], "m³", volume=VOLUME, energy=ENERGY)
    assert not mixed(["kWh", None], "thm", volume=VOLUME, energy=ENERGY)
