"""HTTP client for DJI Home cloud endpoints."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import logging
import struct
from typing import Any
from uuid import uuid4

from aiohttp import ClientError, ClientResponseError, ClientSession
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .const import (
    DEFAULT_API_URL,
    DEFAULT_LOCALE,
    DEFAULT_START_PLAN_KEYS,
    TERMINAL_JOB_STATUSES,
)

_LOGGER = logging.getLogger(__name__)

# Endpoints whose responses carry secrets — the MQTT password (users/auth/token)
# and the map decryption key (safety/info). Their payloads are never logged, even
# at debug level: users routinely paste debug logs into GitHub issues.
_SENSITIVE_PATH_PARTS = ("users/auth/token", "safety/info")


def _loggable_payload(path: str, payload: dict[str, Any]) -> Any:
    """Return the payload for debug logging, redacted for sensitive endpoints."""
    if any(part in path for part in _SENSITIVE_PATH_PARTS):
        return "<redacted>"
    return payload


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
    expires_at: datetime | None = None


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
        # The account's map decryption key (safety/info) is stable, so cache it
        # instead of refetching it for every map/report download. Cleared if a
        # decrypt fails, in case the key ever rotates.
        self._share_key: bytes | None = None

    async def async_get_mqtt_credentials(self) -> DjiMqttCredentials:
        """Fetch temporary MQTT credentials."""
        payload = await self._request(
            "/app/api/v1/users/auth/token",
            params={"reason": "mqtt"},
        )
        data = payload["data"]
        fetched_at = datetime.now(UTC)
        expires_at: datetime | None = None
        expire_seconds = _coerce_result_code(data.get("expire"))
        if expire_seconds and expire_seconds > 0:
            expires_at = fetched_at + timedelta(seconds=expire_seconds)
        return DjiMqttCredentials(
            domain=data["mqtt_domain"],
            port=int(data["mqtt_port"]),
            client_id=data["client_id"],
            username=data["user_uuid"],
            password=data["user_token"],
            fetched_at=fetched_at,
            expires_at=expires_at,
        )

    async def _async_get_share_key(self) -> bytes | None:
        """Return the device map-decryption key (AES-256-GCM) as raw bytes.

        The key is the ``share_encryption_key`` (32-byte hex) from ``safety/info``;
        every encrypted map blob this account serves is decryptable with it. It is
        stable per account, so it is fetched once and cached.
        """
        if self._share_key is not None:
            return self._share_key
        try:
            safety_info = await self._device_request("GET", "safety/info")
        except DjiRomoApiError:
            return None
        share_key_hex: str = safety_info.get("data", {}).get("share_encryption_key", "")
        if not share_key_hex or len(share_key_hex) != 64:
            return None
        try:
            self._share_key = bytes.fromhex(share_key_hex)
        except ValueError:
            return None
        return self._share_key

    async def _async_download_and_decrypt_map(
        self,
        file_url: str,
        file_header: dict[str, str],
        share_key: bytes,
    ) -> dict[str, Any] | None:
        """Download an S3 map blob and AES-256-GCM decrypt it to JSON.

        The blob is ``nonce(16) || ciphertext || tag``; the SSE-C ``file_header``
        is sent as request headers so S3 returns the (still app-encrypted) bytes.
        """
        if not file_url:
            return None
        try:
            async with self._session.get(
                file_url,
                headers=dict(file_header),
                raise_for_status=True,
            ) as resp:
                encrypted_bytes = await resp.read()
        except ClientError:
            return None

        try:
            aesgcm = AESGCM(share_key)
            loop = asyncio.get_running_loop()
            plaintext = await loop.run_in_executor(
                None,
                aesgcm.decrypt,
                encrypted_bytes[:16],
                encrypted_bytes[16:],
                None,
            )
            return json.loads(plaintext)
        except Exception:  # noqa: BLE001
            # Drop the cached key so a rotated key is refetched on the next try.
            self._share_key = None
            return None

    async def _async_fetch_current_map(self) -> dict[str, Any] | None:
        """Download and decrypt the current SLAM map file, returning its JSON.

        The map is an AES-256-GCM blob (nonce = first 16 bytes, tag appended)
        encrypted with the device share key from safety/info.  It contains the
        floor plan (seg_map) and the cleaning-coverage grid (grid_map), and is
        refreshed server-side during a session — so a fresh client (or HA after a
        restart) can re-fetch the full current coverage without local state.
        """
        share_key = await self._async_get_share_key()
        if share_key is None:
            return None

        try:
            maps_payload = await self._device_request("GET", "maps/list")
            map_list: list[dict[str, Any]] = maps_payload.get("data", {}).get("map_list", [])
            current_map = next((m for m in map_list if m.get("is_current")), None)
            if not current_map:
                current_map = map_list[0] if map_list else None
            if not current_map:
                return None
        except DjiRomoApiError:
            return None

        return await self._async_download_and_decrypt_map(
            current_map.get("file_url", ""),
            current_map.get("file_header", {}),
            share_key,
        )

    async def async_get_map_data(self) -> dict[str, Any] | None:
        """Fetch and decrypt the full map data.

        Returns the full decrypted map dictionary.
        """
        return await self._async_fetch_current_map()

    async def async_get_current_map_meta(self) -> dict[str, Any] | None:
        """Return the current map's identity/revision without downloading the blob.

        Reads only the lightweight ``maps/list`` JSON and returns
        ``{"map_index": ..., "map_version": ...}`` for the current map.
        ``map_index`` is stable per physical map (changes on a map reset);
        ``map_version`` bumps on every edit (e.g. deleting a no-go zone). Used to
        decide when the cached overlays are stale and to detect a map reset.
        """
        try:
            maps_payload = await self._device_request("GET", "maps/list")
        except DjiRomoApiError:
            return None
        map_list: list[dict[str, Any]] = maps_payload.get("data", {}).get("map_list", [])
        current_map = next((m for m in map_list if m.get("is_current")), None)
        if not current_map:
            current_map = map_list[0] if map_list else None
        if not current_map:
            return None
        return {
            "map_index": current_map.get("map_index"),
            "map_version": current_map.get("map_version"),
        }

    async def async_get_job_room_map(self, job_uuid: str) -> dict[str, Any] | None:
        """Fetch and decrypt a completed job's full ``room_map`` snapshot.

        This is the "cleaning report" map the DJI app draws for a past job. Unlike
        ``/map`` (occupancy only) it carries every layer for that job's end state —
        ``seg_map`` (rooms), ``grid_map`` (occupancy), ``obstacle_layer``,
        ``carpet_layer``, ``restricted_layer``, ``virtual_wall`` — **plus** the dense
        sweep trace ``history_path`` (``[x, y, yaw, flag, type, width]`` points in
        metres) and ``robot_pos`` / ``station_pos``. It is a larger blob (~650 KB).

        The job detail only carries the ``room_map.file_id`` (no URL); it is resolved
        via ``GET /cr/app/api/v1/storage/{file_id}/url?file_id=<id>&sn=<sn>`` (both
        query params required — the server returns 121001 without them), which yields
        the S3 ``file_url`` + SSE-C ``file_header``; the blob decrypts with the share
        key.
        """
        share_key = await self._async_get_share_key()
        if share_key is None:
            return None

        try:
            job = await self._device_request("GET", f"jobs/cleans/{job_uuid}")
        except DjiRomoApiError:
            return None
        room_map = job.get("data", {}).get("room_map", {})
        file_id = room_map.get("file_id")
        if not file_id:
            return None

        file_url = room_map.get("file_url") or ""
        file_header: dict[str, str] = {}
        if not file_url:
            try:
                resolved = await self._request(
                    f"/cr/app/api/v1/storage/{file_id}/url",
                    params={"file_id": file_id, "sn": self._device_sn},
                )
            except DjiRomoApiError:
                return None
            data = resolved.get("data") or {}
            file_url = data.get("file_url", "")
            file_header = data.get("file_header", {})
        if not file_url:
            return None

        return await self._async_download_and_decrypt_map(
            file_url, file_header, share_key
        )

    async def async_get_homes(self) -> list[dict[str, Any]]:
        """Fetch homes and attached devices for the logged-in user."""
        payload = await self._request("/app/api/v1/homes")
        return payload.get("data", {}).get("homes", [])

    async def async_get_live_paths(
        self, bid: str, start_index: int
    ) -> dict[str, Any] | None:
        """Fetch incremental path points for an active cleaning job.

        Returns the raw API response dict, or None on any error.
        Endpoint: GET /cr/app/api/v1/devices/{SN}/paths?bid=...&start_index=...
        """
        try:
            return await self._device_request(
                "GET",
                "paths",
                params={"bid": bid, "start_index": start_index},
            )
        except DjiRomoApiError:
            return None

    async def async_get_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        """Fetch the most recent cleaning jobs, newest first."""
        jobs, _total = await self.async_get_jobs_and_total(limit)
        return jobs

    async def async_get_jobs_and_total(
        self, limit: int = 10
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Fetch recent jobs plus the lifetime job count (pagination total)."""
        payload = await self._device_request(
            "GET",
            "jobs/cleans/job/list",
            params={"offset": 0, "limit": limit},
        )
        data = payload.get("data", {})
        return data.get("job_list", []), _coerce_result_code(data.get("total"))

    async def async_get_active_job(self) -> dict[str, Any] | None:
        """Fetch the current cleaning job, if one is still running.

        Mirrors the coordinator's detection-by-exclusion: the *running* status
        string is not observable while the robot is docked, so any job whose
        status is not a known terminal one counts as active.
        """
        for job in await self.async_get_jobs():
            if str(job.get("status", "")).lower() not in TERMINAL_JOB_STATUSES:
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

    async def async_set_settings(self, param: dict[str, Any]) -> None:
        """Write one or more device settings.

        The write endpoint is ``PUT .../devices/{sn}/settings`` and the body the
        DJI Home app sends wraps the changed keys in a ``param`` object alongside
        a ``double_check`` flag, e.g. ``{"double_check": false, "param":
        {"is_child_lock_open": 0}}``. Sending the keys at the top level (without
        the ``param`` wrapper) is what made every earlier guess return ``121001
        "Request parameter error"``. ``param`` keys mirror the ``settings`` GET
        schema, so partial updates are fine — only the keys present are changed.
        """
        await self._device_request(
            "PUT",
            "settings",
            json={"double_check": False, "param": param},
        )

    async def async_set_voice_language(self, lang_code: str) -> None:
        """Switch the robot's voice language by upgrading its voicepack module.

        Language is NOT a settings write: the app POSTs a module upgrade and the
        robot downloads/installs the voicepack asynchronously. The settings
        ``device_language`` field only reflects the new language once installed.
        Endpoint + body captured via MITM 2026-06-22
        (``{"module_type": "voicepack_<code>"}``).
        """
        await self._device_request(
            "POST",
            "moduleFile/upgrade",
            json={"module_type": f"voicepack_{lang_code}"},
        )

    async def async_get_consumables(self) -> list[dict[str, Any]]:
        """Fetch robot consumable status."""
        payload = await self._device_request("GET", "consumables")
        return payload.get("data", {}).get("list", [])

    async def async_get_dock_consumables(self) -> dict[str, Any]:
        """Fetch dock consumable and tank status."""
        payload = await self._device_request("GET", "consumables/dock")
        return payload.get("data", {})

    async def async_get_consumable_notifications(self) -> list[dict[str, Any]]:
        """Fetch consumable notifications (both notify types, in parallel)."""
        payloads = await asyncio.gather(
            *(
                self._device_request(
                    "GET",
                    "consumables/notifications",
                    params={"notify_type": notify_type},
                )
                for notify_type in (0, 1)
            )
        )
        alerts: list[dict[str, Any]] = []
        for payload in payloads:
            alerts.extend(payload.get("data", {}).get("list", []))
        return alerts

    async def async_start_clean(self) -> None:
        """Start a whole-home cleaning job from the best available shortcut."""
        shortcuts = await self.async_get_shortcuts()
        if not shortcuts:
            raise DjiRomoApiError("No DJI Home cleaning shortcuts were returned.")

        await self.async_start_shortcut(_default_start_shortcut(shortcuts))

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
        await self.async_start_rooms([room_config], room_map, name)

    async def async_start_rooms(
        self,
        room_configs: list[dict[str, Any]],
        room_map: dict[str, Any],
        name: str,
    ) -> None:
        """Start a cleaning job covering one or more rooms, in the given order."""
        if not room_configs:
            raise DjiRomoApiError("No rooms were provided to clean.")
        area_configs = [
            {
                "config_uuid": str(uuid4()),
                "clean_mode": room_config.get("clean_mode", 2),
                "fan_speed": room_config.get("fan_speed", 2),
                "water_level": room_config.get("water_level", 2),
                "clean_num": room_config.get("clean_num", 1),
                "storm_mode": room_config.get("storm_mode", 0),
                "secondary_clean_num": room_config.get("secondary_clean_num", 1),
                "clean_speed": room_config.get("clean_speed", 2),
                "order_id": order_id,
                "poly_type": room_config.get("poly_type", 2),
                "poly_index": room_config.get("poly_index", 0),
                "poly_label": room_config.get("poly_label", 0),
                "user_label": room_config.get("user_label", 0),
                "poly_name_index": room_config.get("poly_name_index", 0),
                "skip_area": 0,
                "floor_cleaner_type": room_config.get("floor_cleaner_type", 0),
                "repeat_mop": room_config.get("repeat_mop", False),
            }
            for order_id, room_config in enumerate(room_configs, start=1)
        ]
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
                "plan_area_configs": area_configs,
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
        result_code = _coerce_result_code(result.get("code"))
        allowed = allowed_result_codes or {0}
        if result_code not in allowed:
            message = result.get("message") or "Unknown DJI Romo device API error"
            raise DjiRomoApiError(message)

        _LOGGER.debug(
            "DJI Romo device API response for %s %s: %s",
            method,
            path,
            _loggable_payload(path, payload),
        )
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
        if _coerce_result_code(result.get("code")) != 0:
            message = result.get("message") or "Unknown DJI Home API error"
            if "token" in message.lower() or "auth" in message.lower():
                raise DjiRomoAuthError(message)
            raise DjiRomoApiError(message)

        _LOGGER.debug(
            "DJI Home API response for %s: %s", path, _loggable_payload(path, payload)
        )
        return payload

    def _headers(self, *, include_json: bool = False) -> dict[str, str]:
        """Return DJI Home app-like request headers."""
        headers = {
            "x-member-token": self._user_token,
            "X-DJI-locale": self._locale,
            # Matches the DJI Home app version the API behaviour was captured
            # from (verified live 2026-07-02: settings GET returns code 0).
            "version-name": "1.6.0",
            "User-Agent": "DJI-Home/1.6.0",
            "x-request-start": str(int(datetime.now(UTC).timestamp() * 1000)),
        }
        if include_json:
            headers["Content-Type"] = "application/json"
        return headers


