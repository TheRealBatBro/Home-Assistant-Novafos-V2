"""Config flow: paste a refresh token from tools/login.py."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WattsApi, WattsApiError, WattsAuthError
from .const import (
    CONF_DEVICE_IDS,
    CONF_REFRESH_TOKEN,
    CONF_UNIT,
    CONF_USER_ID,
    DOMAIN,
    UNIT_LITER,
    UNIT_M3,
)

SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REFRESH_TOKEN): str,
        vol.Optional(CONF_DEVICE_IDS, default=""): str,
        vol.Required(CONF_UNIT, default=UNIT_M3): vol.In([UNIT_M3, UNIT_LITER]),
    }
)


class NovafosWattsConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            api = WattsApi(async_get_clientsession(self.hass), user_input[CONF_REFRESH_TOKEN].strip())
            new_token = user_input[CONF_REFRESH_TOKEN].strip()

            async def keep(token: str) -> None:
                nonlocal new_token
                new_token = token

            api._on_new_refresh_token = keep
            try:
                manual = [d.strip() for d in user_input[CONF_DEVICE_IDS].split(",") if d.strip()]
                await api.async_user_id()
                devices = manual or await api.async_water_devices()
            except WattsAuthError:
                errors["base"] = "invalid_auth"
            except WattsApiError:
                errors["base"] = "cannot_connect"
            else:
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    await self.async_set_unique_id(api.user_id)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title="Novafos water",
                        data={
                            CONF_REFRESH_TOKEN: new_token,
                            CONF_USER_ID: api.user_id,
                            CONF_DEVICE_IDS: devices,
                            CONF_UNIT: user_input[CONF_UNIT],
                        },
                    )
        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)
