"""State coordinator for DJI Romo."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
import logging
from typing import Any
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import (
    DjiMqttCredentials,
    DjiRomoApiClient,
    DjiRomoApiError,
    DjiRomoAuthError,
)
from .const import (
    AVAILABILITY_CHECK_INTERVAL,
    CLEANING_REFRESH_INTERVAL,
    CLOUD_REFRESH_FAILURE_LIMIT,
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
    COORDINATOR_REFRESH_INTERVAL,
    DEFAULT_COMMAND_MAPPING,
    DOMAIN,
    EVENT_HMS,
    MQTT_CREDENTIAL_ASSUMED_LIFETIME,
    MQTT_CREDENTIAL_REFRESH_MARGIN,
    MQTT_STALE_AFTER,
    OFFLINE_AFTER,
    STATIC_REFRESH_INTERVAL,
    TRAJECTORY_MAX_POINTS,
    TRAJECTORY_SAVE_DELAY,
    TRAJECTORY_STORAGE_KEY,
    TRAJECTORY_STORAGE_POINTS,
    TRAJECTORY_STORAGE_VERSION,
)
from .mqtt import DjiRomoMqttClient, DjiRomoMqttError
from .rooms import duplicate_label_ids, room_configs_from_shortcuts, room_name

_LOGGER = logging.getLogger(__name__)
# Typed config entry carrying the coordinator in runtime_data (PEP 695 lazy alias,
# so the forward reference to the class below resolves fine).
type DjiRomoConfigEntry = ConfigEntry[DjiRomoCoordinator]
AUTH_REPAIR_ISSUE_ID = "auth_failed"
ACTIVITY_CONFIRMATION_COUNT = 2
# Job statuses that mean the job is finished (verified live: DJI returns "ok" for
# a successful run and "canceled" for an aborted one). Anything not in this set is
# treated as an active job. We avoid hard-coding the *running* status string since
# it is not observable while the robot is docked.
TERMINAL_JOB_STATUSES = frozenset(
    {
        "ok",
        "canceled",
        "cancelled",
        "completed",
        "complete",
        "failed",
        "fail",
        "error",
        "stopped",
        "stop",
        "timeout",
        "interrupted",
        "abort",
        "aborted",
    }
)
ACTIVITY_HOLD_DURATION = timedelta(seconds=20)
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
    "online",
    "robot_x",
    "robot_y",
    "current_room",
    "cloud_data",
    "clean_progress",
    "clean_duration_s",
    "clean_remaining_s",
    "charger_connected",
    "battery_care_active",
    "dust_bag_uv_enable",
    "hatch_status",
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
    online: bool = True
    robot_x: float | None = None
    robot_y: float | None = None
    robot_yaw: float | None = None
    dock_x: float | None = None
    dock_y: float | None = None
    current_room: str | None = None
    active_step: int | None = None
    # Poly index of the room the robot is actually cleaning right now, from the
    # live room_clean_progress event (area_cleaning.current_poly_index). Preferred
    # over the step+plan derivation, which breaks when the active job isn't in REST.
    active_poly_index: int | None = None
    last_osd_at: datetime | None = None
    last_updated: datetime | None = None
    cloud_last_updated: datetime | None = None
    cloud_data: dict[str, Any] = field(default_factory=dict)
    last_job: dict[str, Any] = field(default_factory=dict)
    active_job: dict[str, Any] = field(default_factory=dict)
    rooms: list[dict[str, Any]] = field(default_factory=list)
    hms_alerts: list[dict[str, Any]] = field(default_factory=list)
    # The robot's swept path for the current session, accumulated from MQTT
    # position samples only while it is actively sweeping. Rendered as the cleaning
    # band on the map (matching the DJI app). Reset when a new session starts.
    trajectory: list[tuple[float, float]] = field(default_factory=list)
    total_cleanings: int | None = None
    # Floor plan polygons from seg_map.poly_info (each has vertices, poly_label, etc.)
    floor_plan_polys: list[dict[str, Any]] = field(default_factory=list)
    grid_map_data: dict[str, Any] | None = None
    # The last *completed* cleaning's full report map (rooms + grid + obstacles +
    # carpets + restricted zones + the dense ``history_path`` sweep trace +
    # robot_pos/station_pos), fetched from the per-job room_map snapshot. Rendered
    # by the "Last Cleaning" image. Refetched only when the newest finished job
    # changes (it is a ~650 KB blob).
    last_clean_map: dict[str, Any] | None = None
    last_clean_map_uuid: str | None = None
    carpet_polys: list[dict[str, Any]] = field(default_factory=list)
    restricted_polys: list[dict[str, Any]] = field(default_factory=list)
    virtual_walls: list[dict[str, Any]] = field(default_factory=list)
    # Point obstacles from obstacle_layer in live_map_update (furniture legs, toys, etc.)
    obstacles: list[tuple[float, float]] = field(default_factory=list)
    # Dock drying state, from the MQTT drying_progress event (dust box / mop drying).
    drying_active: bool = False
    drying_stage: str | None = None
    drying_percent: int | None = None
    drying_remaining_s: int | None = None
    # Live progress of the current cleaning job, from room_clean_progress events.
    # Cleared when the robot returns to idle/docked.
    clean_progress: int | None = None
    clean_duration_s: int | None = None
    clean_remaining_s: int | None = None
    # Live dock/robot flags pushed in the device_osd stream (also seeded from REST
    # so they have a value before the first osd). Let binary sensors update in ~1 s.
    charger_connected: int | None = None
    battery_care_active: int | None = None
    dust_bag_uv_enable: bool | None = None
    hatch_status: int | None = None


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
        # Cache of rarely-changing REST data (settings/consumables/shortcuts) so the
        # fast cleaning poll doesn't refetch them every cycle.
        self._static_cache: dict[str, Any] | None = None
        self._static_fetched_at: datetime | None = None
        # UUID of the completed job whose report map is cached in the snapshot, so we
        # only refetch the (large) room_map when a newer finished job appears.
        self._last_clean_map_uuid: str | None = None
        self._mqtt_credentials: DjiMqttCredentials | None = None
        # No connection-lost callback: paho auto-reconnects transient broker
        # drops seamlessly with the same client (and re-subscribes via on_connect).
        # We only step in for sustained outages, from the availability timer.
        self._mqtt = DjiRomoMqttClient(hass.loop, self._handle_mqtt_message)
        self._mqtt_down_checks = 0
        self._mqtt_recovering = False
        # Consecutive REST refresh failures; entities go unavailable past the limit.
        self._cloud_refresh_failures = 0
        # Serializes settings writes so two switches sharing one nested object
        # (e.g. add_cleaner_auto) can't clobber each other: the param is built
        # under this lock, after the previous write's optimistic patch landed.
        self._settings_write_lock = asyncio.Lock()
        self._availability_unsub: CALLBACK_TYPE | None = None
        self._pending_activity: str | None = None
        self._pending_activity_count = 0
        self._held_activity: str | None = None
        self._activity_hold_until: datetime | None = None
        self._paths_unsub: CALLBACK_TYPE | None = None
        self._paths_next_index: int = 0
        # Persisted trajectory so the live map survives a Home Assistant restart.
        self._store: Store[dict[str, Any]] = Store(
            hass,
            TRAJECTORY_STORAGE_VERSION,
            f"{TRAJECTORY_STORAGE_KEY}_{self.device_sn}",
        )
        self._restored: dict[str, Any] | None = None

    async def async_setup(self) -> None:
        """Load persisted state and start the periodic offline-by-silence check.

        Runs before the first refresh so the restored trajectory/positions seed
        the very first snapshot (the map is not blank right after a restart).
        """
        self._restored = await self._store.async_load()
        self._availability_unsub = async_track_time_interval(
            self.hass,
            self._async_check_availability,
            AVAILABILITY_CHECK_INTERVAL,
        )

    async def _async_update_data(self) -> RomoSnapshot:
        """Refresh cloud metadata and keep the MQTT session healthy."""
        await self._async_ensure_mqtt()
        self.device_name = (
            self.entry.options.get(CONF_DEVICE_NAME)
            or self.entry.data[CONF_DEVICE_NAME]
        )

        if self.data is not None:
            snapshot = replace(self.data)
        else:
            snapshot = RomoSnapshot()
            self._seed_from_restore(snapshot)
        await self._async_refresh_cloud_data(snapshot)

        # Poll faster while cleaning so the "current room" sensor tracks the plan.
        self.update_interval = (
            CLEANING_REFRESH_INTERVAL
            if snapshot.activity == "cleaning"
            else COORDINATOR_REFRESH_INTERVAL
        )
        return snapshot

    async def async_shutdown(self) -> None:
        """Stop MQTT alongside coordinator shutdown."""
        if self._availability_unsub is not None:
            self._availability_unsub()
            self._availability_unsub = None
        await self._mqtt.async_disconnect()
        await super().async_shutdown()

    def _seed_from_restore(self, snapshot: RomoSnapshot) -> None:
        """Seed the first snapshot from the persisted trajectory/positions."""
        if not self._restored:
            return
        trajectory = self._restored.get("trajectory")
        if isinstance(trajectory, list):
            snapshot.trajectory = [
                (float(p[0]), float(p[1]))
                for p in trajectory
                if isinstance(p, (list, tuple)) and len(p) >= 2
            ][-TRAJECTORY_MAX_POINTS:]
        for attr, key in (
            ("robot_x", "robot_x"),
            ("robot_y", "robot_y"),
            ("robot_yaw", "robot_yaw"),
            ("dock_x", "dock_x"),
            ("dock_y", "dock_y"),
        ):
            value = self._restored.get(key)
            if isinstance(value, (int, float)):
                setattr(snapshot, attr, float(value))

        # Restore the last live grid + floor plan so the map isn't blank after a
        # restart (until the REST seed / next live_map_update refreshes them).
        grid_map_data = self._restored.get("grid_map_data")
        if isinstance(grid_map_data, dict) and grid_map_data.get("map_data"):
            snapshot.grid_map_data = grid_map_data
        floor_plan_polys = self._restored.get("floor_plan_polys")
        if isinstance(floor_plan_polys, list) and floor_plan_polys:
            snapshot.floor_plan_polys = floor_plan_polys

    def _start_paths_poll(self) -> None:
        """Start the 2-second /paths polling loop."""
        self._stop_paths_poll()
        self._paths_unsub = async_track_time_interval(
            self.hass,
            self._async_poll_paths,
            timedelta(seconds=2),
        )

    def _stop_paths_poll(self) -> None:
        """Cancel the /paths polling loop if running."""
        if self._paths_unsub is not None:
            self._paths_unsub()
            self._paths_unsub = None

    async def _async_poll_paths(self, _now: datetime) -> None:
        """Fetch incremental /paths points and append them to the live trace."""
        if not self.data:
            return
        bid = self.data.mission_bid
        if not bid or self.data.activity != "cleaning":
            self._stop_paths_poll()
            return

        result = await self.api.async_get_live_paths(bid, self._paths_next_index)
        if result is None:
            return

        data = (result.get("data") or {})
        raw_pts: list[list[float]] = data.get("history_path") or []
        if not raw_pts:
            return

        # Keep only drawn pass types; x/y are cols 0/1, type is col 4.
        DRAWN_TYPES = {48, 80, 112}
        new_pts = [
            (float(pt[0]), float(pt[1]))
            for pt in raw_pts
            if len(pt) >= 5 and int(pt[4]) in DRAWN_TYPES
        ]
        new_end: int = data.get("end_index", self._paths_next_index)
        if new_end > self._paths_next_index:
            self._paths_next_index = new_end

        if not new_pts:
            return

        # Merge into existing trajectory (cap at TRAJECTORY_MAX_POINTS as a safety net).
        prev = self.data.trajectory
        merged = list(prev[-(TRAJECTORY_MAX_POINTS - len(new_pts)):]) + new_pts
        snapshot = replace(self.data, trajectory=merged)
        self.async_set_updated_data(snapshot)
        self._schedule_trajectory_save(snapshot)

    @callback
    def _schedule_trajectory_save(self, snapshot: RomoSnapshot) -> None:
        """Persist the live map state (debounced) so it survives restarts.

        Covers the cleaning trace + robot/dock positions and the live occupancy
        grid + floor plan (both pushed via MQTT, ~19 KB, so a restart shows the last
        known map instantly instead of a blank grid until the next cloud fetch). The
        trajectory is downsampled for storage so a long session doesn't write
        thousands of points to disk on every debounced save; the live in-memory
        trace keeps full resolution. Debounced (TRAJECTORY_SAVE_DELAY), so even the
        2 Hz live_map_update stream only writes ~once per 30 s.
        """
        # Build the payload lazily inside the callback: async_delay_save is debounced,
        # so this only runs ~once per 30 s at write time instead of on every (2 Hz)
        # call — the trajectory downsample isn't recomputed on each live_map_update.
        def _payload() -> dict[str, Any]:
            return {
                "trajectory": [
                    list(p)
                    for p in _downsample(snapshot.trajectory, TRAJECTORY_STORAGE_POINTS)
                ],
                "robot_x": snapshot.robot_x,
                "robot_y": snapshot.robot_y,
                "robot_yaw": snapshot.robot_yaw,
                "dock_x": snapshot.dock_x,
                "dock_y": snapshot.dock_y,
                "grid_map_data": snapshot.grid_map_data,
                "floor_plan_polys": snapshot.floor_plan_polys,
            }

        self._store.async_delay_save(_payload, TRAJECTORY_SAVE_DELAY)

    async def async_clear_trajectory(self) -> None:
        """Clear the accumulated sweep trace and forget the persisted copy."""
        snapshot = replace(self.data) if self.data else RomoSnapshot()
        snapshot.trajectory = []
        self._paths_next_index = 0
        snapshot.last_updated = datetime.now(UTC)
        await self._store.async_remove()
        self._restored = None
        self.async_set_updated_data(snapshot)

    def property_value(self, key: str) -> Any:
        """Return a value from the cloud properties payload by leaf key (BFS)."""
        properties = self.data.cloud_data.get("properties", {}) if self.data else {}
        if not isinstance(properties, dict):
            return None
        stack: list[dict[str, Any]] = [properties]
        while stack:
            current = stack.pop()
            if key in current:
                return current[key]
            stack.extend(v for v in current.values() if isinstance(v, dict))
        return None

    async def _async_refresh_cloud_data(self, snapshot: RomoSnapshot) -> None:
        """Refresh slower REST details used by diagnostic sensors.

        Properties and jobs are volatile and fetched every cycle. Settings,
        consumables and shortcuts barely change, so they are cached and only
        refetched every ``STATIC_REFRESH_INTERVAL`` (relevant during the fast
        cleaning poll).
        """
        now = datetime.now(UTC)
        need_static = (
            self._static_cache is None
            or self._static_fetched_at is None
            or (now - self._static_fetched_at) > STATIC_REFRESH_INTERVAL
        )
        try:
            if need_static:
                (
                    properties,
                    jobs_and_total,
                    settings,
                    consumables,
                    dock_consumables,
                    consumable_alerts,
                    shortcuts,
                ) = await asyncio.gather(
                    self.api.async_get_properties(),
                    self.api.async_get_jobs_and_total(),
                    self.api.async_get_settings(),
                    self.api.async_get_consumables(),
                    self.api.async_get_dock_consumables(),
                    self.api.async_get_consumable_notifications(),
                    self.api.async_get_shortcuts(),
                )
                self._static_cache = {
                    "settings": settings,
                    "consumables": consumables,
                    "dock_consumables": dock_consumables,
                    "consumable_alerts": consumable_alerts,
                    "shortcuts": shortcuts,
                }
                self._static_fetched_at = now
            else:
                properties, jobs_and_total = await asyncio.gather(
                    self.api.async_get_properties(),
                    self.api.async_get_jobs_and_total(),
                )
                cache = self._static_cache
                settings = cache["settings"]
                consumables = cache["consumables"]
                dock_consumables = cache["dock_consumables"]
                consumable_alerts = cache["consumable_alerts"]
                shortcuts = cache["shortcuts"]
            jobs, total_cleanings = jobs_and_total
        except DjiRomoAuthError as err:
            self._async_create_auth_repair_issue(str(err))
            raise ConfigEntryAuthFailed(
                f"DJI Home authentication failed: {err}"
            ) from err
        except DjiRomoApiError as err:
            self._cloud_refresh_failures += 1
            if self._cloud_refresh_failures >= CLOUD_REFRESH_FAILURE_LIMIT:
                raise UpdateFailed(
                    "Failed to refresh DJI Romo cloud details "
                    f"{self._cloud_refresh_failures} times in a row: {err}"
                ) from err
            _LOGGER.warning(
                "Failed to refresh DJI Romo cloud details (%s/%s): %s",
                self._cloud_refresh_failures,
                CLOUD_REFRESH_FAILURE_LIMIT,
                err,
            )
            return

        self._cloud_refresh_failures = 0
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

        if total_cleanings is not None:
            snapshot.total_cleanings = total_cleanings

        last_job = jobs[0] if jobs else None
        # An active job is the newest job whose status is not a known terminal one.
        # (The running status string isn't observable while docked, so detect it by
        # exclusion rather than guessing the exact value.)
        active_job = next(
            (
                j
                for j in jobs
                if str(j.get("status", "")).lower() not in TERMINAL_JOB_STATUSES
            ),
            None,
        )
        if last_job:
            snapshot.last_job = last_job
        # Track active job separately so _current_cleaning_room can read plan_content.
        # Reset the trace whenever a new job UUID appears (new cleaning session); the
        # MQTT mission_bid change also resets it live, this covers the REST path.
        prev_active_uuid = snapshot.active_job.get("uuid") if snapshot.active_job else None
        if active_job is not None:
            if active_job.get("uuid") != prev_active_uuid:
                snapshot.trajectory = []
            snapshot.active_job = active_job
        else:
            snapshot.active_job = {}

        rooms = _rooms_from_shortcuts(shortcuts)
        if rooms:
            snapshot.rooms = rooms


        # Fetch floor plan if we don't have it yet (or once every ~12 refreshes).
        should_fetch_floor_plan = not snapshot.floor_plan_polys or (
            snapshot.cloud_last_updated is not None
            and (datetime.now(UTC) - snapshot.cloud_last_updated) > timedelta(hours=6)
        )
        if should_fetch_floor_plan:
            try:
                map_data = await self.api.async_get_map_data()
                if map_data:
                    poly_info = map_data.get("seg_map", {}).get("poly_info")
                    if poly_info:
                        snapshot.floor_plan_polys = poly_info
                    snapshot.grid_map_data = map_data.get("grid_map")
                    
                    carpet = map_data.get("carpet_layer", {})
                    if carpet and isinstance(carpet.get("data"), list):
                        snapshot.carpet_polys = carpet["data"]
                        
                    restricted = map_data.get("restricted_layer", {})
                    if restricted and isinstance(restricted.get("data"), list):
                        snapshot.restricted_polys = restricted["data"]
                        
                    vw = map_data.get("virtual_wall", {})
                    if vw and isinstance(vw.get("data"), list):
                        snapshot.virtual_walls = vw["data"]
            except Exception:  # noqa: BLE001
                pass  # Non-fatal: map overlay continues working without floor plan

        # Fetch the last *completed* cleaning's report map (rooms + grid + layers +
        # the history_path sweep trace) for the "Last Cleaning" image — only when the
        # newest finished job changes, since the room_map blob is ~650 KB.
        last_completed = next(
            (
                j
                for j in jobs
                if str(j.get("status", "")).lower() in TERMINAL_JOB_STATUSES
                and j.get("uuid")
            ),
            None,
        )
        completed_uuid = last_completed.get("uuid") if last_completed else None
        if completed_uuid and completed_uuid != self._last_clean_map_uuid:
            try:
                report_map = await self.api.async_get_job_room_map(completed_uuid)
            except Exception:  # noqa: BLE001
                report_map = None
            if report_map and report_map.get("history_path"):
                snapshot.last_clean_map = report_map
                self._last_clean_map_uuid = completed_uuid
        
        snapshot.last_clean_map_uuid = self._last_clean_map_uuid

        self._update_device_info(properties)

        flattened = _flatten_dict(properties)
        battery = _coerce_int(_pick_first(flattened, ("battery",)))
        if battery is not None:
            snapshot.battery_level = battery

        # Robot/dock pose + live dock flags (also pushed via MQTT, but seed them
        # from REST so the sensors have values before the first osd message).
        _apply_positions(snapshot, flattened)
        _apply_dock_flags(snapshot, flattened)
        snapshot.current_room = _current_cleaning_room(snapshot)

        online = _pick_first(flattened, ("online_status",))
        if isinstance(online, bool):
            snapshot.online = online
            if online:
                # The REST poll proves the robot is reachable; treat it as a
                # liveness signal so we don't immediately flag it offline.
                snapshot.last_osd_at = datetime.now(UTC)

    def _update_device_info(self, properties: dict[str, Any]) -> None:
        """Capture model/firmware/name shown on the Home Assistant device page."""
        base = properties.get("device_base_info", {})
        if not isinstance(base, dict):
            return
        version = base.get("device_version", {})
        firmware = (
            version.get("firmware_version") if isinstance(version, dict) else None
        )
        self.device_info_payload = {
            "model": base.get("device_model_type")
            or properties.get("device_model_type")
            or "Romo",
            "product_name": base.get("name"),
            "firmware": firmware,
            "dock_sn": properties.get("dock_sn"),
        }

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

        envelope = {"method": mapping} if isinstance(mapping, str) else dict(mapping)

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

    async def async_clean_rooms_by_name(self, names: list[str]) -> list[str]:
        """Start a multi-room clean for the given room names.

        Returns the list of names that were not found so the caller can report
        them. Room settings come from the shared HA cleaning options.
        """
        try:
            shortcuts = await self.api.async_get_shortcuts()
        except DjiRomoAuthError as err:
            self._async_create_auth_repair_issue(str(err))
            raise UpdateFailed(f"Failed to list DJI Romo rooms: {err}") from err
        except DjiRomoApiError as err:
            raise UpdateFailed(f"Failed to list DJI Romo rooms: {err}") from err

        catalog = list(room_configs_from_shortcuts(shortcuts))
        by_name: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        room_map: dict[str, Any] = {}
        for config, r_map, duplicate_labels in catalog:
            room_map = r_map
            by_name[room_name(config, duplicate_labels).casefold()] = (config, r_map)

        selected: list[dict[str, Any]] = []
        ordered_names: list[str] = []
        missing: list[str] = []
        for name in names:
            match = by_name.get(name.strip().casefold())
            if match is None:
                missing.append(name)
                continue
            selected.append(self.room_cleaning_config(match[0]))
            ordered_names.append(name.strip())

        if not selected:
            raise UpdateFailed(
                f"None of the requested rooms were found: {', '.join(names)}"
            )

        try:
            await self.api.async_start_rooms(
                selected, room_map, " + ".join(ordered_names)
            )
        except DjiRomoAuthError as err:
            self._async_create_auth_repair_issue(str(err))
            raise UpdateFailed(f"Failed to start DJI Romo rooms: {err}") from err
        except DjiRomoApiError as err:
            raise UpdateFailed(f"Failed to start DJI Romo rooms: {err}") from err

        return missing

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
                with suppress(TypeError, ValueError):
                    options[key] = int(value)
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
        self.async_set_updated_data(replace(self.data) if self.data else RomoSnapshot())

    async def async_set_device_setting(
        self, build_param: Callable[[], dict[str, Any]]
    ) -> None:
        """Write device settings (PUT settings) and reflect them locally.

        ``build_param`` constructs the ``param`` body from the current snapshot; it
        is called *inside* the write lock (and after the previous write's optimistic
        patch) so two switches sharing one nested object (e.g. add_cleaner_auto)
        merge correctly instead of clobbering each other under concurrent toggles.

        Settings are REST-only (never in MQTT) and cached for STATIC_REFRESH_INTERVAL,
        so after a successful write we patch the cached + live settings optimistically
        (the entity flips immediately) and keep the static cache in sync so the next
        poll does not revert the value before the cloud reports it back.
        """
        async with self._settings_write_lock:
            param = build_param()
            try:
                await self.api.async_set_settings(param)
            except DjiRomoAuthError as err:
                self._async_create_auth_repair_issue(str(err))
                raise UpdateFailed(f"Failed to write DJI Romo setting: {err}") from err
            except DjiRomoApiError as err:
                raise UpdateFailed(f"Failed to write DJI Romo setting: {err}") from err

            if self.data is None:
                return
            settings = {**self.data.cloud_data.get("settings", {}), **param}
            if self._static_cache is not None:
                self._static_cache["settings"] = settings
            new_cloud = {**self.data.cloud_data, "settings": settings}
            self.async_set_updated_data(replace(self.data, cloud_data=new_cloud))

    async def async_set_voice_language(self, lang_code: str) -> None:
        """Switch the robot's voice language (asynchronous voicepack download).

        Unlike settings writes this is a module upgrade, so there is no optimistic
        patch: ``device_language`` only changes once the robot installs the pack.
        We invalidate the static cache and request a refresh so the select catches
        up on the next poll(s) as the new language is reported.
        """
        try:
            await self.api.async_set_voice_language(lang_code)
        except DjiRomoAuthError as err:
            self._async_create_auth_repair_issue(str(err))
            raise UpdateFailed(
                f"Failed to set DJI Romo voice language: {err}"
            ) from err
        except DjiRomoApiError as err:
            raise UpdateFailed(
                f"Failed to set DJI Romo voice language: {err}"
            ) from err
        self._static_fetched_at = None
        await self.async_request_refresh()

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

    def _mqtt_credentials_expired(self) -> bool:
        """Return True when cached MQTT credentials should be refreshed."""
        creds = self._mqtt_credentials
        if creds is None:
            return True
        now = datetime.now(UTC)
        if creds.expires_at is not None:
            # Trust the cloud-provided expiry, refreshing before it lapses.
            return now >= creds.expires_at - MQTT_CREDENTIAL_REFRESH_MARGIN
        # Fall back to the assumed lifetime if the cloud omits an expiry.
        return creds.fetched_at <= (
            now - MQTT_CREDENTIAL_ASSUMED_LIFETIME + MQTT_CREDENTIAL_REFRESH_MARGIN
        )

    async def _async_ensure_mqtt(self) -> None:
        """Refresh MQTT credentials before expiry and maintain the connection."""
        if self._mqtt_credentials_expired():
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

        try:
            await self._mqtt.async_connect(
                self._mqtt_credentials,
                self.subscription_topics,
            )
        except DjiRomoMqttError as err:
            raise UpdateFailed(f"Failed to connect to DJI Romo MQTT: {err}") from err

    @callback
    def _async_check_availability(self, _now: datetime) -> None:
        """Flag offline-by-silence and recover sustained or zombie MQTT outages.

        Transient broker drops are left to paho's built-in auto-reconnect (same
        client, re-subscribes on connect) so they stay invisible. We force a
        credential refresh + rebuild only when either:
        - the session has been down for several consecutive checks (an expired
          broker password rather than a normal recycle), or
        - the session is up but has received no message for ``MQTT_STALE_AFTER``
          (a "zombie" link the socket-level reconnect can't detect).
        """
        stale_since = self._mqtt.stale_since(MQTT_STALE_AFTER)
        if not self._mqtt.is_connected:
            self._mqtt_down_checks += 1
        else:
            self._mqtt_down_checks = 0

        if self.data is not None and self.data.last_osd_at is not None:
            silent_for = datetime.now(UTC) - self.data.last_osd_at
            if silent_for > OFFLINE_AFTER and self.data.online:
                self.data.online = False
                self.async_update_listeners()

        if not self._mqtt_recovering and (
            self._mqtt_down_checks >= 3 or stale_since is not None
        ):
            if stale_since is not None:
                _LOGGER.warning(
                    "DJI Romo MQTT stream silent since %s; rebuilding the session",
                    stale_since.isoformat(),
                )
            self._mqtt_recovering = True
            self.hass.async_create_task(
                self._async_recover_mqtt(stale=stale_since is not None)
            )

    async def _async_recover_mqtt(self, stale: bool = False) -> None:
        """Refresh credentials and rebuild the session after a sustained/zombie outage."""
        try:
            if stale:
                # The socket looks connected but no data flows; tear it down and drop
                # credentials so _async_ensure_mqtt actually rebuilds (async_connect
                # would otherwise no-op because creds/subscriptions still match).
                await self._mqtt.async_disconnect()
                self._mqtt_credentials = None
            await self._async_ensure_mqtt()
        except (UpdateFailed, ConfigEntryAuthFailed) as err:
            _LOGGER.debug("DJI Romo MQTT recovery attempt failed: %s", err)
        finally:
            self._mqtt_recovering = False
            self._mqtt_down_checks = 0
            self._mqtt_down_checks = 0

    @property
    def available(self) -> bool:
        """Return whether the robot is currently reachable."""
        return bool(self.last_update_success and self.data and self.data.online)

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
        previous = self.data or RomoSnapshot()
        topic_kind = _topic_kind(topic)

        # Health-management alerts ride on the events topic and only update the
        # alert list / fire an event; they never carry osd state, so handle them
        # on their own and stop before the osd-parsing branch.
        if (
            topic_kind == "events"
            and isinstance(payload, dict)
            and str(payload.get("method")) == "hms"
        ):
            self._handle_hms_event(previous, payload)
            return

        # live_map_update events carry a complete seg_map.poly_info which lets us
        # keep the floor plan fresh at ~2 Hz during cleaning without waiting for
        # the periodic REST floor plan fetch.
        if (
            topic_kind == "events"
            and isinstance(payload, dict)
            and str(payload.get("method")) == "live_map_update"
        ):
            self._handle_live_map_update(previous, payload)
            return

        # drying_progress events report the dock drying the dust box / mop pads,
        # with a percentage and an estimated remaining time. They carry no osd
        # state, so handle them on their own like hms/live_map_update.
        if (
            topic_kind == "events"
            and isinstance(payload, dict)
            and str(payload.get("method")) == "drying_progress"
        ):
            self._handle_drying_progress(previous, payload)
            return

        # A shallow copy keeps cloud_data/last_job/hms_alerts shared by reference
        # (this handler never mutates them) instead of deep-copying a large dict
        # roughly once per second.
        snapshot = replace(previous)
        snapshot.selected_topic = topic

        if isinstance(payload, dict):
            snapshot.last_osd_at = datetime.now(UTC)
            snapshot.online = True
            flattened = _flatten_dict(payload)
            _apply_positions(snapshot, flattened)
            _apply_dock_flags(snapshot, flattened)

            # A new mission_bid means a new cleaning session: clear the previous
            # trace so the map shows only the current run (matching the DJI app).
            new_bid = _pick_first(flattened, ("mission_bid",))
            new_bid = str(new_bid) if new_bid else None
            if new_bid and new_bid != "0" and new_bid != previous.mission_bid:
                snapshot.trajectory = []
                self._paths_next_index = 0
                self._stop_paths_poll()

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
                    bid_str = str(mission_bid) or None
                    if bid_str and bid_str != "0":
                        snapshot.mission_bid = bid_str
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
                candidate_activity = _infer_property_activity(
                    flattened,
                    snapshot.status_text,
                    previous.activity,
                )
                snapshot.activity = self._stable_activity(
                    previous.activity,
                    candidate_activity,
                    source="property",
                )
                # Clear the live "current clean" figures once the run is over.
                if snapshot.activity in {"docked", "idle"}:
                    snapshot.clean_progress = None
                    snapshot.clean_duration_s = None
                    snapshot.clean_remaining_s = None
                    snapshot.cleaned_area = None
                    snapshot.active_poly_index = None
                    snapshot.active_step = None
            elif topic_kind == "events":
                if str(payload.get("method")) == "room_clean_progress":
                    # Live progress figures for the current job.
                    percent = _coerce_int(_pick_first(flattened, ("percent",)))
                    if percent is not None:
                        snapshot.clean_progress = percent
                    acreage = _coerce_float(_pick_first(flattened, ("cleaned_acreage",)))
                    if acreage is not None:
                        snapshot.cleaned_area = acreage
                    duration = _coerce_int(_pick_first(flattened, ("job_duration",)))
                    if duration is not None:
                        snapshot.clean_duration_s = duration
                    remaining = _coerce_int(
                        _pick_first(flattened, ("estimate_remain_time",))
                    )
                    if remaining is not None:
                        snapshot.clean_remaining_s = remaining
                    # The room actually being cleaned right now (authoritative, no
                    # dependency on the REST job plan/step).
                    poly = _coerce_int(_pick_first(flattened, ("current_poly_index",)))
                    if poly is not None:
                        snapshot.active_poly_index = poly
                event_activity = _infer_event_activity(flattened, previous.activity)
                if event_activity is not None:
                    snapshot.activity = self._stable_activity(
                        previous.activity,
                        event_activity,
                        source="events",
                    )
                # Capture the real-time step from MQTT so current_room can update
                # immediately instead of waiting for the 60s REST poll.
                step = _coerce_int(_pick_first(flattened, ("current_step",)))
                if step is not None:
                    snapshot.active_step = step

            # Recompute the current cleaning room from the latest activity/step on
            # every osd message. _current_cleaning_room is gated on activity, so a
            # property update that ends the clean (e.g. cleaning -> docked) clears
            # it right away instead of leaving a stale room until the next poll.
            snapshot.current_room = _current_cleaning_room(snapshot)
        else:
            snapshot.status_text = str(payload)
            candidate_activity = _infer_property_activity(
                {}, snapshot.status_text, previous.activity
            )
            snapshot.activity = self._stable_activity(
                previous.activity,
                candidate_activity,
                source="other",
            )

        if snapshot.activity == "cleaning" and snapshot.mission_bid and self._paths_unsub is None:
            self._start_paths_poll()
        elif snapshot.activity in {"docked", "idle", "error"} and self._paths_unsub is not None:
            self._stop_paths_poll()

        if not _meaningful_state_changed(previous, snapshot):
            return

        snapshot.last_updated = datetime.now(UTC)
        self.async_set_updated_data(snapshot)

    def _handle_hms_event(
        self,
        previous: RomoSnapshot,
        payload: dict[str, Any],
    ) -> None:
        """Store the latest HMS alert list and fire an event on new alerts."""
        data = payload.get("data", {})
        alerts = data.get("list", []) if isinstance(data, dict) else []
        if not isinstance(alerts, list):
            alerts = []

        now = datetime.now(UTC)
        # An events message still proves the robot is reachable.
        snapshot = replace(previous, online=True, last_osd_at=now)
        if alerts != previous.hms_alerts:
            snapshot.hms_alerts = alerts
            snapshot.last_updated = now
            if alerts:
                self.hass.bus.async_fire(
                    EVENT_HMS,
                    {"device_sn": self.device_sn, "alerts": alerts},
                )
        self.async_set_updated_data(snapshot)

    def _handle_drying_progress(
        self,
        previous: RomoSnapshot,
        payload: dict[str, Any],
    ) -> None:
        """Update dock drying state/percentage/remaining time from a drying_progress event."""
        now = datetime.now(UTC)
        snapshot = replace(previous, online=True, last_osd_at=now)
        data = payload.get("data", {}) if isinstance(payload.get("data"), dict) else {}

        if str(data.get("status", "")).lower() == "in_progress":
            snapshot.drying_active = True
            stage = data.get("sub_job_status", {}).get("cur_submission")
            snapshot.drying_stage = str(stage) if stage else None
            percent = _coerce_int(data.get("progress", {}).get("percent"))
            snapshot.drying_percent = percent
            remaining = _coerce_int(
                data.get("duration", {}).get("estimated_remaining_duration")
            )
            snapshot.drying_remaining_s = remaining if (remaining or 0) >= 0 else None
        else:
            # Drying finished or stopped: clear the live fields.
            snapshot.drying_active = False
            snapshot.drying_stage = None
            snapshot.drying_percent = None
            snapshot.drying_remaining_s = None

        snapshot.last_updated = now
        self.async_set_updated_data(snapshot)

    def _handle_live_map_update(
        self,
        previous: RomoSnapshot,
        payload: dict[str, Any],
    ) -> None:
        """Update the floor plan polygons from a live_map_update MQTT event.

        DJI pushes these at ~2 Hz during cleaning.  We pull the seg_map poly_info
        so the floor plan SVG stays accurate even if the SLAM map is refined
        mid-session.  The message also proves the robot is reachable.
        """
        now = datetime.now(UTC)
        snapshot = replace(previous, online=True, last_osd_at=now)

        map_data = payload.get("data", {}).get("map_data", {})

        seg_map = map_data.get("seg_map", {})
        poly_info = seg_map.get("poly_info")
        if isinstance(poly_info, list) and poly_info:
            snapshot.floor_plan_polys = poly_info

        obstacle_layer = map_data.get("obstacle_layer", {})
        obs_data = obstacle_layer.get("data")
        if isinstance(obs_data, list):
            pts: list[tuple[float, float]] = []
            for item in obs_data:
                verts = item.get("vertices") or []
                if not verts and "position" in item:
                    verts = [item["position"]]
                for v in verts:
                    x = v.get("x")
                    y = v.get("y")
                    if x is not None and y is not None:
                        pts.append((float(x), float(y)))
            if pts:
                snapshot.obstacles = pts

        # Occupancy grid (walls + scanned objects/furniture). DJI pushes the full
        # grid here at ~2 Hz, in the same format decode_grid_cells/image.py already
        # render, so we keep it current — this is what makes the grid grow live like
        # the app. (We still do NOT use grid_map as the cleaning *trace*: it is the
        # cumulative SLAM coverage and doesn't match the app's per-session sweep; the
        # trace is built from the live /paths sweep instead.)
        grid_map = map_data.get("grid_map")
        if isinstance(grid_map, dict) and grid_map.get("map_data"):
            snapshot.grid_map_data = grid_map

        self.async_set_updated_data(snapshot)
        # Persist the live floor plan + grid (debounced) so they survive a restart.
        self._schedule_trajectory_save(snapshot)

    def _stable_activity(
        self,
        previous_activity: str,
        candidate_activity: str,
        *,
        source: str,
    ) -> str:
        """Avoid publishing short-lived activity flips from mixed MQTT sources."""
        now = datetime.now(UTC)

        if source == "events" and candidate_activity in {"paused", "returning"}:
            self._held_activity = candidate_activity
            self._activity_hold_until = now + ACTIVITY_HOLD_DURATION

        if (
            self._held_activity
            and self._activity_hold_until
            and now < self._activity_hold_until
        ):
            if candidate_activity == self._held_activity:
                self._pending_activity = None
                self._pending_activity_count = 0
            elif source == "property" and candidate_activity in {"docked", "error"}:
                self._held_activity = None
                self._activity_hold_until = None
            else:
                return self._held_activity

        if candidate_activity == previous_activity:
            self._pending_activity = None
            self._pending_activity_count = 0
            return candidate_activity

        if candidate_activity in {"docked", "error"}:
            self._pending_activity = None
            self._pending_activity_count = 0
            self._held_activity = None
            self._activity_hold_until = None
            return candidate_activity

        if candidate_activity == self._pending_activity:
            self._pending_activity_count += 1
        else:
            self._pending_activity = candidate_activity
            self._pending_activity_count = 1

        if self._pending_activity_count >= ACTIVITY_CONFIRMATION_COUNT:
            self._pending_activity = None
            self._pending_activity_count = 0
            return candidate_activity

        return previous_activity


def _meaningful_state_changed(previous: RomoSnapshot, current: RomoSnapshot) -> bool:
    """Return True when a meaningful entity state changed."""
    return any(
        getattr(previous, key) != getattr(current, key)
        for key in MEANINGFUL_STATE_KEYS
    )


def _downsample(
    points: list[tuple[float, float]],
    max_points: int,
) -> list[tuple[float, float]]:
    """Return at most ``max_points`` evenly-strided points, keeping the last one."""
    n = len(points)
    if n <= max_points or max_points < 2:
        return list(points)
    step = n / max_points
    sampled = [points[int(i * step)] for i in range(max_points)]
    sampled[-1] = points[-1]  # always keep the most recent position
    return sampled


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


def _apply_positions(snapshot: RomoSnapshot, flattened: dict[str, Any]) -> None:
    """Pull robot/dock pose from an osd or properties payload.

    There are two ``px``/``py`` (robot and dock), so we read each pose dict by
    its parent key rather than the ambiguous leaf name.
    """
    robot = _pick_first(flattened, ("robot_position",))
    if isinstance(robot, dict):
        x = _coerce_float(robot.get("px"))
        y = _coerce_float(robot.get("py"))
        if x is not None:
            snapshot.robot_x = round(x, 3)
        if y is not None:
            snapshot.robot_y = round(y, 3)
        yaw = _yaw_degrees(robot)
        if yaw is not None:
            snapshot.robot_yaw = yaw

    dock = _pick_first(flattened, ("dock_position",))
    if isinstance(dock, dict):
        x = _coerce_float(dock.get("px"))
        y = _coerce_float(dock.get("py"))
        if x is not None:
            snapshot.dock_x = round(x, 3)
        if y is not None:
            snapshot.dock_y = round(y, 3)


def _apply_dock_flags(snapshot: RomoSnapshot, flattened: dict[str, Any]) -> None:
    """Pull live dock/robot flags from an osd or properties payload.

    These are present in the device_osd stream (so binary sensors update in ~1 s)
    and also in REST properties (used to seed before the first osd).
    """
    charger = _coerce_int(_pick_first(flattened, ("charger_connected",)))
    if charger is not None:
        snapshot.charger_connected = charger
    battery_care = _coerce_int(_pick_first(flattened, ("battery_care_active",)))
    if battery_care is not None:
        snapshot.battery_care_active = battery_care
    uv = _pick_first(flattened, ("dust_bag_uv_enable",))
    if isinstance(uv, bool):
        snapshot.dust_bag_uv_enable = uv
    hatch = _coerce_int(_pick_first(flattened, ("hatch_status",)))
    if hatch is not None:
        snapshot.hatch_status = hatch


def _yaw_degrees(pose: dict[str, Any]) -> float | None:
    """Convert a DJI pose quaternion (qw, qz about vertical) to a heading."""
    qw = _coerce_float(pose.get("qw"))
    qz = _coerce_float(pose.get("qz"))
    if qw is None or qz is None:
        return None
    from math import atan2, degrees

    return round(degrees(2 * atan2(qz, qw)) % 360, 1)


def _rooms_from_shortcuts(shortcuts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a name/area list of rooms from the most complete shortcut map.

    Names are disambiguated the same way as the per-room buttons (shared rooms.py
    helpers), so duplicate room types read "Bathroom1"/"Bathroom2" everywhere.
    """
    if not shortcuts:
        return []
    template = max(
        shortcuts,
        key=lambda s: len(s.get("room_map", {}).get("device_map_rooms", [])),
    )
    device_rooms = template.get("room_map", {}).get("device_map_rooms", [])
    duplicate_labels = duplicate_label_ids(device_rooms)
    rooms: list[dict[str, Any]] = []
    for room in sorted(device_rooms, key=lambda r: r.get("order_id", 999)):
        rooms.append(
            {
                "poly_index": room.get("poly_index"),
                "name": room_name(room, duplicate_labels),
                "area": round(_coerce_float(room.get("poly_area")) or 0.0, 2),
                "order_id": room.get("order_id"),
            }
        )
    return rooms


def _current_cleaning_room(snapshot: RomoSnapshot) -> str | None:
    """Best-effort: the room the robot is cleaning.

    Geometric "which room is the robot in" needs the (encrypted) map polygons.

    Sources in priority order:
    1. snapshot.active_poly_index — the poly the robot is actually cleaning right
       now, from the live room_clean_progress event. This is authoritative and does
       NOT depend on the REST job list (an app-started room clean often never shows
       up there, which previously made us read a stale job's plan → wrong room).
    2. Fallback: the active/last job plan ordered list + the current step
       (0-indexed in DJI's API, 1-indexed handled too).

    Gated on ``activity`` (from the MQTT ``mission_status``), a reliable cleaning
    signal, rather than the REST job status string (not observable while docked).
    """
    if snapshot.activity not in {"cleaning", "paused"}:
        return None
    # Preferred: the live poly being cleaned (room_clean_progress).
    if snapshot.active_poly_index is not None:
        for room in snapshot.rooms:
            if room.get("poly_index") == snapshot.active_poly_index:
                return room.get("name")
    job = snapshot.active_job or snapshot.last_job
    configs = job.get("plan_content", {}).get("plan_area_configs", [])
    if not configs:
        return None
    step = snapshot.active_step
    if step is None:
        step = _coerce_int(job.get("progress", {}).get("current_step"))
    if step is None:
        return None
    # DJI uses 0-indexed steps (first room = 0); accept 1-indexed as fallback.
    if 0 <= step < len(configs):
        idx = step
    elif 1 <= step <= len(configs):
        idx = step - 1
    else:
        return None
    poly_index = configs[idx].get("poly_index")
    for room in snapshot.rooms:
        if room.get("poly_index") == poly_index:
            return room.get("name")
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