def decode_grid_cells(
    grid: dict[str, Any],
    *,
    categories: tuple[int, ...] | None = None,
) -> list[tuple[int, int]]:
    """Decode an occupancy ``grid_map`` into the list of set ``(gx, gy)`` cells.

    ``grid_map.map_data[].data`` is a list of base64 chunks; chunk *i* is one
    65536-cell block of **sorted little-endian uint16 offsets**, so the true flat
    cell index is ``i * 65536 + uint16``. Missing that block offset collapses every
    chunk past the first onto the same rows (the original decode bug).

    ``categories`` selects which grid layers to include. The default (``None``)
    returns every **non-zero** category (the scanned occupancy detail) and skips
    category 0, which is the SLAM wall layer; pass e.g. ``(0,)`` to get just the
    walls. Returns integer grid coordinates ``(gx, gy)`` — the caller maps them to
    world metres via ``map_info`` (``origin_x + gx*resolution``, ``origin_y +
    gy*resolution``).
    """
    map_info = grid.get("map_info") or {}
    try:
        width = int(map_info.get("width") or 0)
    except (TypeError, ValueError):
        width = 0
    if width <= 0:
        return []

    cells: list[tuple[int, int]] = []
    for item in grid.get("map_data", []):
        cat = item.get("category", 0)
        if categories is None:
            if cat == 0:
                continue
        elif cat not in categories:
            continue
        for block_index, chunk in enumerate(item.get("data", [])):
            try:
                raw = base64.b64decode(chunk)
            except Exception:  # noqa: BLE001 - skip an unparsable chunk
                continue
            base = block_index * 65536
            # Trim a stray odd byte so iter_unpack never raises.
            for (offset,) in struct.iter_unpack("<H", raw[: len(raw) & ~1]):
                flat = base + offset
                cells.append((flat % width, flat // width))
    return cells


def _default_start_shortcut(shortcuts: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick a whole-home plan for the generic Home Assistant "start" action.

    DJI accounts usually expose several built-in programs plus user-created
    one-room shortcuts. Picking ``shortcuts[0]`` would often start a single-room
    clean. Prefer a known whole-home program by ``plan_name_key``, otherwise the
    shortcut that covers the most rooms.
    """
    by_key = {
        str(s.get("plan_name_key") or ""): s
        for s in shortcuts
        if s.get("plan_name_key")
    }
    for key in DEFAULT_START_PLAN_KEYS:
        if key in by_key:
            return by_key[key]
    return max(
        shortcuts,
        key=lambda s: len(s.get("plan_area_configs", [])),
    )


def _coerce_result_code(value: Any) -> int | None:
    """Return DJI result codes as integers when possible."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


