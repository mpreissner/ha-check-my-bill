"""Tariff model and expected-cost evaluation.

Costs are evaluated per cycle. A cycle that spans a rate change or a season boundary is
split into segments by day, and the cycle quantity, tier blocks and monthly fixed charge are
prorated across segments by their share of days. That matches how utilities prorate.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum


class ExportMode(StrEnum):
    """How the utility treats energy sent to the grid."""

    NONE = "none"
    """Export earns nothing and does not offset import."""
    NET_METERING = "net_metering"
    """Billed quantity is import minus export for the cycle, floored at zero."""
    SEPARATE_RATE = "separate_rate"
    """Import is billed in full and export earns a credit at its own rate."""


@dataclass(frozen=True, slots=True)
class SeasonalRate:
    """A rate per billing unit that applies in the listed months (1-12)."""

    months: frozenset[int]
    rate: float


@dataclass(frozen=True, slots=True)
class RateSpec:
    """A rate per billing unit, optionally overridden for some months."""

    default: float
    seasonal: tuple[SeasonalRate, ...] = ()

    def __post_init__(self) -> None:
        seen: set[int] = set()
        for season in self.seasonal:
            if not season.months or not season.months <= set(range(1, 13)):
                raise ValueError(f"season months must be within 1-12: {sorted(season.months)}")
            if overlap := seen & season.months:
                raise ValueError(f"months in more than one season: {sorted(overlap)}")
            seen |= season.months

    def rate_for(self, month: int) -> float:
        """The rate in effect during ``month``."""
        for season in self.seasonal:
            if month in season.months:
                return season.rate
        return self.default


@dataclass(frozen=True, slots=True)
class Tier:
    """A usage block. Rates left as None fall back to the version's base rates.

    ``up_to`` is the cumulative cycle quantity where the block ends; None means unbounded.
    """

    up_to: float | None
    supply_rate: float | None = None
    delivery_rate: float | None = None


@dataclass(frozen=True, slots=True)
class Rider:
    """A named surcharge per billing unit."""

    name: str
    per_unit: float


@dataclass(frozen=True, slots=True)
class TariffVersion:
    """The rates in effect from ``effective_from`` until the next version."""

    effective_from: date
    supply: RateSpec
    delivery: RateSpec
    tiers: tuple[Tier, ...] = ()
    fixed_monthly: float = 0.0
    fixed_daily: float = 0.0
    riders: tuple[Rider, ...] = ()
    tax_pct: float = 0.0
    export_rate: float = 0.0

    def __post_init__(self) -> None:
        last = 0.0
        for i, tier in enumerate(self.tiers):
            if tier.up_to is None:
                if i != len(self.tiers) - 1:
                    raise ValueError("only the last tier may be unbounded")
                continue
            if tier.up_to <= last:
                raise ValueError("tier limits must be positive and strictly increasing")
            last = tier.up_to


@dataclass(frozen=True, slots=True)
class Tariff:
    """A versioned tariff. Versions are kept sorted by ``effective_from``."""

    versions: tuple[TariffVersion, ...]
    export_mode: ExportMode = ExportMode.NONE

    def __post_init__(self) -> None:
        if not self.versions:
            raise ValueError("a tariff needs at least one version")
        ordered = tuple(sorted(self.versions, key=lambda v: v.effective_from))
        dates = [v.effective_from for v in ordered]
        if len(set(dates)) != len(dates):
            raise ValueError("tariff versions must have distinct effective dates")
        object.__setattr__(self, "versions", ordered)

    def version_on(self, day: date) -> TariffVersion:
        """The version in effect on ``day``.

        Days before the first version use the first version, so backfilled cycles that
        predate the user's earliest entry still get an estimate.
        """
        current = self.versions[0]
        for version in self.versions[1:]:
            if version.effective_from > day:
                break
            current = version
        return current


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Expected charges for a period, in the user's currency."""

    billed_quantity: float
    supply: float
    delivery: float
    riders: float
    fixed: float
    tax: float
    export_credit: float

    @property
    def subtotal(self) -> float:
        """Charges before tax and export credit."""
        return self.supply + self.delivery + self.riders + self.fixed

    @property
    def total(self) -> float:
        """What the bill should come to."""
        return self.subtotal + self.tax - self.export_credit


def billed_quantity(mode: ExportMode, imports: float, exports: float) -> float:
    """The quantity the utility charges energy rates on."""
    if mode is ExportMode.NET_METERING:
        return max(0.0, imports - exports)
    return imports


def _energy_charges(
    version: TariffVersion, month: int, quantity: float, share: float
) -> tuple[float, float]:
    """Supply and delivery charges for ``quantity`` with tier blocks scaled by ``share``."""
    base_supply = version.supply.rate_for(month)
    base_delivery = version.delivery.rate_for(month)
    supply = delivery = 0.0
    remaining = quantity
    floor = 0.0
    for tier in version.tiers:
        if remaining <= 0:
            break
        if tier.up_to is None:
            amount = remaining
        else:
            cap = tier.up_to * share
            amount = min(remaining, cap - floor)
            floor = cap
        supply += amount * (base_supply if tier.supply_rate is None else tier.supply_rate)
        delivery += amount * (base_delivery if tier.delivery_rate is None else tier.delivery_rate)
        remaining -= amount
    if remaining > 0:
        supply += remaining * base_supply
        delivery += remaining * base_delivery
    return supply, delivery


def cost(
    tariff: Tariff,
    start: date,
    end: date,
    imports: float,
    exports: float = 0.0,
    period_days: int | None = None,
) -> CostBreakdown:
    """Expected charges for ``[start, end)`` given the quantity used in that range.

    ``period_days`` is the length of the full billing cycle. Pass it when ``[start, end)``
    is the cycle so far, so the monthly fixed charge is prorated to date rather than
    charged in full. It defaults to the length of the range itself.
    """
    days = (end - start).days
    if days <= 0:
        raise ValueError(f"end {end} must be after start {start}")
    if period_days is None:
        period_days = days
    if period_days < days:
        raise ValueError("period_days cannot be shorter than the range")

    quantity = billed_quantity(tariff.export_mode, imports, exports)

    segments: Counter[tuple[TariffVersion, int]] = Counter()
    for offset in range(days):
        day = start + timedelta(days=offset)
        segments[tariff.version_on(day), day.month] += 1

    supply = delivery = riders = fixed = tax = credit = 0.0
    for (version, month), seg_days in segments.items():
        share = seg_days / days
        seg_quantity = quantity * share
        seg_supply, seg_delivery = _energy_charges(version, month, seg_quantity, share)
        seg_riders = seg_quantity * sum(r.per_unit for r in version.riders)
        seg_fixed = version.fixed_monthly * seg_days / period_days + version.fixed_daily * seg_days
        supply += seg_supply
        delivery += seg_delivery
        riders += seg_riders
        fixed += seg_fixed
        tax += (seg_supply + seg_delivery + seg_riders + seg_fixed) * version.tax_pct / 100
        if tariff.export_mode is ExportMode.SEPARATE_RATE:
            credit += exports * share * version.export_rate

    return CostBreakdown(
        billed_quantity=quantity,
        supply=supply,
        delivery=delivery,
        riders=riders,
        fixed=fixed,
        tax=tax,
        export_credit=credit,
    )
