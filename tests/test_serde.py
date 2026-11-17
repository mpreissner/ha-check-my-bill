from datetime import date

import pytest
from core.serde import tariff_from_options, version_from_dict, version_to_dict
from core.tariff import ExportMode, cost

FULL = {
    "effective_from": "2026-06-01",
    "supply": {"default": 0.11, "seasonal": [{"months": [6, 7, 8, 9], "rate": 0.13}]},
    "delivery": 0.05,
    "tiers": [
        {"up_to": 500.0, "supply_rate": 0.10, "delivery_rate": None},
        {"up_to": None, "supply_rate": None, "delivery_rate": None},
    ],
    "fixed_monthly": 8.0,
    "fixed_daily": 0.1,
    "riders": [{"name": "EmPOWER", "per_unit": 0.006}],
    "tax_pct": 2.0,
    "export_rate": 0.03,
}


def test_round_trip() -> None:
    assert version_to_dict(version_from_dict(FULL)) == FULL


def test_legacy_kwh_keys_load_and_save_as_new() -> None:
    legacy = {
        **FULL,
        "tiers": [
            {"up_to_kwh": 500.0, "supply_rate": 0.10, "delivery_rate": None},
            {"up_to_kwh": None, "supply_rate": None, "delivery_rate": None},
        ],
        "riders": [{"name": "EmPOWER", "per_kwh": 0.006}],
    }
    assert version_from_dict(legacy) == version_from_dict(FULL)
    assert version_to_dict(version_from_dict(legacy)) == FULL


def test_minimal_version_uses_defaults() -> None:
    version = version_from_dict({"effective_from": "2026-01-01", "supply": 0.1, "delivery": 0})
    assert version.tiers == ()
    assert version.fixed_monthly == 0.0
    assert version.supply.rate_for(7) == 0.1


def test_seasonal_rate_applies() -> None:
    assert version_from_dict(FULL).supply.rate_for(7) == 0.13
    assert version_from_dict(FULL).supply.rate_for(1) == 0.11


def test_tariff_from_options() -> None:
    assert tariff_from_options(None, "none") is None
    assert tariff_from_options([], None) is None
    tariff = tariff_from_options([FULL], "separate_rate")
    assert tariff is not None
    assert tariff.export_mode is ExportMode.SEPARATE_RATE
    assert tariff_from_options([FULL], None).export_mode is ExportMode.NONE  # type: ignore[union-attr]
    assert cost(tariff, date(2026, 6, 1), date(2026, 7, 1), 100.0).total > 0


@pytest.mark.parametrize(
    "bad",
    [
        {**FULL, "supply": {"default": 0.1, "seasonal": [{"months": [13], "rate": 0.2}]}},
        {**FULL, "tiers": [{"up_to": None}, {"up_to": 5.0}]},
        {**FULL, "effective_from": "June"},
    ],
)
def test_invalid_versions_raise(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        version_from_dict(bad)
