"""Config flow: paste a refresh token from tools/login.py."""
from __future__ import annotations

from collections.abc import Mapping
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
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_REFRESH_TOKEN): str})


class NovafosWattsConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            api = WattsApi(async_get_clientsession(self.hass), user_input[CONF_REFRESH_TOKEN].strip())
            try:
                manual = [d.strip() for d in user_input[CONF_DEVICE_IDS].split(",") if d.strip()]
                await api.async_user_id()
                devices = manual or list(await api.async_water_devices())
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
                            # The token rotates on use, so keep the newest one.
                            CONF_REFRESH_TOKEN: api.refresh_token,
                            CONF_USER_ID: api.user_id,
                            CONF_DEVICE_IDS: devices,
                            CONF_UNIT: user_input[CONF_UNIT],
                        },
                    )
        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Started by Home Assistant when the saved login stops working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            api = WattsApi(async_get_clientsession(self.hass), user_input[CONF_REFRESH_TOKEN].strip())
            try:
                user_id = await api.async_user_id()
            except WattsAuthError:
                errors["base"] = "invalid_auth"
            except WattsApiError:
                errors["base"] = "cannot_connect"
            else:
                if entry.unique_id and user_id != entry.unique_id:
                    errors["base"] = "wrong_account"
                else:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_REFRESH_TOKEN: api.refresh_token}
                    )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=REAUTH_SCHEMA, errors=errors
        )
