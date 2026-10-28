"""Water sensors."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import WattsCoordinator
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WattsCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for device_id in coordinator.device_ids:
        entities += [
            WaterTotal(coordinator, device_id),
            WaterToday(coordinator, device_id),
            LastReading(coordinator, device_id),
        ]
    async_add_entities(entities)


class _Base(CoordinatorEntity[WattsCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _suffix = ""

    def __init__(self, coordinator: WattsCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_{self._suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name="Novafos water meter",
            manufacturer="Novafos",
            via_device=None,
        )

    @property
    def _data(self):
        return self.coordinator.data.get(self._device_id)


class WaterTotal(_Base):
    _suffix = "total"
    _attr_name = "Water total"
    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_suggested_display_precision = 3

    @property
    def native_value(self) -> float | None:
        return self._data.total if self._data else None


class WaterToday(_Base):
    _suffix = "today"
    _attr_name = "Water today"
    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_suggested_display_precision = 3

    @property
    def native_value(self) -> float | None:
        return self._data.today if self._data else None

    @property
    def last_reset(self) -> datetime:
        return dt_util.start_of_local_day()


class LastReading(_Base):
    _suffix = "last_reading"
    _attr_name = "Last reading"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        return self._data.last_reading if self._data else None
