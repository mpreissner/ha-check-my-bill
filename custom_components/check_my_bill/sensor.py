"""Sensors for the current and last closed billing cycle (spec §3.6)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BillData, CheckMyBillConfigEntry, CheckMyBillCoordinator
from .core.reconcile import Status

PARALLEL_UPDATES = 0

type StateValue = float | str | date | None


@dataclass(frozen=True, kw_only=True)
class BillSensorDescription(SensorEntityDescription):
    """A sensor read from :class:`BillData`."""

    value_fn: Callable[[BillData], StateValue]
    attrs_fn: Callable[[BillData], Mapping[str, Any] | None] = lambda _: None
    needs_tariff: bool = False


def _last_closed_attrs(data: BillData) -> Mapping[str, Any] | None:
    record = data.last_closed
    if record is None:
        return None
    return {
        "start": record.start.isoformat(),
        "end": record.cycle.last_day.isoformat(),
        "estimated_dates": record.estimated,
        "metered": round(record.metered, 3),
        "utility": None if record.utility is None else round(record.utility, 3),
        "status": record.status.value,
        "deviation_pct": None if record.deviation_pct is None else round(record.deviation_pct, 2),
        "cost_basis": record.cost_basis,
    }


def _current_attrs(data: BillData) -> Mapping[str, Any]:
    return {
        "estimated_dates": data.current.estimated,
        "cost_basis": data.current.cost_basis,
    }


SENSORS: tuple[BillSensorDescription, ...] = (
    BillSensorDescription(
        key="cycle_import",
        translation_key="cycle_import",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=lambda d: round(d.current.metered, 3),
    ),
    BillSensorDescription(
        key="cycle_expected_cost",
        translation_key="cycle_expected_cost",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        needs_tariff=True,
        value_fn=lambda d: None if d.current.cost is None else round(d.current.cost.total, 2),
        attrs_fn=_current_attrs,
    ),
    BillSensorDescription(
        key="cycle_projected_cost",
        translation_key="cycle_projected_cost",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        needs_tariff=True,
        value_fn=lambda d: None if d.projected_cost is None else round(d.projected_cost.total, 2),
    ),
    BillSensorDescription(
        key="cycle_status",
        translation_key="cycle_status",
        device_class=SensorDeviceClass.ENUM,
        options=[s.value for s in Status],
        value_fn=lambda d: d.current.status.value,
    ),
    BillSensorDescription(
        key="cycle_start",
        translation_key="cycle_start",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda d: d.current.start,
        attrs_fn=_current_attrs,
    ),
    BillSensorDescription(
        key="cycle_end",
        translation_key="cycle_end",
        device_class=SensorDeviceClass.DATE,
        # The inclusive last day, as the utility prints it on the bill.
        value_fn=lambda d: d.current.cycle.last_day,
    ),
    BillSensorDescription(
        key="last_cycle_expected_cost",
        translation_key="last_cycle_expected_cost",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        needs_tariff=True,
        value_fn=lambda d: (
            None
            if d.last_closed is None or d.last_closed.cost is None
            else round(d.last_closed.cost.total, 2)
        ),
        attrs_fn=_last_closed_attrs,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CheckMyBillConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors. Cost sensors exist only when a tariff is configured."""
    coordinator = entry.runtime_data
    async_add_entities(
        BillSensor(coordinator, description)
        for description in SENSORS
        if coordinator.tariff is not None or not description.needs_tariff
    )


class BillSensor(CoordinatorEntity[CheckMyBillCoordinator], SensorEntity):
    """One value from the coordinator's data."""

    _attr_has_entity_name = True
    entity_description: BillSensorDescription

    def __init__(
        self, coordinator: CheckMyBillCoordinator, description: BillSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
            name=entry.title,
        )
        if description.device_class is SensorDeviceClass.MONETARY:
            self._attr_native_unit_of_measurement = coordinator.hass.config.currency

    @property
    def native_value(self) -> StateValue:
        """The value for the current data."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Context for the value."""
        return self.entity_description.attrs_fn(self.coordinator.data)
