"""Config flow, reconfigure and options flow (docs/specs/config-flow.md)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components.recorder.statistics import async_list_statistic_ids
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    ObjectSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    StatisticSelector,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CYCLE_DAY,
    CONF_CYCLE_END_ENTITY,
    CONF_CYCLE_START_ENTITY,
    CONF_EXPORT_MODE,
    CONF_EXPORT_STAT,
    CONF_IMPORT_STAT,
    CONF_METER,
    CONF_TARIFF,
    CONF_TOLERANCE,
    CONF_UNIT,
    CONF_UTILITY_EXPORT_STAT,
    CONF_UTILITY_IMPORT_STAT,
    CONF_UTILITY_UNIT,
    DEFAULT_GAS_TOLERANCE_PCT,
    DEFAULT_TOLERANCE_PCT,
    DOMAIN,
)
from .core.serde import tariff_from_options, version_from_dict, version_to_dict
from .core.tariff import ExportMode, RateSpec, TariffVersion
from .core.units import CCF, KWH, THERM, Meter
from .discovery import OpowerAccount, gas_statistics, grid_statistics, opower_accounts
from .opower_bills import account_unit
from .stats import ENERGY, VOLUME

TITLE = "Check My Bill"
GAS_TITLE = "Check My Bill \u2013 Gas"
NO_UTILITY = "none"

CONF_ACCOUNT = "account"
CONF_ENABLE_DATE_SENSORS = "enable_date_sensors"
CONF_LAST_BILL_END = "last_bill_end"
CONF_EFFECTIVE_FROM = "effective_from"
CONF_VERSIONS = "versions"
RATE_FIELDS = ("supply", "delivery")
CHARGE_FIELDS = ("fixed_monthly", "fixed_daily", "tax_pct", "export_rate")

_STAT = StatisticSelector()
_DATE = DateSelector()
_SENSOR = EntitySelector(EntitySelectorConfig(domain="sensor"))
_EXPORT_MODE = SelectSelector(
    SelectSelectorConfig(
        options=[m.value for m in ExportMode],
        mode=SelectSelectorMode.DROPDOWN,
        translation_key=CONF_EXPORT_MODE,
    )
)
_CYCLE_DAY = NumberSelector(
    NumberSelectorConfig(min=1, max=28, step=1, mode=NumberSelectorMode.BOX)
)
_TOLERANCE = NumberSelector(
    NumberSelectorConfig(
        min=0.1, max=20, step=0.1, mode=NumberSelectorMode.BOX, unit_of_measurement="%"
    )
)


def _money(unit: str) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=0, step="any", mode=NumberSelectorMode.BOX, unit_of_measurement=unit
        )
    )


GAS_UNITS = (THERM, CCF, "m³", KWH)
"""Billing units offered for gas, the default first."""
_METER = SelectSelector(
    SelectSelectorConfig(
        options=[m.value for m in Meter],
        mode=SelectSelectorMode.LIST,
        translation_key=CONF_METER,
    )
)
_UNIT_LABELS = {
    THERM: "Therms (thm)",
    CCF: "Hundred cubic feet (CCF)",
    "m³": "Cubic meters (m³)",
    KWH: "Kilowatt-hours (kWh)",
}
_GAS_UNIT = SelectSelector(
    SelectSelectorConfig(
        options=[SelectOptionDict(value=u, label=_UNIT_LABELS[u]) for u in GAS_UNITS],
        mode=SelectSelectorMode.DROPDOWN,
    )
)


def _field_selectors(unit: str, *, exports: bool) -> dict[str, NumberSelector]:
    """The rate form's fields, labelled per billing ``unit``."""
    fields = {
        "supply": _money(f"$/{unit}"),
        "delivery": _money(f"$/{unit}"),
        "fixed_monthly": _money("$"),
        "fixed_daily": _money("$"),
        "tax_pct": _money("%"),
    }
    if exports:
        fields["export_rate"] = _money(f"$/{unit}")
    return fields


