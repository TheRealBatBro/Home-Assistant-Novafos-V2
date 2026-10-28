"""Novafos water data from the Watts On backend."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import WattsApi, WattsApiError, WattsAuthError, ci_first
from .const import (
    CONF_DEVICE_IDS,
    CONF_REFRESH_TOKEN,
    CONF_UNIT,
    DOMAIN,
    SCAN_INTERVAL,
    UNIT_LITER,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]

START_KEYS = ("startDate", "start", "from", "timestamp", "time")
END_KEYS = ("endDate", "end", "to")
VALUE_KEYS = ("value", "consumption", "volume", "amount")


@dataclass
class DeviceData:
    total: float
    today: float
    last_reading: datetime | None


def _parse_item(item: dict[str, Any]) -> tuple[datetime, float] | None:
    """Return (interval end, value) for one consumption record, or None."""
    value = ci_first(item, *VALUE_KEYS)
    value = value if isinstance(value, (int, float)) else None
    stamp = ci_first(item, *END_KEYS, *START_KEYS)
    end = dt_util.parse_datetime(str(stamp)) if stamp else None
    if value is None or end is None or value < 0:  # the app treats -1 as "no data"
        return None
    return dt_util.as_utc(end) if end.tzinfo else end.replace(tzinfo=dt_util.UTC), float(value)


class WattsCoordinator(DataUpdateCoordinator[dict[str, DeviceData]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self.entry = entry
        self.device_ids: list[str] = entry.data[CONF_DEVICE_IDS]
        self.factor = 0.001 if entry.data.get(CONF_UNIT) == UNIT_LITER else 1.0
        self._store = Store(hass, 1, f"{DOMAIN}_{entry.entry_id}")
        self._state: dict[str, Any] = {}

        async def save_token(token: str) -> None:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_REFRESH_TOKEN: token}
            )

        self.api = WattsApi(
            async_get_clientsession(hass), entry.data[CONF_REFRESH_TOKEN], save_token
        )

    async def _async_update_data(self) -> dict[str, DeviceData]:
        if not self._state:
            self._state = await self._store.async_load() or {}
        try:
            await self.api._ensure_token()
            result = {}
            for device_id in self.device_ids:
                result[device_id] = await self._update_device(device_id)
        except WattsAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except WattsApiError as err:
            raise UpdateFailed(str(err)) from err
        await self._store.async_save(self._state)
        return result

    async def _update_device(self, device_id: str) -> DeviceData:
        items = sorted(
            filter(None, map(_parse_item, await self.api.async_recent_consumption(device_id)))
        )
        saved = self._state.setdefault(device_id, {"total": 0.0, "last_end": None})
        last_end = dt_util.parse_datetime(saved["last_end"]) if saved["last_end"] else None

        # Cumulative total: only add intervals we have not counted yet.
        for end, value in items:
            if last_end is None or end > last_end:
                saved["total"] += value * self.factor
                last_end = end
        saved["last_end"] = last_end.isoformat() if last_end else None

        today = dt_util.now().date()
        today_sum = sum(
            v * self.factor for end, v in items if dt_util.as_local(end).date() == today
        )
        return DeviceData(round(saved["total"], 4), round(today_sum, 4), last_end)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = WattsCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return ok
