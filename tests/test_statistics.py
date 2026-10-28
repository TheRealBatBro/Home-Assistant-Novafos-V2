"""Statistics inside Home Assistant: the external series and the sensor's own."""
from datetime import datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.novafos_watts.const import DOMAIN

from .conftest import DEVICE, HOURLY_VOLUME, fake_hours, now_hour

STAT_ID = "novafos_watts:water_consumption"
SHARED_ID = "novafos_water:water_consumption"  # written by the older, separate integration
ENTITY = "sensor.novafos_water_consumption"
H = timedelta(hours=1)
M5 = timedelta(minutes=5)
EPS = 1e-6
LEGACY_ISSUE = (DOMAIN, "legacy_statistic_id")


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="user-0001",
        data={"refresh_token": "fake", "user_id": "user-0001", "device_ids": [DEVICE], "unit": "m3"},
    )


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _true_sums() -> list[tuple[datetime, float]]:
    return [(h, HOURLY_VOLUME * (i + 1)) for i, h in enumerate(fake_hours())]


def _meta(unit="m³") -> StatisticMetaData:
    return StatisticMetaData(
        has_sum=True, name=None, source="recorder", statistic_id=ENTITY,
        unit_of_measurement=unit, unit_class="volume", mean_type=StatisticMeanType.NONE,
    )


async def _rows(hass, stat_id, period="hour"):
    await async_wait_recording_done(hass)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, now_hour() - timedelta(days=60), None,
        {stat_id}, period, None, {"sum", "state"},
    )
    return stats.get(stat_id, [])


def _changes(rows):
    return [b["sum"] - a["sum"] for a, b in zip(rows, rows[1:])]


def _store_key(entry) -> str:
    return f"{DOMAIN}_history_{entry.entry_id}"


def _stored(hass_storage, entry, **extra):
    """Coordinator state as an earlier version saved it."""
    hours = fake_hours()
    hass_storage[_store_key(entry)] = {
        "version": 1, "minor_version": 1, "key": _store_key(entry),
        "data": {DEVICE: {
            "hourly": {_iso(h): HOURLY_VOLUME for h in hours},
            "imported": _iso(hours[-1]),
            **extra,
        }},
    }


def _register_sensor(hass, entry, unit=None):
    reg = er.async_get(hass)
    reg.async_get_or_create(
        "sensor", DOMAIN, f"{DEVICE}_consumption",
        suggested_object_id="novafos_water_consumption", config_entry=entry,
    )
    if unit:  # the user picked another display unit in the entity settings
        reg.async_update_entity_options(ENTITY, "sensor", {"unit_of_measurement": unit})


async def _setup(hass, entry):
    if entry.entry_id not in {e.entry_id for e in hass.config_entries.async_entries(DOMAIN)}:
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_fresh_install_leaves_sensor_statistics_to_the_recorder(hass, mock_api):
    await _setup(hass, _entry())

    total = HOURLY_VOLUME * len(fake_hours())
    state = hass.states.get(ENTITY)
    assert abs(float(state.state) - total) < EPS
    assert state.attributes["state_class"] == "total_increasing"

    external = await _rows(hass, STAT_ID)
    assert len(external) == len(fake_hours())
    assert abs(external[-1]["sum"] - total) < EPS
    assert await _rows(hass, SHARED_ID) == []
    assert await _rows(hass, ENTITY) == []  # nothing imported into the sensor


async def test_new_statistic_id_gets_the_whole_series(hass, hass_storage, mock_api):
    """0.4.1 imported into the shared id; the new id must get everything, not the overlap."""
    entry = _entry()
    _stored(hass_storage, entry)
    await _setup(hass, entry)

    external = await _rows(hass, STAT_ID)
    assert [r["start"] for r in external] == [h.timestamp() for h in fake_hours()]
    assert await _rows(hass, SHARED_ID) == []


async def _write_corrupted_history(hass, unit="m³", scale=1.0):
    """What 0.3.0-0.4.1 left: the recorder's series from 0, partly overwritten by imports."""
    true = [(h, v * scale) for h, v in _true_sums()]

    def state(t):  # the sensor showed everything up to a day before t
        return max([v for h, v in true if h + H <= t - timedelta(hours=24)], default=0.0)

    end = now_hour()
    frame = end - timedelta(hours=40)
    base = state(frame)
    short = [frame + i * M5 for i in range(int((end - frame) / M5))]
    get_instance(hass).async_import_statistics(
        _meta(unit),
        [StatisticData(start=t, state=state(t + M5), sum=state(t + M5) - base) for t in short],
        StatisticsShortTerm,
    )
    async_import_statistics(hass, _meta(unit), [
        StatisticData(start=h, state=state(h + H), sum=state(h + H) - base)
        for h in (frame + i * H for i in range(40))
    ])
    async_import_statistics(hass, _meta(unit), [StatisticData(start=h, state=v, sum=v) for h, v in true])
    assert min(_changes(await _rows(hass, ENTITY))) < 0  # the reported bug


