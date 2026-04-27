"""State coordinator for DJI Romo."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
from typing import Any
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import DjiMqttCredentials, DjiRomoApiClient, DjiRomoApiError, DjiRomoAuthError
from .const import (
    COORDINATOR_REFRESH_INTERVAL,
    CONF_COMMAND_MAPPING,
    CONF_COMMAND_TOPIC,
    CONF_DEVICE_NAME,
    CONF_DEVICE_SN,
    CONF_ROOM_CLEAN_MODE,
    CONF_ROOM_CLEAN_NUM,
    CONF_ROOM_CLEAN_SPEED,
    CONF_ROOM_FAN_SPEED,
    CONF_ROOM_WATER_LEVEL,
    CONF_SUBSCRIPTION_TOPICS,
    DEFAULT_COMMAND_MAPPING,
    DOMAIN,
    MQTT_CREDENTIAL_ASSUMED_LIFETIME,
    MQTT_CREDENTIAL_REFRESH_MARGIN,
)
from .mqtt import DjiRomoMqttClient

_LOGGER = logging.getLogger(__name__)
AUTH_REPAIR_ISSUE_ID = "auth_failed"
DEFAULT_ROOM_CLEANING_OPTIONS = {
    CONF_ROOM_CLEAN_MODE: 2,
    CONF_ROOM_FAN_SPEED: 3,
    CONF_ROOM_WATER_LEVEL: 2,
    CONF_ROOM_CLEAN_NUM: 1,
    CONF_ROOM_CLEAN_SPEED: 2,
}
MEANINGFUL_STATE_KEYS = (
    "battery_level",
    "activity",
    "status_text",
    "mission_bid",
    "cleaned_area",
    "fan_speed",
    "clean_mode",
    "water_level",
    "clean_num",
    "clean_speed",
    "cloud_data",
)


@dataclass(slots=True)
class RomoSnapshot:
    """Current best-effort picture of the robot state."""

    battery_level: int | None = None
    activity: str = "idle"
    status_text: str | None = None
    selected_topic: str | None = None
    mission_bid: str | None = None
    cleaned_area: float | None = None
    fan_speed: int | None = None
    clean_mode: int | None = None
    water_level: int | None = None
    clean_num: int | None = None
    clean_speed: int | None = None
    last_updated: datetime | None = None
    cloud_last_updated: datetime | None = None
    cloud_data: dict[str, Any] = field(default_factory=dict)
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
        self.device_name = (
            self.entry.options.get(CONF_DEVICE_NAME)
            or self.entry.data[CONF_DEVICE_NAME]
        )

        snapshot = deepcopy(self.data) if self.data else RomoSnapshot()
        await self._async_refresh_cloud_data(snapshot)

        if self.last_update_success and self.data is not None:
            return snapshot

        return snapshot

    async def async_shutdown(self) -> None:
        """Stop MQTT alongside coordinator shutdown."""
        await self._mqtt.async_disconnect()
        await super().async_shutdown()

    async def _async_refresh_cloud_data(self, snapshot: RomoSnapshot) -> None:
        """Refresh slower REST details used by diagnostic sensors."""
        try:
            (
                properties,
                settings,
                consumables,
                dock_consumables,
                consumable_alerts,
            ) = await asyncio.gather(
                self.api.async_get_properties(),
                self.api.async_get_settings(),
                self.api.async_get_consumables(),
                self.api.async_get_dock_consumables(),
                self.api.async_get_consumable_notifications(),
            )
        except DjiRomoAuthError as err:
            self._async_create_auth_repair_issue(str(err))
            raise ConfigEntryAuthFailed(
                f"DJI Home authentication failed: {err}"
            ) from err
        except DjiRomoApiError as err:
            _LOGGER.warning("Failed to refresh DJI Romo cloud details: %s", err)
            return

        self._async_delete_auth_repair_issue()
        snapshot.cloud_data = {
            "properties": properties,
            "settings": settings,
            "consumables": {
                item.get("code"): item
                for item in consumables
                if isinstance(item, dict) and item.get("code")
            },
            "dock_consumables": dock_consumables,
            "consumable_alerts": consumable_alerts,
        }
        snapshot.cloud_last_updated = datetime.now(UTC)

        if battery := _coerce_int(
            _pick_first(_flatten_dict(properties), ("battery",))
        ):
            snapshot.battery_level = battery

    async def async_send_named_command(
        self,
        command_key: str,
        params: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        """Send a logical command using the configurable mapping."""
        if params is None and await self._async_send_rest_command(command_key):
            return

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

    async def async_start_shortcut(self, shortcut: dict[str, Any]) -> None:
        """Start a DJI Home cleaning shortcut and surface auth failures."""
        try:
            await self.api.async_start_shortcut(shortcut)
        except DjiRomoAuthError as err:
            self._async_create_auth_repair_issue(str(err))
            raise UpdateFailed(f"Failed to start DJI Romo shortcut: {err}") from err
        except DjiRomoApiError as err:
            raise UpdateFailed(f"Failed to start DJI Romo shortcut: {err}") from err

    async def async_start_room(
        self,
        room_config: dict[str, Any],
        room_map: dict[str, Any],
        name: str,
    ) -> None:
        """Start a DJI Home room clean and surface auth failures."""
        try:
            await self.api.async_start_room(
                self.room_cleaning_config(room_config),
                room_map,
                name,
            )
        except DjiRomoAuthError as err:
            self._async_create_auth_repair_issue(str(err))
            raise UpdateFailed(f"Failed to start DJI Romo room '{name}': {err}") from err
        except DjiRomoApiError as err:
            raise UpdateFailed(f"Failed to start DJI Romo room '{name}': {err}") from err

    def room_cleaning_config(self, base_config: dict[str, Any]) -> dict[str, Any]:
        """Return a room config with the selected HA cleaning options applied."""
        config = dict(base_config)
        options = self.room_cleaning_options
        config["clean_mode"] = options[CONF_ROOM_CLEAN_MODE]
        config["fan_speed"] = options[CONF_ROOM_FAN_SPEED]
        config["water_level"] = options[CONF_ROOM_WATER_LEVEL]
        config["clean_num"] = options[CONF_ROOM_CLEAN_NUM]
        config["clean_speed"] = (
            0
            if options[CONF_ROOM_CLEAN_MODE] == 2
            else options[CONF_ROOM_CLEAN_SPEED]
        )
        config["secondary_clean_num"] = base_config.get("secondary_clean_num", 1)
        config["floor_cleaner_type"] = base_config.get("floor_cleaner_type", 0)
        config["repeat_mop"] = base_config.get("repeat_mop", False)
        return config

    @property
    def room_cleaning_options(self) -> dict[str, int]:
        """Return selected room-cleaning options."""
        options = dict(DEFAULT_ROOM_CLEANING_OPTIONS)
        for key in options:
            value = self.entry.data.get(key, self.entry.options.get(key))
            if value is not None:
                try:
                    options[key] = int(value)
                except (TypeError, ValueError):
                    pass
        return options

    async def async_set_room_cleaning_option(self, key: str, value: int) -> None:
        """Persist a room-cleaning option and refresh config-backed entities."""
        if key not in DEFAULT_ROOM_CLEANING_OPTIONS:
            raise UpdateFailed(f"Unknown DJI Romo cleaning option '{key}'.")
        cleaned_options = dict(self.entry.options)
        for option_key in DEFAULT_ROOM_CLEANING_OPTIONS:
            cleaned_options.pop(option_key, None)
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, key: int(value)},
            options=cleaned_options,
        )
        self.async_set_updated_data(deepcopy(self.data) if self.data else RomoSnapshot())

    async def async_run_dock_action(self, action: str) -> None:
        """Run a dock action and surface auth failures."""
        action_map = {
            "dust_collect": self.api.async_dust_collect,
            "wash_mop_pads": self.api.async_wash_mop_pads,
            "dry_mop_pads": self.api.async_start_drying,
        }
        if action not in action_map:
            raise UpdateFailed(f"Unknown DJI Romo dock action '{action}'.")
        try:
            await action_map[action]()
        except DjiRomoAuthError as err:
            self._async_create_auth_repair_issue(str(err))
            raise UpdateFailed(f"Failed to run DJI Romo dock action '{action}': {err}") from err
        except DjiRomoApiError as err:
            raise UpdateFailed(f"Failed to run DJI Romo dock action '{action}': {err}") from err

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
            except DjiRomoAuthError as err:
                self._async_create_auth_repair_issue(str(err))
                raise ConfigEntryAuthFailed(
                    f"DJI Home authentication failed: {err}"
                ) from err
            except DjiRomoApiError as err:
                raise UpdateFailed(f"Failed to obtain MQTT credentials: {err}") from err
            self._async_delete_auth_repair_issue()

        await self._mqtt.async_connect(
            self._mqtt_credentials,
            self.subscription_topics,
        )

    async def _async_publish(self, payload: dict[str, Any]) -> None:
        """Publish a payload after ensuring MQTT connectivity."""
        await self._async_ensure_mqtt()
        _LOGGER.debug("Publishing DJI Romo payload to %s: %s", self.command_topic, payload)
        await self._mqtt.async_publish(self.command_topic, payload)

    async def _async_send_rest_command(self, command_key: str) -> bool:
        """Send commands that are known to be DJI Home REST job actions."""
        try:
            if command_key == "start":
                if self.data and self.data.activity == "paused":
                    await self.api.async_resume_cleaning(self.data.mission_bid)
                else:
                    await self.api.async_start_clean()
                return True
            if command_key == "pause":
                await self.api.async_pause_cleaning(self.data.mission_bid if self.data else None)
                return True
            if command_key == "stop":
                await self.api.async_stop_cleaning(self.data.mission_bid if self.data else None)
                return True
            if command_key == "return_to_base":
                if (
                    self.data
                    and self.data.activity in {"cleaning", "paused"}
                    and self.data.mission_bid
                ):
                    await self.api.async_stop_cleaning(self.data.mission_bid)
                else:
                    await self.api.async_return_to_base()
                return True
        except DjiRomoApiError as err:
            if isinstance(err, DjiRomoAuthError):
                self._async_create_auth_repair_issue(str(err))
            raise UpdateFailed(f"Failed to send DJI Romo command '{command_key}': {err}") from err

        return False

    def _async_create_auth_repair_issue(self, error: str) -> None:
        """Create a Home Assistant repair issue for expired DJI auth."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            AUTH_REPAIR_ISSUE_ID,
            breaks_in_ha_version=None,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="auth_failed",
            translation_placeholders={"error": error},
        )

    def _async_delete_auth_repair_issue(self) -> None:
        """Remove the auth repair issue after a successful auth refresh."""
        ir.async_delete_issue(self.hass, DOMAIN, AUTH_REPAIR_ISSUE_ID)

    def _handle_mqtt_message(self, topic: str, payload: Any) -> None:
        """Parse a pushed MQTT message into a snapshot."""
        previous = deepcopy(self.data) if self.data else RomoSnapshot()
        snapshot = deepcopy(previous)
        snapshot.selected_topic = topic
        topic_kind = _topic_kind(topic)

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

            fan_speed = _coerce_int(_pick_first(flattened, ("fan_speed", "suction")))
            if fan_speed is not None:
                snapshot.fan_speed = fan_speed

            clean_mode = _coerce_int(_pick_first(flattened, ("clean_mode",)))
            if clean_mode is not None:
                snapshot.clean_mode = clean_mode

            water_level = _coerce_int(_pick_first(flattened, ("water_level",)))
            if water_level is not None:
                snapshot.water_level = water_level

            clean_num = _coerce_int(_pick_first(flattened, ("clean_num",)))
            if clean_num is not None:
                snapshot.clean_num = clean_num

            clean_speed = _coerce_int(_pick_first(flattened, ("clean_speed",)))
            if clean_speed is not None:
                snapshot.clean_speed = clean_speed

            if topic_kind == "property":
                mission_bid = _pick_first(flattened, ("mission_bid",))
                if mission_bid is not None:
                    snapshot.mission_bid = str(mission_bid) or None
                status_text = _pick_first(
                    flattened,
                    (
                        "mission_status",
                        "robot_position.status",
                        "work_status",
                        "clean_status",
                        "phase",
                        "status",
                        "state",
                    ),
                )
                if status_text is not None:
                    snapshot.status_text = status_text
                snapshot.activity = _infer_property_activity(
                    flattened,
                    snapshot.status_text,
                    previous.activity,
                )
            elif topic_kind == "events":
                event_activity = _infer_event_activity(flattened, previous.activity)
                if event_activity is not None:
                    snapshot.activity = event_activity
        else:
            snapshot.raw_state[topic] = {"value": payload}
            snapshot.status_text = str(payload)
            snapshot.activity = _infer_property_activity(
                {}, snapshot.status_text, previous.activity
            )

        if not _meaningful_state_changed(previous, snapshot):
            return

        snapshot.last_updated = datetime.now(UTC)
        self.async_set_updated_data(snapshot)


