"""Convert the tariff to and from the plain dicts kept in config entry options.

Shape of one version (every key except ``effective_from``, ``supply`` and ``delivery``
is optional)::

    {"effective_from": "2026-06-01",
     "supply": 0.11 | {"default": 0.11, "seasonal": [{"months": [6, 7, 8, 9], "rate": 0.13}]},
     "delivery": <same shape as supply>,
     "tiers": [{"up_to": 500, "supply_rate": 0.10, "delivery_rate": null}],
     "fixed_monthly": 8.0, "fixed_daily": 0.0,
     "riders": [{"name": "EmPOWER", "per_unit": 0.006}],
     "tax_pct": 2.0, "export_rate": 0.0}

Rates and tier limits are in the entry's billing unit. ``up_to`` and ``per_unit`` were
``up_to_kwh`` and ``per_kwh`` up to 0.1.0b3; both spellings are read, the new ones written.

``export_mode`` is a tariff-level option stored next to the version list.
"""

from datetime import date
from typing import Any

from .tariff import ExportMode, RateSpec, Rider, SeasonalRate, Tariff, TariffVersion, Tier


def _rate_from(value: Any) -> RateSpec:
    if isinstance(value, int | float):
        return RateSpec(float(value))
    seasonal = tuple(
        SeasonalRate(frozenset(int(m) for m in s["months"]), float(s["rate"]))
        for s in value.get("seasonal", ())
    )
    return RateSpec(float(value["default"]), seasonal)


def _rate_to(spec: RateSpec) -> float | dict[str, Any]:
    if not spec.seasonal:
        return spec.default
    return {
        "default": spec.default,
        "seasonal": [{"months": sorted(s.months), "rate": s.rate} for s in spec.seasonal],
    }


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def version_from_dict(data: dict[str, Any]) -> TariffVersion:
    """Build a tariff version. Raises ``ValueError``/``KeyError`` on bad input."""
    return TariffVersion(
        effective_from=date.fromisoformat(data["effective_from"]),
        supply=_rate_from(data["supply"]),
        delivery=_rate_from(data["delivery"]),
        tiers=tuple(
            Tier(
                _optional_float(t.get("up_to", t.get("up_to_kwh"))),
                _optional_float(t.get("supply_rate")),
                _optional_float(t.get("delivery_rate")),
            )
            for t in data.get("tiers", ())
        ),
        fixed_monthly=float(data.get("fixed_monthly", 0.0)),
        fixed_daily=float(data.get("fixed_daily", 0.0)),
        riders=tuple(
            Rider(str(r["name"]), float(r["per_unit"] if "per_unit" in r else r["per_kwh"]))
            for r in data.get("riders", ())
        ),
        tax_pct=float(data.get("tax_pct", 0.0)),
        export_rate=float(data.get("export_rate", 0.0)),
    )


def version_to_dict(version: TariffVersion) -> dict[str, Any]:
    """The inverse of :func:`version_from_dict`."""
    return {
        "effective_from": version.effective_from.isoformat(),
        "supply": _rate_to(version.supply),
        "delivery": _rate_to(version.delivery),
        "tiers": [
            {
                "up_to": t.up_to,
                "supply_rate": t.supply_rate,
                "delivery_rate": t.delivery_rate,
            }
            for t in version.tiers
        ],
        "fixed_monthly": version.fixed_monthly,
        "fixed_daily": version.fixed_daily,
        "riders": [{"name": r.name, "per_unit": r.per_unit} for r in version.riders],
        "tax_pct": version.tax_pct,
        "export_rate": version.export_rate,
    }


def tariff_from_options(
    versions: list[dict[str, Any]] | None, export_mode: str | None
) -> Tariff | None:
    """The configured tariff, or None when the user skipped the tariff step."""
    if not versions:
        return None
    return Tariff(
        tuple(version_from_dict(v) for v in versions),
        ExportMode(export_mode or ExportMode.NONE),
    )
