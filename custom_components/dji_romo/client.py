"""HTTP client for DJI Home cloud endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import Any

from aiohttp import ClientError, ClientSession

from .const import DEFAULT_API_URL, DEFAULT_LOCALE

_LOGGER = logging.getLogger(__name__)


class DjiRomoApiError(Exception):
    """Raised when the DJI Home API responds with an error."""


@dataclass(slots=True)
class DjiMqttCredentials:
    """MQTT credentials returned by DJI Home cloud."""

    domain: str
    port: int
    client_id: str
    username: str
    password: str
    fetched_at: datetime


class DjiRomoApiClient:
    """Small wrapper around the DJI Home cloud API."""

    def __init__(
        self,
        session: ClientSession,
        user_token: str,
        *,
        api_url: str = DEFAULT_API_URL,
        locale: str = DEFAULT_LOCALE,
    ) -> None:
        self._session = session
        self._user_token = user_token
        self._api_url = api_url.rstrip("/")
        self._locale = locale

    async def async_get_mqtt_credentials(self) -> DjiMqttCredentials:
        """Fetch temporary MQTT credentials."""
        payload = await self._request(
            "/app/api/v1/users/auth/token",
            params={"reason": "mqtt"},
        )
        data = payload["data"]
        return DjiMqttCredentials(
            domain=data["mqtt_domain"],
            port=int(data["mqtt_port"]),
            client_id=data["client_id"],
            username=data["user_uuid"],
            password=data["user_token"],
            fetched_at=datetime.now(UTC),
        )

    async def async_get_homes(self) -> list[dict[str, Any]]:
        """Fetch homes and attached devices for the logged-in user."""
        payload = await self._request("/app/api/v1/homes")
        return payload.get("data", {}).get("homes", [])

    async def async_resolve_device(
        self, device_sn: str | None = None
    ) -> dict[str, Any]:
        """Find a device from the homes response."""
        homes = await self.async_get_homes()
        devices: list[dict[str, Any]] = []
        for home in homes:
            for device in home.get("devices", []):
                normalized_sn = device.get("sn") or device.get("device_sn")
                if normalized_sn:
                    device = dict(device)
                    device["sn"] = normalized_sn
                    device["home_id"] = home.get("id") or home.get("home_id")
                    device["home_name"] = home.get("name")
                    devices.append(device)

        if not devices:
            raise DjiRomoApiError("No DJI Home devices were returned for this account.")

        if device_sn is None:
            return devices[0]

        for device in devices:
            if device["sn"] == device_sn:
                return device

        raise DjiRomoApiError(
            f"Device serial '{device_sn}' was not found in the DJI Home account."
        )

    async def _request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Perform a GET request against the DJI Home API."""
        url = f"{self._api_url}{path}"
        headers = {
            "x-member-token": self._user_token,
            "X-DJI-locale": self._locale,
        }

        try:
            async with self._session.get(
                url,
                headers=headers,
                params=params,
                raise_for_status=True,
            ) as response:
                payload: dict[str, Any] = await response.json()
        except ClientError as err:
            raise DjiRomoApiError(f"Failed to call DJI Home API: {err}") from err

        result = payload.get("result", {})
        if result.get("code") != 0:
            message = result.get("message") or "Unknown DJI Home API error"
            raise DjiRomoApiError(message)

        _LOGGER.debug("DJI Home API response for %s: %s", path, payload)
        return payload
