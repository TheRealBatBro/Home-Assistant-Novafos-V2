"""Novafos water data from the Watts On backend."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_import_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import WattsApi, WattsApiError, WattsAuthError, ci_first
from .calc import Summary, parse_utc, summarize
from .const import (
    CONF_DEVICE_IDS,
    CONF_REFRESH_TOKEN,
    CONF_UNIT,
    DOMAIN,
    SCAN_INTERVAL,
    STATISTICS_SOURCE,
    UNIT_LITER,
)

try:  # HA 2025.4+ requires mean_type in the statistics metadata
    from homeassistant.components.recorder.models import StatisticMeanType

    _MEAN: dict[str, Any] = {"mean_type": StatisticMeanType.NONE}
except ImportError:
    _MEAN = {"has_mean": False}

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]

# The API uses short names (sd/ed/vol/mq); the long ones are fallbacks.
START_KEYS = ("sd", "startDate", "start")
VALUE_KEYS = ("vol", "volume", "value")
QUALITY_KEYS = ("mq", "measuredQuality")


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_item(item: dict[str, Any]) -> tuple[datetime, float] | None:
    """Return (interval start, volume) for a final, measured record, else None."""
    value = ci_first(item, *VALUE_KEYS)
    stamp = ci_first(item, *START_KEYS)
    if not isinstance(value, (int, float)) or value < 0 or not stamp:  # -1 means "no data"
        return None
    quality = ci_first(item, *QUALITY_KEYS)
    if quality is not None and str(quality).lower() != "measured":
        return None
    start = dt_util.parse_datetime(str(stamp))
    return (dt_util.as_utc(start), float(value)) if start else None


class WattsCoordinator(DataUpdateCoordinator[dict[str, Summary]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self.entry = entry
        self.device_ids: list[str] = entry.data[CONF_DEVICE_IDS]
        self.factor = 0.001 if entry.data.get(CONF_UNIT) == UNIT_LITER else 1.0
        self._store = Store(hass, 1, f"{DOMAIN}_history_{entry.entry_id}")
        self._state: dict[str, Any] | None = None

        async def save_token(token: str) -> None:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_REFRESH_TOKEN: token}
            )

        self.api = WattsApi(
            async_get_clientsession(hass), entry.data[CONF_REFRESH_TOKEN], save_token
        )

    def statistic_id(self, device_id: str) -> str:
        first = device_id == self.device_ids[0]
        return f"{STATISTICS_SOURCE}:water_consumption" + ("" if first else f"_{device_id}")

    async def _async_update_data(self) -> dict[str, Summary]:
        if self._state is None:
            self._state = await self._store.async_load() or {}
        try:
            result = {d: await self._update_device(d) for d in self.device_ids}
        except WattsAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except WattsApiError as err:
            raise UpdateFailed(str(err)) from err
        await self._store.async_save(self._state)
        return result

    async def _update_device(self, device_id: str) -> Summary:
        assert self._state is not None
        dev = self._state.setdefault(device_id, {"hourly": {}, "imported": None})
        hourly: dict[str, float] = dev["hourly"]

        # Fetch from local midnight two days before the newest stored hour (so late
        # corrections are picked up and whole days are always replaced), or on the
        # first run from the start of this year, which month/year totals need.
        if hourly:
            newest = parse_utc(max(hourly))
            start = dt_util.start_of_local_day(dt_util.as_local(newest) - timedelta(days=2))
        else:
            start = dt_util.start_of_local_day(dt_util.now().replace(month=1, day=1))
        parsed = list(
            filter(None, map(_parse_item, await self.api.async_consumptions(device_id, start)))
        )
        if not hourly and parsed and min(p[0] for p in parsed) > start + timedelta(days=3):
            _LOGGER.warning(
                "Asked for data from %s but the API returned from %s; history is truncated",
                start,
                min(p[0] for p in parsed),
            )
        for begin, value in parsed:
            hourly[_iso(begin)] = value

        summary = summarize(hourly, dt_util.now(), dt_util.DEFAULT_TIME_ZONE, self.factor)
        self._import_statistics(device_id, dev, summary)
        return summary

    @staticmethod
    def _rows(summary: Summary, imported: str | None) -> list[StatisticData]:
        """Hourly rows; sums are cumulative since the first hour. Re-sends 3 days of overlap."""
        cutoff = parse_utc(imported) - timedelta(days=3) if imported else None
        return [
            StatisticData(start=start, state=total, sum=total)
            for start, total in summary.sums
            if cutoff is None or start >= cutoff
        ]

    def _import_statistics(self, device_id: str, dev: dict[str, Any], summary: Summary) -> None:
        """Hourly history for the Energy dashboard, as a statistic of our own."""
        if not summary.sums:
            return
        metadata = StatisticMetaData(
            has_sum=True,
            name="Novafos Water consumption",
            source=STATISTICS_SOURCE,
            statistic_id=self.statistic_id(device_id),
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
            unit_class="volume",
            **_MEAN,
        )
        async_add_external_statistics(self.hass, metadata, self._rows(summary, dev["imported"]))
        dev["imported"] = _iso(summary.sums[-1][0])
        self.import_entity_history(device_id)

    def import_entity_history(self, device_id: str) -> bool:
        """Backfill the consumption sensor's own long-term statistics.

        Only statistics can be backfilled; Home Assistant has no way to write past
        states, so the entity's state graph still starts when it was created.
        The entity may not be registered yet during the first refresh, so setup
        calls this again once the platform is loaded.
        """
        summary = (self.data or {}).get(device_id)
        entity_id = er.async_get(self.hass).async_get_entity_id(
            "sensor", DOMAIN, f"{device_id}_consumption"
        )
        if not summary or not summary.sums or not entity_id or self._state is None:
            return False
        dev = self._state[device_id]
        metadata = StatisticMetaData(
            has_sum=True,
            name=None,
            source="recorder",
            statistic_id=entity_id,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
            unit_class="volume",
            **_MEAN,
        )
        async_import_statistics(self.hass, metadata, self._rows(summary, dev.get("entity_imported")))
        dev["entity_imported"] = _iso(summary.sums[-1][0])
        return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = WattsCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Entities exist now, so the sensor's own statistics can be backfilled.
    if any([coordinator.import_entity_history(d) for d in coordinator.device_ids]):
        await coordinator._store.async_save(coordinator._state)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return ok
