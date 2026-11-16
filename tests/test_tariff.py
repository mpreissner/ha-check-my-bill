from datetime import date

import pytest
from core.tariff import (
    ExportMode,
    RateSpec,
    Rider,
    SeasonalRate,
    Tariff,
    TariffVersion,
    Tier,
    billed_kwh,
    cost,
)

JUN_1 = date(2026, 6, 1)
JUL_1 = date(2026, 7, 1)


def flat(supply: float = 0.10, delivery: float = 0.05, **kwargs: object) -> TariffVersion:
    return TariffVersion(
        effective_from=date(2020, 1, 1),
        supply=RateSpec(supply),
        delivery=RateSpec(delivery),
        **kwargs,  # type: ignore[arg-type]
    )


def test_flat_rate_with_fixed_and_tax() -> None:
    tariff = Tariff((flat(fixed_monthly=10.0, tax_pct=10.0),))
    result = cost(tariff, JUN_1, JUL_1, 100.0)
    assert result.supply == pytest.approx(10.0)
    assert result.delivery == pytest.approx(5.0)
    assert result.fixed == pytest.approx(10.0)
    assert result.subtotal == pytest.approx(25.0)
    assert result.tax == pytest.approx(2.5)
    assert result.total == pytest.approx(27.5)


def test_riders_and_fixed_daily() -> None:
    version = flat(riders=(Rider("a", 0.01), Rider("b", 0.005)), fixed_daily=0.5)
    result = cost(Tariff((version,)), JUN_1, JUL_1, 200.0)
    assert result.riders == pytest.approx(3.0)
    assert result.fixed == pytest.approx(15.0)


def test_seasonal_rate_splits_by_day() -> None:
    summer = RateSpec(0.10, (SeasonalRate(frozenset({7, 8}), 0.20),))
    version = TariffVersion(date(2020, 1, 1), supply=summer, delivery=RateSpec(0.0))
    # 15 June days + 15 July days, 300 kWh → 150 at 0.10 and 150 at 0.20.
    result = cost(Tariff((version,)), date(2026, 6, 16), date(2026, 7, 16), 300.0)
    assert result.supply == pytest.approx(45.0)


def test_rate_change_mid_cycle_prorates_rates_and_fixed() -> None:
    old = TariffVersion(date(2020, 1, 1), RateSpec(0.10), RateSpec(0.0), fixed_monthly=10.0)
    new = TariffVersion(date(2026, 6, 11), RateSpec(0.13), RateSpec(0.0), fixed_monthly=16.0)
    result = cost(Tariff((new, old)), JUN_1, date(2026, 7, 1), 300.0)
    # 10 days old, 20 days new.
    assert result.supply == pytest.approx(100 * 0.10 + 200 * 0.13)
    assert result.fixed == pytest.approx(10.0 / 3 + 16.0 * 2 / 3)


def test_versions_sorted_and_earliest_used_before_first() -> None:
    a = flat(supply=0.1)
    b = TariffVersion(date(2026, 1, 1), RateSpec(0.2), RateSpec(0.0))
    tariff = Tariff((b, a))
    assert tariff.versions == (a, b)
    early = TariffVersion(date(2026, 1, 1), RateSpec(0.2), RateSpec(0.0))
    assert Tariff((early,)).version_on(date(2019, 1, 1)) is early


def test_tiers_within_one_month() -> None:
    version = flat(
        supply=0.0,
        delivery=0.0,
        tiers=(Tier(500, supply_rate=0.10), Tier(None, supply_rate=0.15)),
    )
    result = cost(Tariff((version,)), JUN_1, JUL_1, 1000.0)
    assert result.supply == pytest.approx(50 + 75)


def test_tier_blocks_scale_across_segments() -> None:
    version = flat(
        supply=0.0,
        delivery=0.0,
        tiers=(Tier(500, supply_rate=0.10), Tier(None, supply_rate=0.15)),
    )
    # The same cycle split across two months must cost the same.
    result = cost(Tariff((version,)), date(2026, 6, 16), date(2026, 7, 16), 1000.0)
    assert result.supply == pytest.approx(125.0)


def test_usage_beyond_bounded_tiers_uses_base_rate() -> None:
    version = flat(supply=0.20, delivery=0.0, tiers=(Tier(100, supply_rate=0.10),))
    result = cost(Tariff((version,)), JUN_1, JUL_1, 300.0)
    assert result.supply == pytest.approx(10 + 40)


def test_tier_rate_none_falls_back_to_base() -> None:
    version = flat(supply=0.10, delivery=0.05, tiers=(Tier(100, delivery_rate=0.0),))
    result = cost(Tariff((version,)), JUN_1, JUL_1, 200.0)
    assert result.supply == pytest.approx(20.0)
    assert result.delivery == pytest.approx(5.0)


def test_partial_cycle_prorates_monthly_fixed_charge() -> None:
    tariff = Tariff((flat(fixed_monthly=12.0),))
    result = cost(tariff, JUN_1, date(2026, 6, 11), 0.0, period_days=30)
    assert result.fixed == pytest.approx(4.0)


def test_period_shorter_than_range_rejected() -> None:
    with pytest.raises(ValueError, match="period_days"):
        cost(Tariff((flat(),)), JUN_1, JUL_1, 0.0, period_days=10)


def test_empty_range_rejected() -> None:
    with pytest.raises(ValueError, match="after start"):
        cost(Tariff((flat(),)), JUN_1, JUN_1, 0.0)


def test_export_ignored_without_net_metering() -> None:
    tariff = Tariff((flat(export_rate=0.05),), ExportMode.NONE)
    result = cost(tariff, JUN_1, JUL_1, 100.0, export_kwh=30.0)
    assert result.billed_kwh == 100.0
    assert result.export_credit == 0.0


def test_net_metering_bills_net_and_floors_at_zero() -> None:
    tariff = Tariff((flat(),), ExportMode.NET_METERING)
    assert cost(tariff, JUN_1, JUL_1, 100.0, export_kwh=30.0).billed_kwh == 70.0
    assert cost(tariff, JUN_1, JUL_1, 10.0, export_kwh=30.0).total == pytest.approx(0.0)
    assert billed_kwh(ExportMode.NET_METERING, 10.0, 30.0) == 0.0


def test_separate_rate_credits_export() -> None:
    tariff = Tariff((flat(export_rate=0.05),), ExportMode.SEPARATE_RATE)
    result = cost(tariff, JUN_1, JUL_1, 100.0, export_kwh=40.0)
    assert result.billed_kwh == 100.0
    assert result.export_credit == pytest.approx(2.0)
    assert result.total == pytest.approx(15.0 - 2.0)


def test_overlapping_seasons_rejected() -> None:
    with pytest.raises(ValueError, match="more than one season"):
        RateSpec(0.1, (SeasonalRate(frozenset({6, 7}), 0.2), SeasonalRate(frozenset({7}), 0.3)))


def test_season_months_validated() -> None:
    with pytest.raises(ValueError, match="1-12"):
        RateSpec(0.1, (SeasonalRate(frozenset({13}), 0.2),))


def test_tiers_validated() -> None:
    with pytest.raises(ValueError, match="increasing"):
        flat(tiers=(Tier(500), Tier(400)))
    with pytest.raises(ValueError, match="last tier"):
        flat(tiers=(Tier(None), Tier(400)))


def test_tariff_validated() -> None:
    with pytest.raises(ValueError, match="at least one"):
        Tariff(())
    with pytest.raises(ValueError, match="distinct"):
        Tariff((flat(), flat()))
