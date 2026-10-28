"""Minimal client for the Watts On backend (Azure AD B2C + p.watts-energy.dk)."""
from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
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
            # B2C rotates refresh tokens; losing the new one ends the session.
            new_refresh = body.get("refresh_token")
            if new_refresh and new_refresh != self._refresh_token:
                self._refresh_token = new_refresh
                if self._on_new_refresh_token:
                    await self._on_new_refresh_token(new_refresh)
            return self._access_token

    @property
    def refresh_token(self) -> str:
        """The newest refresh token (it rotates on every refresh)."""
        return self._refresh_token

    async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        # A 401 can mean the access token was revoked before it expired, so drop it,
        # get a fresh one from the refresh token and retry once. Only a second 401,
        # or the token endpoint refusing the refresh token, counts as a login failure.
        for attempt in (1, 2):
            token = await self._ensure_token()
            try:
                async with self._session.get(
                    API_BASE + path.lstrip("/"),
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                ) as resp:
                    if resp.status == 401:
                        self._access_token = None
                        if attempt == 1:
                            continue
                        raise WattsAuthError("API rejected a freshly issued access token")
                    if resp.status == 204:
                        return []
                    if resp.status >= 400:
                        raise WattsApiError(f"{path} -> {resp.status}")
                    return await resp.json(content_type=None)
            except aiohttp.ClientError as err:
                raise WattsApiError(str(err)) from err
        raise WattsAuthError("unreachable")  # loop always returns or raises

    async def async_user_id(self) -> str:
        """Backend user id; it is not the token's sub/oid, so ask the API."""
        if not self.user_id:
            me = await self.get("users/api/v3/User")
            self.user_id = str(ci_first(me, "id", "userId") or "") if isinstance(me, dict) else ""
            if not self.user_id:
                raise WattsApiError("users/api/v3/User returned no id")
        return self.user_id

    async def async_water_devices(self) -> dict[str, str]:
        """Find water devices across all locations, as {device_id: unit}."""
        user_id = await self.async_user_id()
        locations = _as_list(
            await self.get(f"installations/api/v3/users/{user_id}/locations"),
            "locations",
        )
        found: dict[str, str] = {}
        for loc in locations:
            loc_id = ci_first(loc, "id", "locationId")
            if not loc_id:
                continue
            detail = await self.get(f"provisioning/api/v1/locations/{loc_id}")
            for dev in _walk_dicts(detail):
                dev_id = ci_first(dev, "deviceId", "id")
                kind = str(ci_first(dev, "utilityType", "type", "deviceType") or "")
                if dev_id and "water" in kind.lower():
                    found.setdefault(str(dev_id), str(ci_first(dev, "unit") or ""))
        return found

    async def async_consumptions(self, device_id: str, start: datetime) -> list[dict[str, Any]]:
        """Hourly intervals from `start` on (WaterConsumptionService, ?startDate=)."""
        stamp = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return _as_list(
            await self.get(f"water/api/consumptions/{device_id}", {"startDate": stamp}),
            "consumptions",
        )

    @property
    def expires_at(self) -> datetime | None:
        """When the current access token stops working."""
        return datetime.fromtimestamp(self._expires_at, timezone.utc) if self._access_token else None


def ci_first(data: dict[str, Any], *keys: str) -> Any:
    """First non-null value among keys, ignoring case (the API uses PascalCase)."""
    lowered = {k.lower(): v for k, v in data.items()}
    for key in keys:
        if lowered.get(key.lower()) is not None:
            return lowered[key.lower()]
    return None


def _as_list(data: Any, *wrapper_keys: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in (*wrapper_keys, "items", "data", "values", "list"):
            found = ci_first(data, key)
            if isinstance(found, list):
                return [x for x in found if isinstance(x, dict)]
    return []


def _walk_dicts(data: Any):
    if isinstance(data, dict):
        yield data
        for v in data.values():
            yield from _walk_dicts(v)
    elif isinstance(data, list):
        for v in data:
            yield from _walk_dicts(v)