@pytest.mark.parametrize(("unit", "scale"), [("m³", 1.0), ("L", 1000.0)])
async def test_repair_makes_the_sensor_series_continuous(hass, hass_storage, mock_api, unit, scale):
    entry = _entry()
    _stored(hass_storage, entry, statistic_id=STAT_ID, entity_imported=_iso(fake_hours()[-1]))
    entry.add_to_hass(hass)
    _register_sensor(hass, entry, None if unit == "m³" else unit)
    await _write_corrupted_history(hass, unit, scale)
    await _setup(hass, entry)
    assert hass.states.get(ENTITY).attributes["unit_of_measurement"] == unit

    hourly = await _rows(hass, ENTITY)
    assert min(_changes(hourly)) >= -EPS
    assert abs(hourly[-1]["sum"] - hourly[-1]["state"]) < EPS
    short = await _rows(hass, ENTITY, "5minute")
    assert abs(short[-1]["sum"] - short[-1]["state"]) < EPS
    assert "entity_imported" not in hass_storage[_store_key(entry)]["data"][DEVICE]

    # The recorder carries on from the repaired series: no drop at the next hour.
    do_adhoc_statistics(hass, start=now_hour())
    do_adhoc_statistics(hass, start=now_hour() + timedelta(minutes=55))
    await hass.async_block_till_done()
    hourly = await _rows(hass, ENTITY)
    assert hourly[-1]["start"] == now_hour().timestamp()
    assert min(_changes(hourly)) >= -EPS
    assert abs(hourly[-1]["sum"] - float(hass.states.get(ENTITY).state)) < EPS

    # Later polls leave the sensor's statistics alone.
    await hass.data[DOMAIN][entry.entry_id].async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await _rows(hass, ENTITY) == hourly


async def test_repair_without_recorder_series_clears_imported_rows(hass, hass_storage, mock_api):
    entry = _entry()
    _stored(hass_storage, entry, statistic_id=STAT_ID, entity_imported=_iso(fake_hours()[-1]))
    entry.add_to_hass(hass)
    _register_sensor(hass, entry)
    async_import_statistics(hass, _meta(), [StatisticData(start=h, state=v, sum=v) for h, v in _true_sums()])
    assert await _rows(hass, ENTITY)

    await _setup(hass, entry)
    assert await _rows(hass, ENTITY) == []
    assert await _rows(hass, STAT_ID)  # the external statistic is not touched


async def test_repair_runs_once(hass, hass_storage, mock_api):
    entry = _entry()
    _stored(hass_storage, entry, statistic_id=STAT_ID)  # no entity_imported: never backfilled
    entry.add_to_hass(hass)
    _register_sensor(hass, entry)
    rows = [StatisticData(start=h, state=v, sum=v) for h, v in _true_sums()]
    async_import_statistics(hass, _meta(), rows)
    before = await _rows(hass, ENTITY)

    await _setup(hass, entry)
    assert await _rows(hass, ENTITY) == before


async def _energy_uses(hass, stat_id):
    from homeassistant.components.energy.data import async_get_manager

    assert await async_setup_component(hass, "energy", {})
    manager = await async_get_manager(hass)
    await manager.async_update({"energy_sources": [{
        "type": "water", "stat_energy_from": stat_id, "stat_cost": None,
        "entity_energy_price": None, "number_energy_price": None,
    }]})


async def test_energy_dashboard_on_the_old_id_raises_an_issue(hass, mock_api):
    await _energy_uses(hass, SHARED_ID)
    await _setup(hass, _entry())
    issue = ir.async_get(hass).async_get_issue(*LEGACY_ISSUE)
    assert issue and issue.translation_placeholders["new"] == STAT_ID


async def test_no_issue_on_the_new_id(hass, mock_api):
    await _energy_uses(hass, STAT_ID)
    await _setup(hass, _entry())
    assert ir.async_get(hass).async_get_issue(*LEGACY_ISSUE) is None


async def test_no_issue_when_the_older_integration_owns_the_old_id(hass, mock_api):
    MockConfigEntry(domain="novafos_water").add_to_hass(hass)
    await _energy_uses(hass, SHARED_ID)
    await _setup(hass, _entry())
    assert ir.async_get(hass).async_get_issue(*LEGACY_ISSUE) is None
