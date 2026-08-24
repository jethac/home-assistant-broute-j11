"""Smart meter sensors fed by the B-route coordinator."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfElectricCurrent, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import BrouteConfigEntry, BrouteCoordinator
from .protocol.session import MeterReading


@dataclass(frozen=True, kw_only=True)
class BrouteSensorDescription(SensorEntityDescription):
    """Describes a sensor and how to read it from a meter reading."""

    value: Callable[[MeterReading], int | Decimal | None]


SENSORS: tuple[BrouteSensorDescription, ...] = (
    BrouteSensorDescription(
        key="instantaneous_power",
        translation_key="instantaneous_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value=lambda reading: reading.instantaneous_power,
    ),
    BrouteSensorDescription(
        key="instantaneous_current_r",
        translation_key="instantaneous_current_r",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        value=lambda reading: reading.current_r_phase,
    ),
    BrouteSensorDescription(
        key="instantaneous_current_t",
        translation_key="instantaneous_current_t",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        value=lambda reading: reading.current_t_phase,
    ),
    BrouteSensorDescription(
        key="cumulative_forward_energy",
        translation_key="cumulative_forward_energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value=lambda reading: reading.cumulative_forward_energy,
    ),
    BrouteSensorDescription(
        key="cumulative_reverse_energy",
        translation_key="cumulative_reverse_energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value=lambda reading: reading.cumulative_reverse_energy,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BrouteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one sensor per meter property."""
    coordinator = entry.runtime_data
    async_add_entities(
        BrouteSensor(coordinator, description) for description in SENSORS
    )


class BrouteSensor(CoordinatorEntity[BrouteCoordinator], SensorEntity):
    """One meter property."""

    _attr_has_entity_name = True
    entity_description: BrouteSensorDescription

    def __init__(
        self, coordinator: BrouteCoordinator, description: BrouteSensorDescription
    ) -> None:
        """Bind the sensor to ``coordinator``."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.identifier}_{description.key}"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        """Whether the last poll produced a value for this property.

        A property the meter does not advertise stays unavailable instead of
        reporting a stale or zero value (PRD §6.4).
        """
        return (
            super().available
            and self.entity_description.value(self.coordinator.data) is not None
        )

    @property
    def native_value(self) -> int | Decimal | None:
        """The value of this property in its native unit."""
        return self.entity_description.value(self.coordinator.data)
