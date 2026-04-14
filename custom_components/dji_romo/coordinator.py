"""State coordinator for DJI Romo."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
from typing import Any
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import DjiMqttCredentials, DjiRomoApiClient, DjiRomoApiError
from .const import (
    COORDINATOR_REFRESH_INTERVAL,
    CONF_COMMAND_MAPPING,
    CONF_COMMAND_TOPIC,
    CONF_DEVICE_NAME,
    CONF_DEVICE_SN,
    CONF_SUBSCRIPTION_TOPICS,
    DEFAULT_COMMAND_MAPPING,
    MQTT_CREDENTIAL_ASSUMED_LIFETIME,
    MQTT_CREDENTIAL_REFRESH_MARGIN,
)
from .mqtt import DjiRomoMqttClient

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RomoSnapshot:
    """Current best-effort picture of the robot state."""

    battery_level: int | None = None
    activity: str = "idle"
    status_text: str | None = None
    selected_topic: str | None = None
    cleaned_area: float | None = None
    fan_speed: str | None = None
    last_updated: datetime | None = None
    raw_state: dict[str, Any] = field(default_factory=dict)


class DjiRomoCoordinator(DataUpdateCoordinator[RomoSnapshot]):
    """Coordinate cloud metadata and MQTT state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: DjiRomoApiClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="DJI Romo",
            update_interval=COORDINATOR_REFRESH_INTERVAL,
        )
        self.entry = entry
        self.api = api
        self.device_sn: str = entry.data[CONF_DEVICE_SN]
        self.device_name: str = entry.data[CONF_DEVICE_NAME]
        self.device_info_payload: dict[str, Any] = {}
        self._mqtt_credentials: DjiMqttCredentials | None = None
        self._mqtt = DjiRomoMqttClient(hass.loop, self._handle_mqtt_message)

    async def _async_update_data(self) -> RomoSnapshot:
        """Refresh cloud metadata and keep the MQTT session healthy."""
        await self._async_ensure_mqtt()

        try:
            device = await self.api.async_resolve_device(self.device_sn)
        except DjiRomoApiError as err:
            if "token" in str(err).lower():
                raise ConfigEntryAuthFailed(str(err)) from err
            _LOGGER.debug(
                "Skipping DJI Romo device metadata refresh because homes lookup failed: %s",
                err,
            )
        else:
            self.device_info_payload = device
            self.device_name = (
                self.entry.options.get(CONF_DEVICE_NAME)
                or device.get("name")
                or self.entry.data[CONF_DEVICE_NAME]
            )

        if self.last_update_success and self.data is not None:
            return self.data

        return RomoSnapshot()

    async def async_shutdown(self) -> None:
        """Stop MQTT alongside coordinator shutdown."""
        await self._mqtt.async_disconnect()
        await super().async_shutdown()

    async def async_send_named_command(
        self,
        command_key: str,
        params: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        """Send a logical command using the configurable mapping."""
        mapping = self.command_mapping.get(command_key)
        if mapping is None:
            raise UpdateFailed(
                f"Command mapping for '{command_key}' is not configured."
            )

        if isinstance(mapping, str):
            envelope = {"method": mapping}
        else:
            envelope = deepcopy(mapping)

        method = envelope.pop("method", command_key)
        data = envelope.pop("data", {})
        if params is not None:
            data = params

        payload = {
            "bid": str(uuid4()),
            "method": method,
            "timestamp": int(datetime.now(UTC).timestamp() * 1000),
            "data": data,
            **envelope,
        }
        await self._async_publish(payload)

    async def async_send_raw_command(
        self,
        command: str,
        params: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        """Send a raw command through the services topic."""
        payload = {
            "bid": str(uuid4()),
            "method": command,
            "timestamp": int(datetime.now(UTC).timestamp() * 1000),
            "data": params or {},
        }
        await self._async_publish(payload)

    @property
    def command_topic(self) -> str:
        """Resolved MQTT topic for commands."""
        return (
            self.entry.options.get(CONF_COMMAND_TOPIC)
            or self.entry.data.get(CONF_COMMAND_TOPIC)
        ).format(device_sn=self.device_sn)

    @property
    def command_mapping(self) -> dict[str, Any]:
        """Merged command mapping from config and defaults."""
        raw = (
            self.entry.options.get(CONF_COMMAND_MAPPING)
            or self.entry.data.get(CONF_COMMAND_MAPPING)
            or {}
        )
        merged = dict(DEFAULT_COMMAND_MAPPING)
        merged.update(raw)
        return merged

    @property
    def subscription_topics(self) -> list[str]:
        """Resolved MQTT subscriptions."""
        topics = (
            self.entry.options.get(CONF_SUBSCRIPTION_TOPICS)
            or self.entry.data[CONF_SUBSCRIPTION_TOPICS]
        )
        return [topic.format(device_sn=self.device_sn) for topic in topics]

    async def _async_ensure_mqtt(self) -> None:
        """Refresh MQTT credentials before expiry and maintain the connection."""
        if self._mqtt_credentials is None or self._mqtt_credentials.fetched_at <= (
            datetime.now(UTC) - MQTT_CREDENTIAL_ASSUMED_LIFETIME + MQTT_CREDENTIAL_REFRESH_MARGIN
        ):
            try:
                self._mqtt_credentials = await self.api.async_get_mqtt_credentials()
            except DjiRomoApiError as err:
                raise UpdateFailed(f"Failed to obtain MQTT credentials: {err}") from err

        await self._mqtt.async_connect(
            self._mqtt_credentials,
            self.subscription_topics,
        )

    async def _async_publish(self, payload: dict[str, Any]) -> None:
        """Publish a payload after ensuring MQTT connectivity."""
        await self._async_ensure_mqtt()
        _LOGGER.debug("Publishing DJI Romo payload to %s: %s", self.command_topic, payload)
        await self._mqtt.async_publish(self.command_topic, payload)

    def _handle_mqtt_message(self, topic: str, payload: Any) -> None:
        """Parse a pushed MQTT message into a snapshot."""
        snapshot = deepcopy(self.data) if self.data else RomoSnapshot()
        snapshot.selected_topic = topic
        snapshot.last_updated = datetime.now(UTC)

        if isinstance(payload, dict):
            snapshot.raw_state[topic] = payload
            flattened = _flatten_dict(payload)
            battery_level = _coerce_int(
                _pick_first(
                    flattened,
                    (
                        "battery",
                        "battery_level",
                        "electricity",
                        "power_percent",
                        "soc",
                    ),
                )
            )
            if battery_level is not None:
                snapshot.battery_level = battery_level

            cleaned_area = _coerce_float(
                _pick_first(flattened, ("cleaned_area", "clean_area", "area"))
            )
            if cleaned_area is not None:
                snapshot.cleaned_area = cleaned_area

            fan_speed = _pick_first(
                flattened,
                ("fan_speed", "suction", "mode", "clean_mode"),
            )
            if fan_speed is not None:
                snapshot.fan_speed = fan_speed

            status_text = _pick_first(
                flattened,
                ("status", "state", "work_status", "clean_status", "phase"),
            )
            if status_text is not None:
                snapshot.status_text = status_text
            snapshot.activity = _infer_activity(flattened, snapshot.status_text)
        else:
            snapshot.raw_state[topic] = {"value": payload}
            snapshot.status_text = str(payload)
            snapshot.activity = _infer_activity({}, snapshot.status_text)

        self.async_set_updated_data(snapshot)


def _flatten_dict(
    payload: dict[str, Any],
    prefix: str = "",
) -> dict[str, Any]:
    """Flatten nested dict/list payloads so heuristic matching stays simple."""
    flattened: dict[str, Any] = {}
    for key, value in payload.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        flattened[path] = value
        flattened[str(key)] = value
        if isinstance(value, dict):
            flattened.update(_flatten_dict(value, path))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                item_key = f"{path}[{index}]"
                flattened[item_key] = item
                if isinstance(item, dict):
                    flattened.update(_flatten_dict(item, item_key))
    return flattened


def _pick_first(flattened: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Pick a value if any flattened key ends with one of the requested names."""
    for target in keys:
        for key, value in flattened.items():
            if key == target or key.endswith(f".{target}"):
                return value
    return None


def _coerce_int(value: Any) -> int | None:
    """Convert a candidate value to int."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    """Convert a candidate value to float."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _infer_activity(flattened: dict[str, Any], status_text: str | None) -> str:
    """Map loose status payloads to HA vacuum activities."""
    values = " ".join(
        str(value).lower()
        for value in [status_text, *flattened.values()]
        if value is not None
    )
    if any(term in values for term in ("error", "fault", "stuck", "blocked")):
        return "error"
    if any(term in values for term in ("return", "go_home", "back_charge", "docking")):
        return "returning"
    if any(term in values for term in ("pause", "paused")):
        return "paused"
    if any(term in values for term in ("charge", "charging", "dock", "docked")):
        return "docked"
    if any(
        term in values for term in ("clean", "cleaning", "sweep", "mop", "working")
    ):
        return "cleaning"
    return "idle"
