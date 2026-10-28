"""Minimal client for the Watts On backend (Azure AD B2C + p.watts-energy.dk)."""
from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from .const import API_BASE, CLIENT_ID, SCOPE, TOKEN_URL


class WattsAuthError(Exception):
    """Refresh token rejected; the user has to log in again."""


class WattsApiError(Exception):
    """Any other failure talking to the backend."""


def jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying it (we only read our own user id)."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


class WattsApi:
    """Keeps an access token fresh from a rotating refresh token."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        refresh_token: str,
        on_new_refresh_token: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._session = session
        self._refresh_token = refresh_token
        self._on_new_refresh_token = on_new_refresh_token
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self.user_id: str | None = None

    async def _ensure_token(self) -> str:
        async with self._lock:
            if self._access_token and time.time() < self._expires_at - 120:
                return self._access_token
            try:
                async with self._session.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": CLIENT_ID,
                        "refresh_token": self._refresh_token,
                        "scope": SCOPE,
                    },
                ) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status in (400, 401):
                        raise WattsAuthError(body.get("error_description", body))
                    if resp.status >= 400:
                        raise WattsApiError(f"token endpoint {resp.status}: {body}")
            except aiohttp.ClientError as err:
                raise WattsApiError(str(err)) from err

            self._access_token = body["access_token"]
            self._expires_at = time.time() + int(body.get("expires_in", 3600))
            claims = jwt_claims(body.get("id_token") or self._access_token)
            self.user_id = claims.get("oid") or claims.get("sub") or self.user_id

            # B2C rotates refresh tokens; losing the new one ends the session.
            new_refresh = body.get("refresh_token")
            if new_refresh and new_refresh != self._refresh_token:
                self._refresh_token = new_refresh
                if self._on_new_refresh_token:
                    await self._on_new_refresh_token(new_refresh)
            return self._access_token

    async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        token = await self._ensure_token()
        try:
            async with self._session.get(
                API_BASE + path.lstrip("/"),
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status == 401:
                    self._access_token = None
                    raise WattsAuthError("API rejected access token")
                if resp.status == 204:
                    return []
                if resp.status >= 400:
                    raise WattsApiError(f"{path} -> {resp.status}")
                return await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise WattsApiError(str(err)) from err

    async def async_water_devices(self) -> list[str]:
        """Find water device ids across all of the user's locations."""
        await self._ensure_token()
        locations = _as_list(
            await self.get(f"installations/api/v3/users/{self.user_id}/locations"),
            "locations",
        )
        found: list[str] = []
        for loc in locations:
            loc_id = _first(loc, "id", "locationId")
            if not loc_id:
                continue
            detail = await self.get(f"provisioning/api/v1/locations/{loc_id}")
            for dev in _walk_dicts(detail):
                dev_id = _first(dev, "deviceId", "id")
                kind = str(_first(dev, "utilityType", "type", "deviceType") or "")
                if dev_id and "water" in kind.lower() and dev_id not in found:
                    found.append(str(dev_id))
        return found

    async def async_recent_consumption(self, device_id: str) -> list[dict[str, Any]]:
        return _as_list(
            await self.get(f"water/api/consumptions/{device_id}/recent"),
            "consumptions",
        )


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if data.get(key) is not None:
            return data[key]
    return None


def _as_list(data: Any, *wrapper_keys: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in (*wrapper_keys, "items", "data", "values", "list"):
            if isinstance(data.get(key), list):
                return [x for x in data[key] if isinstance(x, dict)]
    return []


def _walk_dicts(data: Any):
    if isinstance(data, dict):
        yield data
        for v in data.values():
            yield from _walk_dicts(v)
    elif isinstance(data, list):
        for v in data:
            yield from _walk_dicts(v)