def _suggested(key: str, value: Any, *, required: bool = False) -> vol.Marker:
    """A field pre-filled with ``value`` that the user can clear (unless required)."""
    marker = vol.Required if required else vol.Optional
    if value is None:
        return marker(key)
    return marker(key, description={"suggested_value": value})


def _rates_schema(
    prefill: dict[str, Any],
    *,
    unit: str = KWH,
    exports: bool = True,
    extra: dict[Any, Any] | None = None,
) -> vol.Schema:
    fields: dict[Any, Any] = dict(extra or {})
    for key, selector in _field_selectors(unit, exports=exports).items():
        fields[_suggested(key, prefill.get(key))] = selector
    return vol.Schema(fields)


def _version_from_form(user_input: dict[str, Any], effective_from: date) -> dict[str, Any] | None:
    """The version dict for the rate form, None if both rates were left empty.

    Raises ``ValueError`` when only one of supply and delivery is given.
    """
    rates = [user_input.get(key) for key in RATE_FIELDS]
    if all(r is None for r in rates):
        return None
    if any(r is None for r in rates):
        raise ValueError("rates_both")
    version = {"effective_from": effective_from.isoformat()}
    for key in (*RATE_FIELDS, *CHARGE_FIELDS):
        version[key] = float(user_input.get(key) or 0.0)
    return version_to_dict(version_from_dict(version))


def _prefill_from(version: TariffVersion | None) -> dict[str, Any]:
    if version is None:
        return {}
    return {
        "supply": version.supply.default,
        "delivery": version.delivery.default,
        "fixed_monthly": version.fixed_monthly,
        "fixed_daily": version.fixed_daily,
        "tax_pct": version.tax_pct,
        "export_rate": version.export_rate,
    }


def _carry_over(new: dict[str, Any], base: TariffVersion) -> dict[str, Any]:
    """Keep the base version's tiers, riders, and seasonal rates whose default is unchanged."""
    form = ("effective_from", *RATE_FIELDS, *CHARGE_FIELDS)
    merged = {**version_to_dict(base), **{key: new[key] for key in form}}
    for key in RATE_FIELDS:
        spec: RateSpec = getattr(base, key)
        if spec.seasonal and new[key] == spec.default:
            merged[key] = version_to_dict(base)[key]
    return version_to_dict(version_from_dict(merged))


def cycle_day_from(last_bill_end: date) -> int:
    """The meter-read day implied by the last bill's (inclusive) end date."""
    return min(28, (last_bill_end + timedelta(days=1)).day)


def _account_label(account: OpowerAccount) -> str:
    return f"{account.title}: {account.consumption_stat}"


async def _stat_units(hass: Any, statistic_ids: list[str | None]) -> dict[str, str | None]:
    """Each listed statistic's unit; statistics the recorder does not know are absent."""
    wanted = {s for s in statistic_ids if s}
    if not wanted:
        return {}
    listed = await async_list_statistic_ids(hass, statistic_ids=wanted)
    return {s["statistic_id"]: s.get("statistics_unit_of_measurement") for s in listed}


def _is_gas_unit(unit: str | None) -> bool:
    return unit is not None and (unit in VOLUME.units or unit in ENERGY.units)


def _is_gas_only_unit(unit: str | None) -> bool:
    """A unit no electric meter reports in: a volume, or therms."""
    return unit is not None and (unit in VOLUME.units or unit == THERM)


async def _gas_error(hass: Any, statistic_id: str) -> str | None:
    units = await _stat_units(hass, [statistic_id])
    return None if _is_gas_unit(units.get(statistic_id)) else "not_a_gas_statistic"


async def _electric_error(hass: Any, statistic_ids: list[str | None]) -> str | None:
    units = await _stat_units(hass, statistic_ids)
    if any(_is_gas_only_unit(u) for u in units.values()):
        return "not_an_electric_statistic"
    return None


def _meter(data: Any) -> Meter:
    return Meter(data.get(CONF_METER, Meter.ELECTRIC))