def _meaningful_state_changed(previous: RomoSnapshot, current: RomoSnapshot) -> bool:
    """Return True when a meaningful entity state changed."""
    for key in MEANINGFUL_STATE_KEYS:
        if getattr(previous, key) != getattr(current, key):
            return True
    return False


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


def _topic_kind(topic: str) -> str:
    """Classify the Romo MQTT topic."""
    if topic.endswith("/property"):
        return "property"
    if topic.endswith("/events"):
        return "events"
    if topic.endswith("/services"):
        return "services"
    return "other"


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


def _infer_property_activity(
    flattened: dict[str, Any],
    status_text: str | None,
    previous_activity: str | None = None,
) -> str:
    """Map property payloads to stable HA vacuum activities."""
    mission_status = _coerce_int(_pick_first(flattened, ("mission_status",)))
    charger_connected = _coerce_int(_pick_first(flattened, ("charger_connected",)))
    mission_bid = _pick_first(flattened, ("mission_bid",))
    values = " ".join(
        str(value).lower()
        for value in (
            status_text,
            _pick_first(flattened, ("work_status", "clean_status", "phase")),
        )
        if value is not None
    )

    if any(term in values for term in ("error", "fault", "stuck", "blocked")):
        return "error"

    if mission_status == 3:
        return "returning"
    if mission_status == 2:
        return "cleaning"
    if mission_status == 1:
        return "paused"
    if charger_connected == 1:
        return "docked"
    if mission_status == 0 and mission_bid:
        return "idle"

    if any(term in values for term in ("return", "go_home", "back_charge", "docking")):
        return "returning"
    if any(term in values for term in ("pause", "paused")):
        return "paused"
    if any(term in values for term in ("clean", "cleaning", "sweep", "mop", "working")):
        return "cleaning"
    if previous_activity in {"docked", "returning", "paused", "cleaning"}:
        return previous_activity
    return "idle"


def _infer_event_activity(
    flattened: dict[str, Any],
    previous_activity: str | None = None,
) -> str | None:
    """Interpret task events without letting stale event spam override property state."""
    event_status = _pick_first(flattened, ("status", "submission_state"))
    if str(event_status).lower() == "paused":
        return "paused"
    if str(event_status).lower() != "in_progress":
        return None

    submission_state_value = _pick_first(flattened, ("submission_state",))
    submission_state = (
        str(submission_state_value).lower()
        if submission_state_value is not None
        else ""
    )
    if submission_state and submission_state not in {"running", "in_progress"}:
        return None

    values = " ".join(
        str(value).lower()
        for value in (
            _pick_first(flattened, ("cur_submission",)),
            _pick_first(flattened, ("method",)),
            _pick_first(flattened, ("display_text_key",)),
        )
        if value is not None
    )
    if any(term in values for term in ("go_home", "return", "back_charge", "dock")):
        return "returning"
    if any(term in values for term in ("dust_collect", "charge")):
        return "docked"
    if any(term in values for term in ("clean", "sweep", "mop", "room")):
        if previous_activity == "paused":
            return None
        return "cleaning"
    return None
