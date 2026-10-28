"""Water sensors."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import WattsCoordinator
from .calc import Summary
from .const import DOMAIN


@dataclass(frozen=True, kw_only=True)
class WattsSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Summary, WattsCoordinator], Any]
    attrs_fn: Callable[[Summary], dict[str, Any]] | None = None


def _volume(**kwargs: Any) -> dict[str, Any]:
    return {
        "device_class": SensorDeviceClass.WATER,
        "native_unit_of_measurement": UnitOfVolume.CUBIC_METERS,
        "suggested_display_precision": 3,
        **kwargs,
    }


HISTORY_DAYS = 30  # attributes over 16 KB are not stored by the recorder; this stays ~3 KB


def _history_attributes(summary: Summary) -> dict[str, Any]:
    """Recent daily (local date) and hourly (local start time) volumes in m³."""
    days = sorted(summary.daily)[-HISTORY_DAYS:]
    return {
        "daily": {d.isoformat(): round(summary.daily[d], 4) for d in days},
        "hourly": {
            dt_util.as_local(start).isoformat(): round(value, 4)
            for start, value in summary.recent_hours
        },
    }


DESCRIPTIONS: tuple[WattsSensorDescription, ...] = (
    WattsSensorDescription(
        key="consumption",
        name="Consumption",
        **_volume(state_class=SensorStateClass.TOTAL_INCREASING),
        value_fn=lambda s, c: s.consumption,
        attrs_fn=_history_attributes,
    ),
    WattsSensorDescription(
        key="last_full_day",
        name="Last full day",
        **_volume(),
        value_fn=lambda s, c: s.last_full_day,
        attrs_fn=lambda s: {"date": s.last_full_day_date.isoformat() if s.last_full_day_date else None},
    ),
    WattsSensorDescription(
        key="last_7_days",
        name="Last 7 days",
        **_volume(),
        value_fn=lambda s, c: s.last_7_days,
    ),
    WattsSensorDescription(
        key="this_month",
        name="This month",
        **_volume(),
        value_fn=lambda s, c: s.this_month,
    ),
    WattsSensorDescription(
        key="this_year",
        name="This year",
        **_volume(),
        value_fn=lambda s, c: s.this_year,
    ),
    WattsSensorDescription(
        key="data_until",
        name="Data until",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s, c: s.data_until,
    ),
    WattsSensorDescription(
        key="token_expires",
        name="Token expires",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s, c: c.api.expires_at,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WattsCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        WattsSensor(coordinator, device_id, description)
        for device_id in coordinator.device_ids
        for description in DESCRIPTIONS
    )


class WattsSensor(CoordinatorEntity[WattsCoordinator], SensorEntity):
    _attr_has_entity_name = True
    # Keep the bulky history out of the database; it is rebuilt on every update.
    _unrecorded_attributes = frozenset({"daily", "hourly"})
    entity_description: WattsSensorDescription

    def __init__(
        self, coordinator: WattsCoordinator, device_id: str, description: WattsSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name="Novafos water",
            manufacturer="Novafos",
        )

    @property
    def _summary(self) -> Summary | None:
        return self.coordinator.data.get(self._device_id)

    @property
    def native_value(self) -> float | datetime | None:
        summary = self._summary
        if summary is None or (summary.data_until is None and self.entity_description.key != "token_expires"):
            return None
        value = self.entity_description.value_fn(summary, self.coordinator)
        return round(value, 4) if isinstance(value, float) else value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self._summary) if fn and self._summary else None