class CheckMyBillConfigFlow(ConfigFlow, domain=DOMAIN):
    """Grid -> utility -> (cycle) -> tariff."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._accounts: list[OpowerAccount] = []
        self._account: OpowerAccount | None = None
        self._reload_opower: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> CheckMyBillOptionsFlow:
        """Rates and settings."""
        return CheckMyBillOptionsFlow()

    @property
    def _gas(self) -> bool:
        return self._data.get(CONF_METER) == Meter.GAS

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Meter type, then the grid statistics pre-filled from the Energy dashboard."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if Meter(user_input.get(CONF_METER, Meter.ELECTRIC)) is Meter.GAS:
                self._data = {CONF_METER: Meter.GAS.value}
                return await self.async_step_gas()
            imports = user_input.get(CONF_IMPORT_STAT)
            export = user_input.get(CONF_EXPORT_STAT)
            if not imports:
                errors[CONF_IMPORT_STAT] = "import_required"
            elif error := await _electric_error(self.hass, [imports, export]):
                errors["base"] = error
            else:
                # Electric entries carry no meter or unit: absent means electric, kWh.
                self._data = {CONF_IMPORT_STAT: imports}
                if export:
                    self._data[CONF_EXPORT_STAT] = export
                return await self._async_step_accounts()

        pairs = await grid_statistics(self.hass)
        first = pairs[0] if pairs else None
        schema = vol.Schema(
            {
                vol.Required(CONF_METER, default=Meter.ELECTRIC.value): _METER,
                _suggested(CONF_IMPORT_STAT, first and first.import_stat): _STAT,
                _suggested(CONF_EXPORT_STAT, first and first.export_stat): _STAT,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_gas(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The gas consumption statistic, pre-filled from the Energy dashboard."""
        errors: dict[str, str] = {}
        if user_input is not None:
            imports = user_input[CONF_IMPORT_STAT]
            if error := await _gas_error(self.hass, imports):
                errors["base"] = error
            else:
                self._data[CONF_IMPORT_STAT] = imports
                return await self._async_step_accounts()

        sources = await gas_statistics(self.hass)
        schema = vol.Schema(
            {_suggested(CONF_IMPORT_STAT, sources[0] if sources else None, required=True): _STAT}
        )
        return self.async_show_form(step_id="gas", data_schema=schema, errors=errors)

    async def _async_step_accounts(self) -> ConfigFlowResult:
        self._accounts = await opower_accounts(self.hass, _meter(self._data))
        if self._accounts:
            return await self.async_step_utility()
        return await self._async_step_after_utility()

    async def _async_step_after_utility(self) -> ConfigFlowResult:
        """Units for gas; then the cycle unless the date sensors give it."""
        if self._gas and CONF_UNIT not in self._data:
            return await self.async_step_units()
        if CONF_CYCLE_START_ENTITY in self._data:
            return await self.async_step_tariff()
        return await self.async_step_cycle()

    def _disabled_by_integration(self, account: OpowerAccount) -> list[str]:
        registry = er.async_get(self.hass)
        return [
            entity_id
            for entity_id in (account.start_entity, account.end_entity)
            if entity_id
            and (entry := registry.async_get(entity_id)) is not None
            and entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        ]

    def _disabled_by_user(self, account: OpowerAccount) -> bool:
        registry = er.async_get(self.hass)
        return any(
            (entry := registry.async_get(entity_id)) is not None
            and entry.disabled_by is not None
            and entry.disabled_by is not er.RegistryEntryDisabler.INTEGRATION
            for entity_id in (account.start_entity, account.end_entity)
            if entity_id
        )

    async def async_step_utility(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the Opower account; optionally enable its billing-date sensors."""
        if user_input is not None:
            choice = user_input[CONF_ACCOUNT]
            account = next((a for a in self._accounts if a.consumption_stat == choice), None)
            if account is None:
                return await self._async_step_after_utility()
            self._account = account
            self._data[CONF_UTILITY_IMPORT_STAT] = account.consumption_stat
            if account.return_stat:
                self._data[CONF_UTILITY_EXPORT_STAT] = account.return_stat

            usable = (
                account.start_entity is not None
                and account.end_entity is not None
                and not self._disabled_by_user(account)
            )
            if usable and (disabled := self._disabled_by_integration(account)):
                if user_input.get(CONF_ENABLE_DATE_SENSORS, True):
                    registry = er.async_get(self.hass)
                    for entity_id in disabled:
                        registry.async_update_entity(entity_id, disabled_by=None)
                    self._reload_opower = account.config_entry_id
                else:
                    usable = False
            if usable:
                self._data[CONF_CYCLE_START_ENTITY] = account.start_entity
                self._data[CONF_CYCLE_END_ENTITY] = account.end_entity
            return await self._async_step_after_utility()

        options = [
            SelectOptionDict(value=a.consumption_stat, label=_account_label(a))
            for a in self._accounts
        ]
        options.append(SelectOptionDict(value=NO_UTILITY, label=NO_UTILITY))
        fields: dict[Any, Any] = {
            vol.Required(CONF_ACCOUNT, default=self._accounts[0].consumption_stat): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    mode=SelectSelectorMode.LIST,
                    translation_key=CONF_ACCOUNT,
                )
            )
        }
        if any(self._disabled_by_integration(a) for a in self._accounts):
            fields[vol.Required(CONF_ENABLE_DATE_SENSORS, default=True)] = BooleanSelector()
        return self.async_show_form(step_id="utility", data_schema=vol.Schema(fields))

    async def async_step_units(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Gas: the billing unit, and the utility statistic's real unit (gas.md §2.1)."""
        utility = self._data.get(CONF_UTILITY_IMPORT_STAT)
        if user_input is not None:
            self._data[CONF_UNIT] = user_input[CONF_UNIT]
            if utility and (utility_unit := user_input.get(CONF_UTILITY_UNIT)):
                self._data[CONF_UTILITY_UNIT] = utility_unit
            return await self._async_step_after_utility()

        fields: dict[Any, Any] = {vol.Required(CONF_UNIT, default=THERM): _GAS_UNIT}
        if utility:
            guess = account_unit(self.hass, utility)
            if guess is None:
                guess = (await _stat_units(self.hass, [utility])).get(utility)
            fields[vol.Required(CONF_UTILITY_UNIT, default=guess or CCF)] = _GAS_UNIT
        return self.async_show_form(step_id="units", data_schema=vol.Schema(fields))

    async def async_step_cycle(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manual cycle: the meter-read day, or the last bill's end date."""
        errors: dict[str, str] = {}
        if user_input is not None:
            day = user_input.get(CONF_CYCLE_DAY)
            last = user_input.get(CONF_LAST_BILL_END)
            if (day is None) == (last is None):
                errors["base"] = "cycle_one"
            else:
                self._data[CONF_CYCLE_DAY] = (
                    int(day) if day is not None else cycle_day_from(date.fromisoformat(last))
                )
                return await self.async_step_tariff()

        schema = vol.Schema(
            {vol.Optional(CONF_CYCLE_DAY): _CYCLE_DAY, vol.Optional(CONF_LAST_BILL_END): _DATE}
        )
        return self.async_show_form(step_id="cycle", data_schema=schema, errors=errors)

    async def async_step_tariff(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Rates; leaving them empty validates quantities only."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                version = _version_from_form(user_input, dt_util.now().date())
            except ValueError as err:
                errors["base"] = str(err) if str(err) == "rates_both" else "invalid_tariff"
            else:
                export_mode = (
                    ExportMode.NONE
                    if self._gas
                    else user_input.get(CONF_EXPORT_MODE, ExportMode.NONE)
                )
                return await self._async_create(
                    tariff=[version] if version else [], export_mode=export_mode
                )

        extra = (
            {}
            if self._gas
            else {vol.Required(CONF_EXPORT_MODE, default=ExportMode.NONE.value): _EXPORT_MODE}
        )
        schema = _rates_schema(
            {}, unit=self._data.get(CONF_UNIT, KWH), exports=not self._gas, extra=extra
        )
        return self.async_show_form(step_id="tariff", data_schema=schema, errors=errors)

    async def _async_create(
        self, tariff: list[dict[str, Any]], export_mode: str
    ) -> ConfigFlowResult:
        unique_id = self._data.get(CONF_UTILITY_IMPORT_STAT) or self._data[CONF_IMPORT_STAT]
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        if self._reload_opower:
            self.hass.config_entries.async_schedule_reload(self._reload_opower)
        base = GAS_TITLE if self._gas else TITLE
        title = f"{base} ({self._account.title})" if self._account else base
        return self.async_create_entry(
            title=title,
            data=self._data,
            options={
                CONF_TARIFF: tariff,
                CONF_EXPORT_MODE: export_mode,
                CONF_TOLERANCE: DEFAULT_GAS_TOLERANCE_PCT if self._gas else DEFAULT_TOLERANCE_PCT,
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the statistics or the cycle source."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        current = dict(entry.data)
        gas = _meter(entry.data) is Meter.GAS
        if user_input is not None:
            start = user_input.get(CONF_CYCLE_START_ENTITY)
            end = user_input.get(CONF_CYCLE_END_ENTITY)
            if bool(start) != bool(end):
                errors["base"] = "cycle_pair"
            elif not start and user_input.get(CONF_CYCLE_DAY) is None:
                errors["base"] = "cycle_one"
            elif error := (
                await _gas_error(self.hass, user_input[CONF_IMPORT_STAT])
                if gas
                else await _electric_error(
                    self.hass,
                    [user_input[CONF_IMPORT_STAT], user_input.get(CONF_EXPORT_STAT)],
                )
            ):
                errors["base"] = error
            else:
                # The meter type and setup-time units are kept; gas.md §2.3.
                kept = {
                    k: entry.data[k]
                    for k in (CONF_METER, CONF_UNIT, CONF_UTILITY_UNIT)
                    if k in entry.data
                }
                data = {**kept, **{k: v for k, v in user_input.items() if v not in (None, "")}}
                if not data.get(CONF_UTILITY_IMPORT_STAT):
                    data.pop(CONF_UTILITY_UNIT, None)
                if CONF_CYCLE_DAY in data:
                    data[CONF_CYCLE_DAY] = int(data[CONF_CYCLE_DAY])
                unique_id = data.get(CONF_UTILITY_IMPORT_STAT) or data[CONF_IMPORT_STAT]
                if unique_id != entry.unique_id and any(
                    e.unique_id == unique_id
                    for e in self._async_current_entries(include_ignore=False)
                    if e.entry_id != entry.entry_id
                ):
                    return self.async_abort(reason="already_configured")
                return self.async_update_reload_and_abort(entry, unique_id=unique_id, data=data)
            current = user_input

        fields: dict[Any, Any] = {
            _suggested(CONF_IMPORT_STAT, current.get(CONF_IMPORT_STAT), required=True): _STAT,
        }
        if not gas:
            fields[_suggested(CONF_EXPORT_STAT, current.get(CONF_EXPORT_STAT))] = _STAT
        fields[_suggested(CONF_UTILITY_IMPORT_STAT, current.get(CONF_UTILITY_IMPORT_STAT))] = _STAT
        if not gas:
            fields[_suggested(CONF_UTILITY_EXPORT_STAT, current.get(CONF_UTILITY_EXPORT_STAT))] = (
                _STAT
            )
        fields[_suggested(CONF_CYCLE_START_ENTITY, current.get(CONF_CYCLE_START_ENTITY))] = _SENSOR
        fields[_suggested(CONF_CYCLE_END_ENTITY, current.get(CONF_CYCLE_END_ENTITY))] = _SENSOR
        fields[_suggested(CONF_CYCLE_DAY, current.get(CONF_CYCLE_DAY))] = _CYCLE_DAY
        if gas:
            fields[vol.Required(CONF_UNIT, default=current.get(CONF_UNIT, THERM))] = _GAS_UNIT
            fields[_suggested(CONF_UTILITY_UNIT, current.get(CONF_UTILITY_UNIT))] = _GAS_UNIT
        return self.async_show_form(
            step_id="reconfigure", data_schema=vol.Schema(fields), errors=errors
        )


class CheckMyBillOptionsFlow(OptionsFlow):
    """Rate versions and settings."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """What to change."""
        return self.async_show_menu(
            step_id="init", menu_options=["rates", "rate_versions", "settings"]
        )

    def _save(self, **changes: Any) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **changes})

    def _versions(self) -> list[dict[str, Any]]:
        """Stored versions in the current key format; legacy keys are rewritten on save."""
        versions = []
        for raw in self.config_entry.options.get(CONF_TARIFF) or []:
            try:
                versions.append(version_to_dict(version_from_dict(raw)))
            except AttributeError, KeyError, TypeError, ValueError:
                versions.append(raw)
        return versions

    @property
    def _gas(self) -> bool:
        return _meter(self.config_entry.data) is Meter.GAS

    def _export_mode(self) -> str:
        if self._gas:
            return ExportMode.NONE.value
        return self.config_entry.options.get(CONF_EXPORT_MODE) or ExportMode.NONE.value

    async def async_step_rates(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add a rate version, or change the one starting on the same day."""
        tariff = tariff_from_options(self._versions(), self._export_mode())
        base = tariff.version_on(dt_util.now().date()) if tariff else None
        errors: dict[str, str] = {}
        if user_input is not None:
            effective = date.fromisoformat(user_input[CONF_EFFECTIVE_FROM])
            try:
                version = _version_from_form(user_input, effective)
            except ValueError as err:
                errors["base"] = str(err) if str(err) == "rates_both" else "invalid_tariff"
            else:
                if version is None:
                    errors["base"] = "rates_both"
                else:
                    if base is not None:
                        version = _carry_over(version, base)
                    versions = [
                        v
                        for v in self._versions()
                        if v["effective_from"] != version["effective_from"]
                    ]
                    versions.append(version)
                    versions.sort(key=lambda v: v["effective_from"])
                    return self._save(**{CONF_TARIFF: versions})

        schema = _rates_schema(
            _prefill_from(base),
            unit=self.config_entry.data.get(CONF_UNIT, KWH),
            exports=not self._gas,
            extra={
                vol.Required(CONF_EFFECTIVE_FROM, default=dt_util.now().date().isoformat()): _DATE
            },
        )
        return self.async_show_form(step_id="rates", data_schema=schema, errors=errors)

    async def async_step_rate_versions(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Every rate version, with tiers, seasonal rates and riders, as YAML."""
        errors: dict[str, str] = {}
        if user_input is not None:
            raw = user_input.get(CONF_VERSIONS) or []
            try:
                if not isinstance(raw, list):
                    raise TypeError("expected a list of versions")
                tariff = tariff_from_options(raw, self._export_mode())
            except AttributeError, KeyError, TypeError, ValueError:
                errors["base"] = "invalid_tariff"
            else:
                versions = [version_to_dict(v) for v in tariff.versions] if tariff else []
                return self._save(**{CONF_TARIFF: versions})

        schema = vol.Schema({_suggested(CONF_VERSIONS, self._versions()): ObjectSelector()})
        return self.async_show_form(step_id="rate_versions", data_schema=schema, errors=errors)

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Tolerance, and the export mode for electric."""
        if user_input is not None:
            changes: dict[str, Any] = {CONF_TOLERANCE: float(user_input[CONF_TOLERANCE])}
            if CONF_EXPORT_MODE in user_input:
                changes[CONF_EXPORT_MODE] = user_input[CONF_EXPORT_MODE]
            return self._save(**changes)
        options = self.config_entry.options
        tolerance = DEFAULT_GAS_TOLERANCE_PCT if self._gas else DEFAULT_TOLERANCE_PCT
        fields: dict[Any, Any] = {
            vol.Required(CONF_TOLERANCE, default=options.get(CONF_TOLERANCE, tolerance)): (
                _TOLERANCE
            )
        }
        if not self._gas:
            fields[vol.Required(CONF_EXPORT_MODE, default=self._export_mode())] = _EXPORT_MODE
        return self.async_show_form(step_id="settings", data_schema=vol.Schema(fields))
