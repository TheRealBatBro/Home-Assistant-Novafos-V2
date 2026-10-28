"""Fixtures. All ids are fake; the API mock mirrors the live response shape."""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util

pytest_plugins = ["pytest_homeassistant_custom_component"]

DEVICE = "dev-0001"
HOURLY_VOLUME = 0.01  # m³ every hour
DATA_DAYS = 10
LAG = timedelta(hours=30)  # Novafos data is about a day behind


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    yield


def now_hour() -> datetime:
    return dt_util.utcnow().replace(minute=0, second=0, microsecond=0)


def fake_hours() -> list[datetime]:
    """Interval starts the API knows about, oldest first."""
    end = now_hour() - LAG
    first = end - timedelta(days=DATA_DAYS)
    return [first + timedelta(hours=i) for i in range(int((end - first) / timedelta(hours=1)))]


def item(start: datetime) -> dict:
    stamp = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"sd": stamp, "ed": end, "vol": HOURLY_VOLUME, "mq": "Measured"}


@pytest.fixture
def mock_api():
    async def consumptions(self, device_id, start):
        return [item(h) for h in fake_hours() if h >= start]

    with patch(
        "custom_components.novafos_watts.api.WattsApi.async_consumptions", consumptions
    ):
        yield
