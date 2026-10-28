"""Novafos water data from the Watts On backend."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_import_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

from .api import WattsApi, WattsApiError, WattsAuthError, ci_first
from .calc import Summary, parse_utc, rebase_entity_statistics, summarize
from .const import (
    CONF_DEVICE_IDS,
    CONF_REFRESH_TOKEN,
    CONF_UNIT,
    DOMAIN,
    LEGACY_STATISTICS_SOURCE,
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
        statistic_id = self.statistic_id(device_id)
        if dev.get("statistic_id") != statistic_id:
            # New id (0.5.0 moved off novafos_water:, which the older integration
            # owns): import the whole series, not just the overlap.
            dev["imported"] = None
            dev["statistic_id"] = statistic_id
        metadata = StatisticMetaData(
            has_sum=True,
            name="Novafos water consumption (Watts On)",
            source=STATISTICS_SOURCE,
            statistic_id=statistic_id,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
            unit_class="volume",
            **_MEAN,
        )
        async_add_external_statistics(self.hass, metadata, self._rows(summary, dev["imported"]))
        dev["imported"] = _iso(summary.sums[-1][0])

    async def async_repair_entity_statistics(self, device_id: str) -> bool:
        """Once, undo the 0.3.0-0.4.1 backfill of the consumption sensor's statistics.

        The recorder owns those statistics now (the sensor is total_increasing).
        Returns True when the stored state changed and needs saving.
        """
        assert self._state is not None
        dev = self._state.get(device_id, {})
        summary = (self.data or {}).get(device_id)
        if "entity_imported" not in dev or summary is None:
            return False
        entity_id = er.async_get(self.hass).async_get_entity_id(
            "sensor", DOMAIN, f"{device_id}_consumption"
        )
        if entity_id:
            await self._rebase_entity(entity_id, summary)
        del dev["entity_imported"]
        return True

    async def _rebase_entity(self, entity_id: str, summary: Summary) -> None:
        recorder = get_instance(self.hass)
        meta = await recorder.async_add_executor_job(
            lambda: get_metadata(self.hass, statistic_ids={entity_id})
        )
        if entity_id not in meta:
            return
        # The user may have changed the sensor's unit (say to litres); the
        # statistics are then stored in that unit. Work in m³ and write back in it.
        unit = meta[entity_id][1]["unit_of_measurement"] or UnitOfVolume.CUBIC_METERS

        async def rows(period: str) -> list[dict[str, Any]]:
            stats = await recorder.async_add_executor_job(
                statistics_during_period,
                self.hass,
                datetime(2000, 1, 1, tzinfo=dt_util.UTC),
                None,
                {entity_id},
                period,
                {"volume": UnitOfVolume.CUBIC_METERS},
                {"state", "sum"},
            )
            return [r for r in stats.get(entity_id, []) if r.get("sum") is not None]

        hours = await rows("hour")
        if not hours:
            return
        short = await rows("5minute")
        plan = rebase_entity_statistics(
            summary.sums,
            [dt_util.utc_from_timestamp(r["start"]) for r in hours],
            [
                (dt_util.utc_from_timestamp(r["start"]), float(r["state"]), float(r["sum"]))
                for r in short
                if r.get("state") is not None
            ],
        )
        if plan is None:
            # Only imported rows and no recorder series to join them to: the
            # recorder's first compile would start at 0 below them, so drop them.
            recorder.async_clear_statistics([entity_id])
            _LOGGER.info("Cleared imported statistics of %s", entity_id)
            return
        # Queued in this order: the adjustment also shifts the hourly rows, which
        # the import then overwrites with their final sums.
        if abs(plan.offset) > 1e-9:
            recorder.async_adjust_statistics(
                entity_id, plan.frame_start, plan.offset, UnitOfVolume.CUBIC_METERS
            )
        metadata = StatisticMetaData(
            has_sum=True,
            name=None,
            source="recorder",
            statistic_id=entity_id,
            unit_of_measurement=unit,
            unit_class="volume",
            **_MEAN,
        )
        convert = VolumeConverter.converter_factory(UnitOfVolume.CUBIC_METERS, unit)
        async_import_statistics(
            self.hass,
            metadata,
            [
                StatisticData(start=start, state=convert(total), sum=convert(total))
                for start, total in plan.rows
            ],
        )
        _LOGGER.info(
            "Repaired statistics of %s: %s hourly rows, recorder series from %s moved by %s",
            entity_id,
            len(plan.rows),
            plan.frame_start,
            plan.offset,
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = WattsCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Entities exist now, so the sensor's statistic_id can be looked up.
    if any([await coordinator.async_repair_entity_statistics(d) for d in coordinator.device_ids]):
        await coordinator._store.async_save(coordinator._state)
    await _async_check_energy_sources(hass)
    return True


def _mentions(data: Any, prefix: str) -> bool:
    if isinstance(data, str):
        return data.startswith(prefix)
    if isinstance(data, dict):
        return any(_mentions(v, prefix) for v in data.values())
    if isinstance(data, list):
        return any(_mentions(v, prefix) for v in data)
    return False


async def _async_check_energy_sources(hass: HomeAssistant) -> None:
    """Raise a repair issue if the Energy dashboard uses the pre-0.5.0 statistic id.

    Before 0.5.0 this integration wrote novafos_water:water_consumption, which
    stopped updating. If the older Novafos integration (domain novafos_water) is
    set up, that id is its own and still live, so nothing is flagged.
    """
    legacy = f"{LEGACY_STATISTICS_SOURCE}:water_consumption"
    stale = False
    if (
        "energy" in hass.config.components
        and not hass.config_entries.async_entries(LEGACY_STATISTICS_SOURCE)
    ):
        from homeassistant.components.energy.data import async_get_manager

        stale = _mentions((await async_get_manager(hass)).data, legacy)
    if not stale:
        ir.async_delete_issue(hass, DOMAIN, "legacy_statistic_id")
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        "legacy_statistic_id",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="legacy_statistic_id",
        translation_placeholders={"old": legacy, "new": f"{STATISTICS_SOURCE}:water_consumption"},
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return ok
