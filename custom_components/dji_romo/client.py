"""HTTP client for DJI Home cloud endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import Any
from uuid import uuid4

from aiohttp import ClientError, ClientResponseError, ClientSession

from .const import DEFAULT_API_URL, DEFAULT_LOCALE

_LOGGER = logging.getLogger(__name__)


class DjiRomoApiError(Exception):
    """Raised when the DJI Home API responds with an error."""


class DjiRomoAuthError(DjiRomoApiError):
    """Raised when the DJI Home user token is invalid or expired."""


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
        device_sn: str | None = None,
        api_url: str = DEFAULT_API_URL,
        locale: str = DEFAULT_LOCALE,
    ) -> None:
        self._session = session
        self._user_token = user_token
        self._device_sn = device_sn
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

    async def async_get_active_job(self) -> dict[str, Any] | None:
        """Fetch the current or most recent cleaning job."""
        payload = await self._device_request("GET", "jobs/cleans/job/list")
        jobs = payload.get("data", {}).get("job_list", [])
        for job in jobs:
            if job.get("status") in {"in_progress", "paused"}:
                return job
        return None

    async def async_get_shortcuts(self) -> list[dict[str, Any]]:
        """Fetch app cleaning shortcuts, including room and map metadata."""
        payload = await self._device_request(
            "GET",
            "shortcuts/list",
            params={"plan_data_version": 0, "slot_id": 0},
        )
        return payload.get("data", {}).get("plan_list", [])

    async def async_get_properties(self) -> dict[str, Any]:
        """Fetch device and dock properties."""
        payload = await self._device_request("GET", "things/properties")
        return payload.get("data", {})

    async def async_get_settings(self) -> dict[str, Any]:
        """Fetch device settings."""
        payload = await self._device_request("GET", "settings")
        return payload.get("data", {})

    async def async_get_consumables(self) -> list[dict[str, Any]]:
        """Fetch robot consumable status."""
        payload = await self._device_request("GET", "consumables")
        return payload.get("data", {}).get("list", [])

    async def async_get_dock_consumables(self) -> dict[str, Any]:
        """Fetch dock consumable and tank status."""
        payload = await self._device_request("GET", "consumables/dock")
        return payload.get("data", {})

    async def async_get_consumable_notifications(self) -> list[dict[str, Any]]:
        """Fetch consumable notifications."""
        alerts: list[dict[str, Any]] = []
        for notify_type in (0, 1):
            payload = await self._device_request(
                "GET",
                "consumables/notifications",
                params={"notify_type": notify_type},
            )
            alerts.extend(payload.get("data", {}).get("list", []))
        return alerts

    async def async_start_clean(self) -> None:
        """Start a full room-cleaning job using the first DJI Home shortcut."""
        shortcuts = await self.async_get_shortcuts()
        if not shortcuts:
            raise DjiRomoApiError("No DJI Home cleaning shortcuts were returned.")

        await self.async_start_shortcut(shortcuts[0])

    async def async_start_shortcut(self, shortcut: dict[str, Any]) -> None:
        """Start a cleaning job from a DJI Home shortcut."""
        plan_configs = shortcut.get("plan_area_configs", [])
        room_map = shortcut.get("room_map", {})
        if not plan_configs:
            raise DjiRomoApiError("The DJI Home cleaning shortcut has no room config.")

        area_configs = []
        for config in plan_configs:
            area_configs.append(
                {
                    "config_uuid": str(uuid4()),
                    "clean_mode": config.get("clean_mode", 0),
                    "fan_speed": config.get("fan_speed", 2),
                    "water_level": config.get("water_level", 2),
                    "clean_num": config.get("clean_num", 1),
                    "storm_mode": config.get("storm_mode", 0),
                    "secondary_clean_num": config.get("secondary_clean_num", 1),
                    "clean_speed": config.get("clean_speed", 2),
                    "order_id": config.get("order_id", 1),
                    "poly_type": config.get("poly_type", 2),
                    "poly_index": config.get("poly_index", 0),
                    "poly_label": config.get("poly_label", 0),
                    "user_label": config.get("user_label", 0),
                    "poly_name_index": config.get("poly_name_index", 0),
                    "skip_area": 0,
                    "floor_cleaner_type": config.get("floor_cleaner_type", 0),
                    "repeat_mop": config.get("repeat_mop", False),
                }
            )

        body = {
            "sn": self._device_sn,
            "job_timeout": 3600,
            "method": "room_clean",
            "data": {
                "action": "start",
                "name": shortcut.get("plan_name", ""),
                "plan_name_key": shortcut.get("plan_name_key", ""),
                "plan_uuid": shortcut.get("plan_uuid") or str(uuid4()),
                "plan_type": shortcut.get("plan_type", 2),
                "clean_area_type": shortcut.get("clean_area_type", 2),
                "is_valid": True,
                "plan_area_configs": area_configs,
                "room_map": {
                    "map_index": room_map.get("map_index", 0),
                    "map_version": room_map.get("map_version", 0),
                    "file_id": room_map.get("file_id", ""),
                    "slot_id": room_map.get("slot_id", 0),
                },
                "area_config_type": shortcut.get("area_config_type", 0),
            },
        }
        await self._device_request("POST", "jobs/cleans/start", json=body)

    async def async_start_room(
        self,
        room_config: dict[str, Any],
        room_map: dict[str, Any],
        name: str,
    ) -> None:
        """Start a cleaning job for a single room."""
        area_config = {
            "config_uuid": str(uuid4()),
            "clean_mode": room_config.get("clean_mode", 2),
            "fan_speed": room_config.get("fan_speed", 2),
            "water_level": room_config.get("water_level", 2),
            "clean_num": room_config.get("clean_num", 1),
            "storm_mode": room_config.get("storm_mode", 0),
            "secondary_clean_num": room_config.get("secondary_clean_num", 1),
            "clean_speed": room_config.get("clean_speed", 2),
            "order_id": 1,
            "poly_type": room_config.get("poly_type", 2),
            "poly_index": room_config.get("poly_index", 0),
            "poly_label": room_config.get("poly_label", 0),
            "user_label": room_config.get("user_label", 0),
            "poly_name_index": room_config.get("poly_name_index", 0),
            "skip_area": 0,
            "floor_cleaner_type": room_config.get("floor_cleaner_type", 0),
            "repeat_mop": room_config.get("repeat_mop", False),
        }
        body = {
            "sn": self._device_sn,
            "job_timeout": 3600,
            "method": "room_clean",
            "data": {
                "action": "start",
                "name": name,
                "plan_name_key": "",
                "plan_uuid": str(uuid4()),
                "plan_type": 2,
                "clean_area_type": 2,
                "is_valid": True,
                "plan_area_configs": [area_config],
                "room_map": {
                    "map_index": room_map.get("map_index", 0),
                    "map_version": room_map.get("map_version", 0),
                    "file_id": room_map.get("file_id", ""),
                    "slot_id": room_map.get("slot_id", 0),
                },
                "area_config_type": 0,
            },
        }
        await self._device_request("POST", "jobs/cleans/start", json=body)

    async def async_return_to_base(self) -> None:
        """Send the robot back to its dock."""
        await self._device_request(
            "POST",
            "jobs/goHomes/start",
            json={},
            allowed_result_codes={0, 129128},
        )

    async def async_wash_mop_pads(self) -> None:
        """Start mop pad cleaning at the dock."""
        await self._device_request("POST", "jobs/brushCleans/startWithMode", json={})

    async def async_dust_collect(self) -> None:
        """Start manual dust collection at the dock."""
        await self._device_request("POST", "jobs/dustCollects/start", json={})

    async def async_start_drying(self) -> None:
        """Start mop pad drying at the dock."""
        await self._device_request("POST", "jobs/drying/start", json={})

    async def async_pause_cleaning(self, job_uuid: str | None = None) -> None:
        """Pause the active cleaning job."""
        if job_uuid is None:
            job = await self.async_get_active_job()
            job_uuid = job["uuid"] if job else None
        if job_uuid is None:
            raise DjiRomoApiError("No active DJI Romo cleaning job to pause.")
        await self._device_request("POST", f"jobs/cleans/{job_uuid}/pause", json={})

    async def async_resume_cleaning(self, job_uuid: str | None = None) -> None:
        """Resume the active paused cleaning job."""
        if job_uuid is None:
            job = await self.async_get_active_job()
            job_uuid = job["uuid"] if job else None
        if job_uuid is None:
            raise DjiRomoApiError("No active DJI Romo cleaning job to resume.")
        await self._device_request("POST", f"jobs/cleans/{job_uuid}/resume", json={})

    async def async_stop_cleaning(self, job_uuid: str | None = None) -> None:
        """Stop the active cleaning job."""
        if job_uuid is None:
            job = await self.async_get_active_job()
            job_uuid = job["uuid"] if job else None
        if job_uuid is None:
            raise DjiRomoApiError("No active DJI Romo cleaning job to stop.")
        await self._device_request("POST", f"jobs/cleans/{job_uuid}/stop", json={})

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

    async def _device_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        allowed_result_codes: set[int] | None = None,
    ) -> dict[str, Any]:
        """Perform a request against the Romo device API."""
        if self._device_sn is None:
            raise DjiRomoApiError("No DJI Romo device serial is configured.")
        url = f"{self._api_url}/cr/app/api/v1/devices/{self._device_sn}/{path}"
        headers = self._headers(include_json=method != "GET")

        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json,
                raise_for_status=True,
            ) as response:
                payload: dict[str, Any] = await response.json()
        except ClientResponseError as err:
            if err.status == 401:
                raise DjiRomoAuthError("The DJI Home user token is invalid or expired.") from err
            raise DjiRomoApiError(
                f"Failed to call DJI Romo device API: {err.status} {err.message}"
            ) from err
        except ClientError as err:
            raise DjiRomoApiError(f"Failed to call DJI Romo device API: {err}") from err

        result = payload.get("result", {})
        result_code = result.get("code")
        allowed = allowed_result_codes or {0}
        if result_code not in allowed:
            message = result.get("message") or "Unknown DJI Romo device API error"
            raise DjiRomoApiError(message)

        _LOGGER.debug("DJI Romo device API response for %s %s: %s", method, path, payload)
        return payload

    async def _request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Perform a GET request against the DJI Home API."""
        url = f"{self._api_url}{path}"
        headers = self._headers()

        try:
            async with self._session.get(
                url,
                headers=headers,
                params=params,
                raise_for_status=True,
            ) as response:
                payload: dict[str, Any] = await response.json()
        except ClientError as err:
            if isinstance(err, ClientResponseError) and err.status == 401:
                raise DjiRomoAuthError("The DJI Home user token is invalid or expired.") from err
            raise DjiRomoApiError(f"Failed to call DJI Home API: {err}") from err

        result = payload.get("result", {})
        if result.get("code") != 0:
            message = result.get("message") or "Unknown DJI Home API error"
            if "token" in message.lower() or "auth" in message.lower():
                raise DjiRomoAuthError(message)
            raise DjiRomoApiError(message)

        _LOGGER.debug("DJI Home API response for %s: %s", path, payload)
        return payload

    def _headers(self, *, include_json: bool = False) -> dict[str, str]:
        """Return DJI Home app-like request headers."""
        headers = {
            "x-member-token": self._user_token,
            "X-DJI-locale": self._locale,
            "version-name": "1.5.15",
            "User-Agent": "DJI-Home/1.5.15",
            "x-request-start": str(int(datetime.now(UTC).timestamp() * 1000)),
        }
        if include_json:
            headers["Content-Type"] = "application/json"
        return headers
