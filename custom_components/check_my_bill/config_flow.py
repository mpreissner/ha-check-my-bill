"""Config flow, reconfigure and options flow (docs/specs/config-flow.md)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import voluptuous as vol
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
    CONF_TARIFF,
    CONF_TOLERANCE,
    CONF_UTILITY_EXPORT_STAT,
    CONF_UTILITY_IMPORT_STAT,
    DEFAULT_TOLERANCE_PCT,
    DOMAIN,
)
from .core.serde import tariff_from_options, version_from_dict, version_to_dict
from .core.tariff import ExportMode, RateSpec, TariffVersion
from .discovery import OpowerAccount, grid_statistics, opower_accounts

TITLE = "Check My Bill"
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


_FIELD_SELECTORS = {
    "supply": _money("$/kWh"),
    "delivery": _money("$/kWh"),
    "fixed_monthly": _money("$"),
    "fixed_daily": _money("$"),
    "tax_pct": _money("%"),
    "export_rate": _money("$/kWh"),
}


def _suggested(key: str, value: Any, *, required: bool = False) -> vol.Marker:
    """A field pre-filled with ``value`` that the user can clear (unless required)."""
    marker = vol.Required if required else vol.Optional
    if value is None:
        return marker(key)
    return marker(key, description={"suggested_value": value})


def _rates_schema(prefill: dict[str, Any], *, extra: dict[Any, Any] | None = None) -> vol.Schema:
    fields: dict[Any, Any] = dict(extra or {})
    for key, selector in _FIELD_SELECTORS.items():
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

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Grid import and export statistics, pre-filled from the Energy dashboard."""
        if user_input is not None:
            self._data[CONF_IMPORT_STAT] = user_input[CONF_IMPORT_STAT]
            if export := user_input.get(CONF_EXPORT_STAT):
                self._data[CONF_EXPORT_STAT] = export
            self._accounts = await opower_accounts(self.hass)
            if self._accounts:
                return await self.async_step_utility()
            return await self.async_step_cycle()

        pairs = await grid_statistics(self.hass)
        first = pairs[0] if pairs else None
        schema = vol.Schema(
            {
                _suggested(CONF_IMPORT_STAT, first and first.import_stat, required=True): _STAT,
                _suggested(CONF_EXPORT_STAT, first and first.export_stat): _STAT,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

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
                return await self.async_step_cycle()
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
            if not usable:
                return await self.async_step_cycle()
            self._data[CONF_CYCLE_START_ENTITY] = account.start_entity
            self._data[CONF_CYCLE_END_ENTITY] = account.end_entity
            return await self.async_step_tariff()

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
        """Rates; leaving them empty validates kWh only."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                version = _version_from_form(user_input, dt_util.now().date())
            except ValueError as err:
                errors["base"] = str(err) if str(err) == "rates_both" else "invalid_tariff"
            else:
                return await self._async_create(
                    tariff=[version] if version else [],
                    export_mode=user_input.get(CONF_EXPORT_MODE, ExportMode.NONE),
                )

        schema = _rates_schema(
            {},
            extra={vol.Required(CONF_EXPORT_MODE, default=ExportMode.NONE.value): _EXPORT_MODE},
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
        title = f"{TITLE} ({self._account.title})" if self._account else TITLE
        return self.async_create_entry(
            title=title,
            data=self._data,
            options={
                CONF_TARIFF: tariff,
                CONF_EXPORT_MODE: export_mode,
                CONF_TOLERANCE: DEFAULT_TOLERANCE_PCT,
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the statistics or the cycle source."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        current = dict(entry.data)
        if user_input is not None:
            start = user_input.get(CONF_CYCLE_START_ENTITY)
            end = user_input.get(CONF_CYCLE_END_ENTITY)
            if bool(start) != bool(end):
                errors["base"] = "cycle_pair"
            elif not start and user_input.get(CONF_CYCLE_DAY) is None:
                errors["base"] = "cycle_one"
            else:
                data = {k: v for k, v in user_input.items() if v not in (None, "")}
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

        schema = vol.Schema(
            {
                _suggested(CONF_IMPORT_STAT, current.get(CONF_IMPORT_STAT), required=True): _STAT,
                _suggested(CONF_EXPORT_STAT, current.get(CONF_EXPORT_STAT)): _STAT,
                _suggested(CONF_UTILITY_IMPORT_STAT, current.get(CONF_UTILITY_IMPORT_STAT)): _STAT,
                _suggested(CONF_UTILITY_EXPORT_STAT, current.get(CONF_UTILITY_EXPORT_STAT)): _STAT,
                _suggested(CONF_CYCLE_START_ENTITY, current.get(CONF_CYCLE_START_ENTITY)): _SENSOR,
                _suggested(CONF_CYCLE_END_ENTITY, current.get(CONF_CYCLE_END_ENTITY)): _SENSOR,
                _suggested(CONF_CYCLE_DAY, current.get(CONF_CYCLE_DAY)): _CYCLE_DAY,
            }
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)


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
        return list(self.config_entry.options.get(CONF_TARIFF) or [])

    def _export_mode(self) -> str:
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
        """Tolerance and export mode."""
        if user_input is not None:
            return self._save(
                **{
                    CONF_TOLERANCE: float(user_input[CONF_TOLERANCE]),
                    CONF_EXPORT_MODE: user_input[CONF_EXPORT_MODE],
                }
            )
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TOLERANCE, default=options.get(CONF_TOLERANCE, DEFAULT_TOLERANCE_PCT)
                ): _TOLERANCE,
                vol.Required(CONF_EXPORT_MODE, default=self._export_mode()): _EXPORT_MODE,
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)
