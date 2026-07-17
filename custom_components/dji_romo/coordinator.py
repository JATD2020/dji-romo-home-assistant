"""State coordinator for DJI Romo."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .activity import ActivityFilter
from .client import (
    DjiMqttCredentials,
    DjiRomoApiClient,
    DjiRomoApiError,
    DjiRomoAuthError,
)
from .const import (
    AVAILABILITY_CHECK_INTERVAL,
    CLEAN_PASS_TYPES,
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
    DEFAULT_COMMAND_TOPIC,
    DEFAULT_SUBSCRIPTION_TOPICS,
    DOMAIN,
    EVENT_HMS,
    MQTT_CREDENTIAL_ASSUMED_LIFETIME,
    MQTT_CREDENTIAL_REFRESH_MARGIN,
    MQTT_STALE_AFTER,
    OFFLINE_AFTER,
    STATIC_REFRESH_INTERVAL,
    TERMINAL_JOB_STATUSES,
    TRAJECTORY_MAX_POINTS,
    TRAJECTORY_SAVE_DELAY,
    TRAJECTORY_STORAGE_KEY,
    TRAJECTORY_STORAGE_POINTS,
    TRAJECTORY_STORAGE_VERSION,
)
from .mqtt import DjiRomoMqttAuthError, DjiRomoMqttClient, DjiRomoMqttError
from .rooms import duplicate_label_ids, room_configs_from_shortcuts, room_name
from .validation import (
    format_mqtt_topic,
    validate_command_mapping,
    validate_subscription_topics,
)

_LOGGER = logging.getLogger(__name__)
# Typed config entry carrying the coordinator in runtime_data (PEP 695 lazy alias,
# so the forward reference to the class below resolves fine).
type DjiRomoConfigEntry = ConfigEntry[DjiRomoCoordinator]
AUTH_REPAIR_ISSUE_ID = "auth_failed"
PATH_PAGE_LIMIT = 25
MAP_PUSH_MIN_INTERVAL = timedelta(seconds=2)
POSITION_UPDATE_THRESHOLD = 0.02
YAW_UPDATE_THRESHOLD = 2.0
DEFAULT_ROOM_CLEANING_OPTIONS = {
    CONF_ROOM_CLEAN_MODE: 2,
    CONF_ROOM_FAN_SPEED: 3,
    CONF_ROOM_WATER_LEVEL: 2,
    CONF_ROOM_CLEAN_NUM: 1,
    CONF_ROOM_CLEAN_SPEED: 0,
}
MEANINGFUL_STATE_KEYS = (
    "battery_level",
    "activity",
    "mission_bid",
    "cleaned_area",
    "fan_speed",
    "clean_mode",
    "water_level",
    "clean_num",
    "clean_speed",
    "online",
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
LIVE_SEEDED_FIELDS = frozenset(
    {
        "battery_level",
        "robot_x",
        "robot_y",
        "robot_yaw",
        "dock_x",
        "dock_y",
        "charger_connected",
        "battery_care_active",
        "dust_bag_uv_enable",
        "hatch_status",
    }
)


@dataclass(slots=True)
class RomoSnapshot:
    """Current best-effort picture of the robot state."""

    battery_level: int | None = None
    activity: str = "idle"
    status_text: str | None = None
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
            # Push updates reset a coordinator's built-in poll timer. DJI sends
            # roughly one MQTT message per second, so REST polling uses a separate
            # timer that push traffic cannot postpone indefinitely.
            update_interval=None,
            always_update=False,
        )
        self.entry = entry
        self.api = api
        self.device_sn: str = entry.data[CONF_DEVICE_SN]
        self.device_name: str = entry.data[CONF_DEVICE_NAME]
        self.device_info_payload: dict[str, Any] = {}
        self.shortcuts: list[dict[str, Any]] = []
        # Cache rarely-changing REST data (settings/consumables/shortcuts).
        self._static_cache: dict[str, Any] | None = None
        self._static_fetched_at: datetime | None = None
        self._map_overlays_fetched_at: datetime | None = None
        self._map_index: int | None = None
        self._map_version: int | None = None
        self._map_refresh_task: asyncio.Task[None] | None = None
        self._pending_current_map_refresh = False
        self._pending_report_uuid: str | None = None
        self._current_map_refresh_inflight = False
        self._report_refresh_inflight_uuid: str | None = None
        # UUID of the completed job whose report map is cached in the snapshot, so we
        # only refetch the (large) room_map when a newer finished job appears.
        self._last_clean_map_uuid: str | None = None
        self._trace_session_bid: str | None = None
        self._mqtt_credentials: DjiMqttCredentials | None = None
        # No connection-lost callback: paho auto-reconnects transient broker
        # drops seamlessly with the same client (and re-subscribes via on_connect).
        # We only step in for sustained outages, from the availability timer.
        self._mqtt = DjiRomoMqttClient(hass.loop, self._handle_mqtt_message)
        self._mqtt_connect_lock = asyncio.Lock()
        self._sent_bids: deque[str] = deque(maxlen=32)
        self._mqtt_down_checks = 0
        self._mqtt_recovering = False
        self._mqtt_recovery_task: asyncio.Task[None] | None = None
        self._shutting_down = False
        self._last_mqtt_message_at: datetime | None = None
        self._last_map_dispatch_at: datetime | None = None
        # Consecutive REST refresh failures; entities go unavailable past the limit.
        self._cloud_refresh_failures = 0
        self._cloud_healthy = True
        self._auth_failed = False
        self._last_cloud_success_at: datetime | None = None
        # Serializes settings writes so two switches sharing one nested object
        # (e.g. add_cleaner_auto) can't clobber each other: the param is built
        # under this lock, after the previous write's optimistic patch landed.
        self._settings_write_lock = asyncio.Lock()
        self._availability_unsub: CALLBACK_TYPE | None = None
        self._rest_poll_unsub: CALLBACK_TYPE | None = None
        self._activity_filter = ActivityFilter()
        self._paths_unsub: CALLBACK_TYPE | None = None
        self._paths_polling = False
        self._paths_backfilled = False
        self._paths_next_index: int = 0
        self._last_trajectory_write: datetime | None = None
        # Persisted trajectory so the live map survives a Home Assistant restart.
        self._store: Store[dict[str, Any]] = Store(
            hass,
            TRAJECTORY_STORAGE_VERSION,
            f"{TRAJECTORY_STORAGE_KEY}_{self.device_sn}",
        )
        self._restored: dict[str, Any] | None = None

    async def _async_setup(self) -> None:
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
        self._rest_poll_unsub = async_track_time_interval(
            self.hass,
            self._async_rest_poll_tick,
            COORDINATOR_REFRESH_INTERVAL,
        )

    async def _async_update_data(self) -> RomoSnapshot:
        """Refresh cloud metadata and keep the MQTT session healthy."""
        await self._async_ensure_mqtt()
        self.device_name = (
            self.entry.options.get(CONF_DEVICE_NAME)
            or self.entry.data[CONF_DEVICE_NAME]
        )

        base = self.data
        if base is not None:
            snapshot = replace(base)
        else:
            snapshot = RomoSnapshot()
            self._seed_from_restore(snapshot)
        await self._async_refresh_cloud_data(snapshot)

        latest = self.data
        if base is None or latest is base:
            return snapshot
        return _rebase_rest_fields(base, snapshot, latest)

    async def _async_rest_poll_tick(self, _now: datetime) -> None:
        """Poll REST independently from the MQTT-driven coordinator updates."""
        await self.async_request_refresh()

    async def async_shutdown(self) -> None:
        """Stop MQTT alongside coordinator shutdown."""
        self._shutting_down = True
        if self._availability_unsub is not None:
            self._availability_unsub()
            self._availability_unsub = None
        if self._rest_poll_unsub is not None:
            self._rest_poll_unsub()
            self._rest_poll_unsub = None
        if self._map_refresh_task is not None:
            self._map_refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._map_refresh_task
            self._map_refresh_task = None
        if self._mqtt_recovery_task is not None:
            self._mqtt_recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._mqtt_recovery_task
            self._mqtt_recovery_task = None
        self._stop_paths_poll()
        await self._mqtt.async_disconnect()
        await super().async_shutdown()

    def _seed_from_restore(self, snapshot: RomoSnapshot) -> None:
        """Seed the first snapshot from the persisted trajectory/positions."""
        if not self._restored:
            return
        restored_index = _coerce_int(self._restored.get("map_index"))
        if self._map_index is not None and restored_index != self._map_index:
            return
        if self._map_index is None:
            self._map_index = restored_index
            self._map_version = _coerce_int(self._restored.get("map_version"))
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
        for attr in ("carpet_polys", "restricted_polys", "virtual_walls"):
            value = self._restored.get(attr)
            if isinstance(value, list):
                setattr(snapshot, attr, value)
        obstacles = self._restored.get("obstacles")
        if isinstance(obstacles, list):
            snapshot.obstacles = [
                (float(point[0]), float(point[1]))
                for point in obstacles
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ]
        trace_session_bid = self._restored.get("trace_session_bid")
        if isinstance(trace_session_bid, str):
            self._trace_session_bid = trace_session_bid

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
        """Fetch available /paths pages without overlapping timer ticks."""
        if not self.data:
            return
        bid = self.data.mission_bid
        if not bid or self.data.activity != "cleaning":
            self._stop_paths_poll()
            return
        if self._paths_polling:
            return
        self._paths_polling = True
        try:
            await self._async_drain_paths(bid)
        finally:
            self._paths_polling = False

    async def _async_drain_paths(self, bid: str) -> None:
        """Drain a bounded number of path pages and merge them into the trace."""
        rebuild = not self._paths_backfilled
        if rebuild:
            self._paths_next_index = 0

        new_pts: list[tuple[float, float]] = []
        received_page = False
        for _ in range(PATH_PAGE_LIMIT):
            result = await self.api.async_get_live_paths(bid, self._paths_next_index)
            if (
                self.data is None
                or self.data.activity != "cleaning"
                or self.data.mission_bid != bid
            ):
                return
            if result is None:
                break
            data = result.get("data") or {}
            if not isinstance(data, dict):
                break
            received_page = True
            history_path = data.get("history_path")
            if not isinstance(history_path, list):
                history_path = []
            for point in history_path:
                try:
                    if len(point) >= 5 and int(point[4]) in CLEAN_PASS_TYPES:
                        new_pts.append((float(point[0]), float(point[1])))
                except (TypeError, ValueError):
                    continue
            new_end = _coerce_int(data.get("end_index"))
            advanced = new_end is not None and new_end > self._paths_next_index
            if advanced:
                self._paths_next_index = new_end
            remained = _coerce_int(data.get("num_remained_points")) or 0
            if remained <= 0 or not advanced:
                break

        if rebuild and received_page:
            self._paths_backfilled = True
        if not new_pts:
            return

        if (
            self.data is None
            or self.data.activity != "cleaning"
            or self.data.mission_bid != bid
        ):
            return
        previous = [] if rebuild else self.data.trajectory
        new_pts = new_pts[-TRAJECTORY_MAX_POINTS:]
        keep = TRAJECTORY_MAX_POINTS - len(new_pts)
        merged = (list(previous[-keep:]) if keep > 0 else []) + new_pts
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
                "carpet_polys": snapshot.carpet_polys,
                "restricted_polys": snapshot.restricted_polys,
                "virtual_walls": snapshot.virtual_walls,
                "obstacles": [list(point) for point in snapshot.obstacles],
                "trace_session_bid": self._trace_session_bid,
                "map_index": self._map_index,
                "map_version": self._map_version,
            }

        now = datetime.now(UTC)
        if self._last_trajectory_write is None or (
            now - self._last_trajectory_write
        ) >= timedelta(seconds=TRAJECTORY_SAVE_DELAY):
            self._last_trajectory_write = now
            self.hass.async_create_task(
                self._store.async_save(_payload()),
                f"{DOMAIN} persist map state",
            )
        else:
            self._store.async_delay_save(_payload, TRAJECTORY_SAVE_DELAY)

    def _reset_trace_for_session(
        self,
        snapshot: RomoSnapshot,
        mission_bid: str | None,
    ) -> bool:
        """Clear the live trace exactly once for each cleaning mission."""
        bid = str(mission_bid) if mission_bid else None
        if not bid or bid == "0" or bid == self._trace_session_bid:
            return False
        snapshot.trajectory = []
        snapshot.obstacles = []
        self._paths_next_index = 0
        self._paths_backfilled = False
        self._trace_session_bid = bid
        return True

    async def async_clear_trajectory(self) -> None:
        """Clear the accumulated sweep trace and forget the persisted copy."""
        snapshot = replace(self.data) if self.data else RomoSnapshot()
        snapshot.trajectory = []
        snapshot.obstacles = []
        self._paths_next_index = 0
        self._paths_backfilled = False
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
        refetched every ``STATIC_REFRESH_INTERVAL``.
        """
        now = datetime.now(UTC)
        need_static = (
            self._static_cache is None
            or self._static_fetched_at is None
            or (now - self._static_fetched_at) > STATIC_REFRESH_INTERVAL
        )
        map_meta: dict[str, Any] | None = None
        try:
            if need_static:
                previous = self._static_cache or {}
                map_meta_request = (
                    [
                        _async_optional_api_call(
                            "current map metadata",
                            self.api.async_get_current_map_meta,
                            None,
                        )
                    ]
                    if snapshot.activity != "cleaning"
                    else []
                )
                (
                    properties,
                    jobs_and_total,
                    settings,
                    consumables,
                    dock_consumables,
                    consumable_alerts,
                    shortcuts,
                    cleaning_statistics,
                    *map_meta_result,
                ) = await asyncio.gather(
                    self.api.async_get_properties(),
                    self.api.async_get_jobs_and_total(),
                    _async_optional_api_call(
                        "settings",
                        self.api.async_get_settings,
                        previous.get("settings", {}),
                    ),
                    _async_optional_api_call(
                        "consumables",
                        self.api.async_get_consumables,
                        previous.get("consumables", []),
                    ),
                    _async_optional_api_call(
                        "dock consumables",
                        self.api.async_get_dock_consumables,
                        previous.get("dock_consumables", {}),
                    ),
                    _async_optional_api_call(
                        "consumable alerts",
                        self.api.async_get_consumable_notifications,
                        previous.get("consumable_alerts", []),
                    ),
                    _async_optional_api_call(
                        "cleaning presets",
                        self.api.async_get_shortcuts,
                        previous.get("shortcuts", []),
                    ),
                    _async_optional_api_call(
                        "cleaning statistics",
                        self.api.async_get_cleaning_statistics,
                        previous.get("cleaning_statistics", {}),
                    ),
                    *map_meta_request,
                )
                map_meta = map_meta_result[0] if map_meta_result else None
                self._static_cache = {
                    "settings": settings,
                    "consumables": consumables,
                    "dock_consumables": dock_consumables,
                    "consumable_alerts": consumable_alerts,
                    "shortcuts": shortcuts,
                    "cleaning_statistics": cleaning_statistics,
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
                cleaning_statistics = cache.get("cleaning_statistics", {})
            jobs, _pagination_total = jobs_and_total
        except DjiRomoAuthError as err:
            self._handle_auth_failure(err)
            raise ConfigEntryAuthFailed(
                f"DJI Home authentication failed: {err}"
            ) from err
        except DjiRomoApiError as err:
            self._cloud_refresh_failures += 1
            if (
                self.data is None
                or self._cloud_refresh_failures >= CLOUD_REFRESH_FAILURE_LIMIT
            ):
                self._cloud_healthy = False
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
        self._cloud_healthy = True
        self._auth_failed = False
        self._last_cloud_success_at = now
        self._async_delete_auth_repair_issue()
        cloud_data = {
            "properties": properties,
            "settings": settings,
            "consumables": {
                item.get("code"): item
                for item in consumables
                if isinstance(item, dict) and item.get("code")
            },
            "dock_consumables": dock_consumables,
            "consumable_alerts": consumable_alerts,
            "cleaning_statistics": cleaning_statistics,
        }
        if cloud_data != snapshot.cloud_data:
            snapshot.cloud_data = cloud_data
            snapshot.cloud_last_updated = now

        total_cleanings = _coerce_int(cleaning_statistics.get("total_count"))
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
        snapshot.last_job = last_job or {}
        # Track active job separately so _current_cleaning_room can read plan_content.
        # Reset the trace whenever a new job UUID appears (new cleaning session); the
        # MQTT mission_bid change also resets it live, this covers the REST path.
        if active_job is not None:
            self._reset_trace_for_session(snapshot, active_job.get("uuid"))
            snapshot.active_job = active_job
        else:
            snapshot.active_job = {}

        rooms = _rooms_from_shortcuts(shortcuts)
        self.shortcuts = shortcuts
        snapshot.rooms = rooms

        map_changed = False
        map_reset = False
        if map_meta:
            new_index = _coerce_int(map_meta.get("map_index"))
            new_version = _coerce_int(map_meta.get("map_version"))
            map_reset = (
                self._map_index is not None
                and new_index is not None
                and new_index != self._map_index
            )
            map_changed = map_reset or (
                self._map_version is not None
                and new_version is not None
                and new_version != self._map_version
            )
            if new_index is not None:
                self._map_index = new_index
            if new_version is not None:
                self._map_version = new_version

        if map_reset:
            snapshot.trajectory = []
            snapshot.obstacles = []
            snapshot.floor_plan_polys = []
            snapshot.grid_map_data = None
            snapshot.carpet_polys = []
            snapshot.restricted_polys = []
            snapshot.virtual_walls = []
            self._trace_session_bid = None
            self._paths_next_index = 0
            self._paths_backfilled = False
            await self._store.async_remove()

        should_fetch_floor_plan = (
            not snapshot.floor_plan_polys
            or self._map_overlays_fetched_at is None
            or map_changed
            or (now - self._map_overlays_fetched_at) > timedelta(hours=6)
        )
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
        snapshot.last_clean_map_uuid = self._last_clean_map_uuid
        self._schedule_map_refresh(
            current=should_fetch_floor_plan,
            report_uuid=(
                completed_uuid if completed_uuid != self._last_clean_map_uuid else None
            ),
        )

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

    def _schedule_map_refresh(
        self,
        *,
        current: bool,
        report_uuid: str | None,
    ) -> None:
        """Fetch large map files in the background so setup and polling stay fast."""
        if current:
            self._pending_current_map_refresh = True
        if (
            report_uuid
            and report_uuid != self._last_clean_map_uuid
            and report_uuid != self._report_refresh_inflight_uuid
        ):
            self._pending_report_uuid = report_uuid

        if not self._pending_current_map_refresh and self._pending_report_uuid is None:
            return
        if self._map_refresh_task is not None and not self._map_refresh_task.done():
            return
        self._map_refresh_task = self.hass.async_create_task(
            self._async_refresh_maps(),
            f"{DOMAIN} refresh map data",
        )

    async def _async_refresh_maps(self) -> None:
        """Drain pending current and report map downloads."""
        while self._pending_current_map_refresh or self._pending_report_uuid:
            fetch_current = self._pending_current_map_refresh
            report_uuid = self._pending_report_uuid
            expected_map_index = self._map_index if fetch_current else None
            expected_map_version = self._map_version if fetch_current else None
            self._pending_current_map_refresh = False
            self._pending_report_uuid = None
            self._current_map_refresh_inflight = fetch_current
            self._report_refresh_inflight_uuid = report_uuid
            started_at = datetime.now(UTC)

            try:
                requests = []
                if fetch_current:
                    requests.append(self.api.async_get_map_data())
                if report_uuid:
                    requests.append(self.api.async_get_job_room_map(report_uuid))
                results = await asyncio.gather(*requests, return_exceptions=True)

                result_index = 0
                current_map: dict[str, Any] | None = None
                report_map: dict[str, Any] | None = None
                if fetch_current:
                    result = results[result_index]
                    result_index += 1
                    if isinstance(result, dict):
                        current_map = result
                    elif isinstance(result, Exception):
                        _LOGGER.debug(
                            "DJI Romo current map refresh failed (%s)",
                            type(result).__name__,
                        )
                if report_uuid:
                    result = results[result_index]
                    if isinstance(result, dict):
                        report_map = result
                    elif isinstance(result, Exception):
                        _LOGGER.debug(
                            "DJI Romo report map refresh failed (%s)",
                            type(result).__name__,
                        )

                self._apply_map_refresh(
                    current_map=current_map,
                    expected_map_index=expected_map_index,
                    expected_map_version=expected_map_version,
                    current_started_at=started_at,
                    report_uuid=report_uuid,
                    report_map=report_map,
                )
            except Exception:
                _LOGGER.exception("Unexpected error while refreshing DJI Romo maps")
            finally:
                self._current_map_refresh_inflight = False
                self._report_refresh_inflight_uuid = None

    def _apply_map_refresh(
        self,
        *,
        current_map: dict[str, Any] | None,
        expected_map_index: int | None,
        expected_map_version: int | None,
        current_started_at: datetime,
        report_uuid: str | None,
        report_map: dict[str, Any] | None,
    ) -> None:
        """Merge downloaded map data into the latest pushed snapshot."""
        if self.data is None:
            return
        snapshot = replace(self.data)
        changed = False
        current_changed = False

        if current_map:
            map_index = _coerce_int(current_map.get("map_index"))
            map_version = _coerce_int(current_map.get("map_version"))
            identity_changed_while_fetching = (
                expected_map_index is not None and self._map_index != expected_map_index
            ) or (
                expected_map_version is not None
                and self._map_version != expected_map_version
            )
            response_is_for_other_map = (
                map_index is not None
                and self._map_index is not None
                and map_index != self._map_index
            ) or (
                map_version is not None
                and self._map_version is not None
                and map_version != self._map_version
            )
            if identity_changed_while_fetching or response_is_for_other_map:
                _LOGGER.debug("Discarding stale DJI Romo current map response")
                current_map = None

        if current_map:
            map_index = _coerce_int(current_map.get("map_index"))
            map_version = _coerce_int(current_map.get("map_version"))
            if self._map_index is None and map_index is not None:
                self._map_index = map_index
            if self._map_version is None and map_version is not None:
                self._map_version = map_version

            # A live MQTT map received after this download started owns the live
            # floor/grid layers; only REST-only restrictions are merged in that case.
            if (
                self._last_map_dispatch_at is None
                or self._last_map_dispatch_at <= current_started_at
            ):
                current_changed |= _set_list_from_layer(
                    snapshot,
                    "floor_plan_polys",
                    current_map.get("seg_map"),
                    "poly_info",
                )
                grid_map = current_map.get("grid_map")
                if isinstance(grid_map, dict) and grid_map != snapshot.grid_map_data:
                    snapshot.grid_map_data = grid_map
                    current_changed = True
                current_changed |= _set_list_from_layer(
                    snapshot,
                    "carpet_polys",
                    current_map.get("carpet_layer"),
                    "data",
                )
            current_changed |= _set_list_from_layer(
                snapshot,
                "restricted_polys",
                current_map.get("restricted_layer"),
                "data",
            )
            current_changed |= _set_list_from_layer(
                snapshot,
                "virtual_walls",
                current_map.get("virtual_wall"),
                "data",
            )
            self._map_overlays_fetched_at = datetime.now(UTC)
            changed |= current_changed

        if report_uuid and report_map:
            if (
                report_uuid != self._last_clean_map_uuid
                or report_map != snapshot.last_clean_map
            ):
                snapshot.last_clean_map = report_map
                snapshot.last_clean_map_uuid = report_uuid
                self._last_clean_map_uuid = report_uuid
                changed = True

        if not changed:
            return
        snapshot.cloud_last_updated = datetime.now(UTC)
        self.async_set_updated_data(snapshot)
        if current_changed:
            self._schedule_trajectory_save(snapshot)

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
            self._handle_auth_failure(err, start_reauth=True)
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
            self._handle_auth_failure(err, start_reauth=True)
            raise UpdateFailed(
                f"Failed to start DJI Romo room '{name}': {err}"
            ) from err
        except DjiRomoApiError as err:
            raise UpdateFailed(
                f"Failed to start DJI Romo room '{name}': {err}"
            ) from err

    async def async_clean_rooms_by_name(self, names: list[str]) -> list[str]:
        """Start a multi-room clean for the given room names.

        Returns the list of names that were not found so the caller can report
        them. Room settings come from the shared HA cleaning options.
        """
        try:
            shortcuts = await self.api.async_get_shortcuts()
        except DjiRomoAuthError as err:
            self._handle_auth_failure(err, start_reauth=True)
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
            self._handle_auth_failure(err, start_reauth=True)
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
        config["clean_speed"] = options[CONF_ROOM_CLEAN_SPEED]
        config["secondary_clean_num"] = base_config.get("secondary_clean_num", 1)
        config["floor_cleaner_type"] = base_config.get("floor_cleaner_type", 0)
        config["repeat_mop"] = base_config.get("repeat_mop", False)
        return config

    @property
    def room_cleaning_options(self) -> dict[str, int]:
        """Return selected room-cleaning options."""
        options = dict(DEFAULT_ROOM_CLEANING_OPTIONS)
        for key in options:
            value = self.entry.options.get(key, self.entry.data.get(key))
            if value is not None:
                with suppress(TypeError, ValueError):
                    options[key] = int(value)
        return options

    async def async_set_room_cleaning_option(self, key: str, value: int) -> None:
        """Persist a room-cleaning option and refresh config-backed entities."""
        if key not in DEFAULT_ROOM_CLEANING_OPTIONS:
            raise UpdateFailed(f"Unknown DJI Romo cleaning option '{key}'.")
        cleaned_options = dict(self.entry.options)
        cleaned_options[key] = int(value)
        self.hass.config_entries.async_update_entry(
            self.entry,
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
                self._handle_auth_failure(err, start_reauth=True)
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
            self._handle_auth_failure(err, start_reauth=True)
            raise UpdateFailed(f"Failed to set DJI Romo voice language: {err}") from err
        except DjiRomoApiError as err:
            raise UpdateFailed(f"Failed to set DJI Romo voice language: {err}") from err
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
            self._handle_auth_failure(err, start_reauth=True)
            raise UpdateFailed(
                f"Failed to run DJI Romo dock action '{action}': {err}"
            ) from err
        except DjiRomoApiError as err:
            raise UpdateFailed(
                f"Failed to run DJI Romo dock action '{action}': {err}"
            ) from err

    @property
    def command_topic(self) -> str:
        """Resolved MQTT topic for commands."""
        configured = self.entry.options.get(
            CONF_COMMAND_TOPIC,
            self.entry.data.get(CONF_COMMAND_TOPIC, DEFAULT_COMMAND_TOPIC),
        )
        try:
            return format_mqtt_topic(
                configured,
                self.device_sn,
                allow_wildcards=False,
            )
        except ValueError:
            _LOGGER.error("Invalid DJI Romo command topic; using the default")
            return format_mqtt_topic(DEFAULT_COMMAND_TOPIC, self.device_sn)

    @property
    def command_mapping(self) -> dict[str, Any]:
        """Merged command mapping from config and defaults."""
        raw = self.entry.options.get(
            CONF_COMMAND_MAPPING,
            self.entry.data.get(CONF_COMMAND_MAPPING, {}),
        )
        try:
            validated = validate_command_mapping(raw)
        except ValueError:
            _LOGGER.error("Invalid DJI Romo command mapping; using the defaults")
            validated = {}
        merged = dict(DEFAULT_COMMAND_MAPPING)
        merged.update(validated)
        return merged

    @property
    def subscription_topics(self) -> list[str]:
        """Resolved MQTT subscriptions."""
        configured = self.entry.options.get(
            CONF_SUBSCRIPTION_TOPICS,
            self.entry.data.get(
                CONF_SUBSCRIPTION_TOPICS,
                DEFAULT_SUBSCRIPTION_TOPICS,
            ),
        )
        try:
            topics = validate_subscription_topics(configured)
        except ValueError:
            _LOGGER.error("Invalid DJI Romo subscription topics; using the defaults")
            topics = DEFAULT_SUBSCRIPTION_TOPICS
        return [format_mqtt_topic(topic, self.device_sn) for topic in topics]

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
        async with self._mqtt_connect_lock:
            for attempt in range(2):
                if self._mqtt_credentials_expired():
                    try:
                        self._mqtt_credentials = (
                            await self.api.async_get_mqtt_credentials()
                        )
                    except DjiRomoAuthError as err:
                        self._handle_auth_failure(err)
                        raise ConfigEntryAuthFailed(
                            f"DJI Home authentication failed: {err}"
                        ) from err
                    except DjiRomoApiError as err:
                        raise UpdateFailed(
                            f"Failed to obtain MQTT credentials: {err}"
                        ) from err
                    self._auth_failed = False
                    self._async_delete_auth_repair_issue()

                credentials = self._mqtt_credentials
                if credentials is None:
                    raise UpdateFailed("DJI Home returned no MQTT credentials.")
                try:
                    await self._mqtt.async_connect(
                        credentials,
                        self.subscription_topics,
                    )
                    return
                except DjiRomoMqttAuthError as err:
                    await self._mqtt.async_disconnect()
                    self._mqtt_credentials = None
                    if attempt == 0:
                        continue
                    raise UpdateFailed(
                        f"Failed to authenticate with DJI Romo MQTT: {err}"
                    ) from err
                except DjiRomoMqttError as err:
                    raise UpdateFailed(
                        f"Failed to connect to DJI Romo MQTT: {err}"
                    ) from err

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
        if self._shutting_down:
            return

        stale_since = self._mqtt.stale_since(MQTT_STALE_AFTER)
        if not self._mqtt.is_connected:
            self._mqtt_down_checks += 1
        else:
            self._mqtt_down_checks = 0

        if (
            self.data is not None
            and self.data.online
            and not self._mqtt.is_connected
            and self._last_mqtt_message_at is not None
            and datetime.now(UTC) - self._last_mqtt_message_at > OFFLINE_AFTER
        ):
            self.async_set_updated_data(replace(self.data, online=False))

        if not self._mqtt_recovering and (
            self._mqtt_down_checks >= 3 or stale_since is not None
        ):
            if stale_since is not None:
                _LOGGER.warning(
                    "DJI Romo MQTT stream silent since %s; rebuilding the session",
                    stale_since.isoformat(),
                )
            self._mqtt_recovering = True
            self._mqtt_recovery_task = self.hass.async_create_task(
                self._async_recover_mqtt(),
                f"{DOMAIN} recover MQTT",
            )

    async def _async_recover_mqtt(self) -> None:
        """Refresh credentials and rebuild the session after a sustained/zombie outage."""
        try:
            await self._mqtt.async_disconnect()
            self._mqtt_credentials = None
            if self._shutting_down:
                return
            await self._async_ensure_mqtt()
        except ConfigEntryAuthFailed as err:
            self.entry.async_start_reauth(self.hass)
            _LOGGER.debug("DJI Romo MQTT recovery needs reauthentication: %s", err)
        except UpdateFailed as err:
            _LOGGER.debug("DJI Romo MQTT recovery attempt failed: %s", err)
        finally:
            self._mqtt_recovering = False
            self._mqtt_down_checks = 0
            self._mqtt_recovery_task = None

    @property
    def available(self) -> bool:
        """Return whether the robot is currently reachable."""
        return bool(
            self.data
            and self.data.online
            and self._cloud_healthy
            and not self._auth_failed
        )

    @property
    def mqtt_connected(self) -> bool:
        """Return whether the DJI cloud MQTT session is connected."""
        return self._mqtt.is_connected

    @property
    def cloud_refresh_failures(self) -> int:
        """Return the number of consecutive cloud refresh failures."""
        return self._cloud_refresh_failures

    @property
    def last_cloud_success_at(self) -> datetime | None:
        """Return the most recent successful REST refresh time."""
        return self._last_cloud_success_at

    async def _async_publish(self, payload: dict[str, Any]) -> None:
        """Publish a payload after ensuring MQTT connectivity."""
        try:
            await self._async_ensure_mqtt()
        except ConfigEntryAuthFailed as err:
            self.entry.async_start_reauth(self.hass)
            raise UpdateFailed("DJI Home authentication must be refreshed.") from err
        bid = payload.get("bid")
        if bid:
            self._sent_bids.append(str(bid))
        _LOGGER.debug("Publishing DJI Romo method %s", payload.get("method"))
        try:
            await self._mqtt.async_publish(self.command_topic, payload)
        except DjiRomoMqttError as err:
            await self._mqtt.async_disconnect()
            self._mqtt_credentials = None
            raise UpdateFailed(f"Failed to publish DJI Romo command: {err}") from err

    async def _async_send_rest_command(self, command_key: str) -> bool:
        """Send commands that are known to be DJI Home REST job actions."""
        try:
            if command_key == "start":
                if self.data and self.data.activity == "paused":
                    await self.api.async_resume_cleaning(self.data.mission_bid)
                else:
                    await self.api.async_start_clean()
                self._set_activity_after_command("cleaning", hold=True)
                return True
            if command_key == "pause":
                await self.api.async_pause_cleaning(self._active_mission_bid())
                self._set_activity_after_command("paused", hold=True)
                return True
            if command_key == "stop":
                await self.api.async_stop_cleaning(self._active_mission_bid())
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
                self._set_activity_after_command("returning", hold=True)
                return True
        except DjiRomoApiError as err:
            if isinstance(err, DjiRomoAuthError):
                self._handle_auth_failure(err, start_reauth=True)
            raise UpdateFailed(
                f"Failed to send DJI Romo command '{command_key}': {err}"
            ) from err

        return False

    def _set_activity_after_command(self, activity: str, *, hold: bool = False) -> None:
        """Publish the expected state after a successful Home Assistant command."""
        self._activity_filter.override(activity, hold=hold)
        if self.data is None:
            return

        snapshot = replace(
            self.data,
            activity=activity,
            online=True,
            last_updated=datetime.now(UTC),
        )
        snapshot.current_room = _current_cleaning_room(snapshot)
        self.async_set_updated_data(snapshot)

    def _active_mission_bid(self) -> str | None:
        """Return the stored bid only while a cleaning job is still active."""
        if self.data and self.data.activity in {"cleaning", "paused", "returning"}:
            return self.data.mission_bid
        return None

    def _handle_auth_failure(
        self,
        error: Exception,
        *,
        start_reauth: bool = False,
    ) -> None:
        """Mark authentication unhealthy and optionally start reauthentication."""
        self._auth_failed = True
        self._cloud_healthy = False
        self._async_create_auth_repair_issue(str(error))
        if start_reauth:
            self.entry.async_start_reauth(self.hass)

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
        had_data = self.data is not None
        previous = self.data or RomoSnapshot()
        topic_kind = _topic_kind(topic)
        self._last_mqtt_message_at = datetime.now(UTC)

        if (
            topic_kind == "services"
            and isinstance(payload, dict)
            and str(payload.get("bid")) in self._sent_bids
        ):
            return

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

        if isinstance(payload, dict):
            snapshot.last_osd_at = datetime.now(UTC)
            snapshot.online = True
            flattened = _flatten_dict(payload)
            _apply_positions(snapshot, flattened)
            _apply_dock_flags(snapshot, flattened)

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
                event_bid = _pick_first(flattened, ("mission_bid",))
                event_matches_mission = (
                    event_bid is None
                    or previous.mission_bid is None
                    or str(event_bid) == previous.mission_bid
                )
                event_activity = (
                    _infer_event_activity(flattened, previous.activity)
                    if event_matches_mission
                    else None
                )
                event_activity = _gate_event_activity(
                    event_activity,
                    snapshot.charger_connected,
                )
                if event_activity is not None:
                    snapshot.activity = self._stable_activity(
                        previous.activity,
                        event_activity,
                        source="events",
                    )

                event_is_current = (
                    event_matches_mission
                    and snapshot.charger_connected != 1
                    and snapshot.activity in {"cleaning", "paused"}
                )
                if (
                    event_is_current
                    and str(payload.get("method")) == "room_clean_progress"
                ):
                    # Live progress figures for the current job.
                    percent = _coerce_int(_pick_first(flattened, ("percent",)))
                    if percent is not None:
                        snapshot.clean_progress = percent
                    acreage = _coerce_float(
                        _pick_first(flattened, ("cleaned_acreage",))
                    )
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
                # Capture the real-time step from MQTT so current_room can update
                # immediately instead of waiting for the 60s REST poll.
                if event_is_current:
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

        if snapshot.activity == "cleaning":
            self._reset_trace_for_session(snapshot, snapshot.mission_bid)

        if (
            snapshot.activity == "cleaning"
            and snapshot.mission_bid
            and self._paths_unsub is None
        ):
            self._start_paths_poll()
        elif (
            snapshot.activity in {"docked", "idle", "error"}
            and self._paths_unsub is not None
        ):
            self._stop_paths_poll()

        if not _meaningful_state_changed(previous, snapshot):
            return

        snapshot.last_updated = datetime.now(UTC)
        self.async_set_updated_data(snapshot)

        if (
            had_data
            and previous.activity != snapshot.activity
            and (
                "cleaning" in (previous.activity, snapshot.activity)
                or snapshot.activity == "docked"
            )
        ):
            self._static_fetched_at = None
            self.hass.async_create_task(
                self.async_request_refresh(),
                f"{DOMAIN} refresh after activity change",
            )

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
        if self.data is not None and previous.online and alerts == previous.hms_alerts:
            return
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
        data = payload.get("data", {}) if isinstance(payload.get("data"), dict) else {}

        if str(data.get("status", "")).lower() == "in_progress":
            active = True
            sub_job = data.get("sub_job_status")
            stage_value = (
                sub_job.get("cur_submission") if isinstance(sub_job, dict) else None
            )
            stage = str(stage_value) if stage_value else previous.drying_stage
            progress = data.get("progress")
            percent = (
                _coerce_int(progress.get("percent"))
                if isinstance(progress, dict) and "percent" in progress
                else previous.drying_percent
            )
            duration = data.get("duration")
            remaining = (
                _coerce_int(duration.get("estimated_remaining_duration"))
                if isinstance(duration, dict)
                and "estimated_remaining_duration" in duration
                else previous.drying_remaining_s
            )
            remaining = remaining if remaining is None or remaining >= 0 else None
        else:
            active, stage, percent, remaining = False, None, None, None

        if previous.online and (
            active,
            stage,
            percent,
            remaining,
        ) == (
            previous.drying_active,
            previous.drying_stage,
            previous.drying_percent,
            previous.drying_remaining_s,
        ):
            return

        snapshot = replace(
            previous,
            online=True,
            last_osd_at=now,
            drying_active=active,
            drying_stage=stage,
            drying_percent=percent,
            drying_remaining_s=remaining,
        )
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

        data = payload.get("data")
        map_data = data.get("map_data", {}) if isinstance(data, dict) else {}
        if not isinstance(map_data, dict):
            return

        map_index = _coerce_int(map_data.get("map_index"))
        map_version = _coerce_int(map_data.get("map_version"))
        if map_index is not None:
            self._map_index = map_index
        if map_version is not None:
            self._map_version = map_version

        seg_map = map_data.get("seg_map", {})
        poly_info = seg_map.get("poly_info")
        if isinstance(poly_info, list) and poly_info:
            snapshot.floor_plan_polys = poly_info

        obstacle_layer = map_data.get("obstacle_layer", {})
        obs_data = obstacle_layer.get("data")
        if isinstance(obs_data, list):
            pts: list[tuple[float, float]] = []
            for item in obs_data:
                if not isinstance(item, dict):
                    continue
                verts = item.get("vertices") or []
                if not verts and "position" in item:
                    verts = [item["position"]]
                for v in verts:
                    if not isinstance(v, dict):
                        continue
                    x = v.get("x")
                    y = v.get("y")
                    if x is not None and y is not None:
                        pts.append((float(x), float(y)))
            snapshot.obstacles = pts

        carpet = map_data.get("carpet_layer")
        if isinstance(carpet, dict) and isinstance(carpet.get("data"), list):
            snapshot.carpet_polys = carpet["data"]

        # Occupancy grid (walls + scanned objects/furniture). DJI pushes the full
        # grid here at ~2 Hz, in the same format decode_grid_cells/image.py already
        # render, so we keep it current — this is what makes the grid grow live like
        # the app. (We still do NOT use grid_map as the cleaning *trace*: it is the
        # cumulative SLAM coverage and doesn't match the app's per-session sweep; the
        # trace is built from the live /paths sweep instead.)
        grid_map = map_data.get("grid_map")
        if isinstance(grid_map, dict) and grid_map.get("map_data"):
            snapshot.grid_map_data = grid_map

        map_fields = (
            "floor_plan_polys",
            "obstacles",
            "carpet_polys",
            "grid_map_data",
        )
        if previous.online and all(
            getattr(previous, field_name) == getattr(snapshot, field_name)
            for field_name in map_fields
        ):
            return
        if (
            previous.online
            and self._last_map_dispatch_at is not None
            and now - self._last_map_dispatch_at < MAP_PUSH_MIN_INTERVAL
        ):
            return

        self._last_map_dispatch_at = now
        snapshot.last_updated = now
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
        return self._activity_filter.update(
            previous_activity,
            candidate_activity,
            source=source,
        )


async def _async_optional_api_call(
    label: str,
    call: Callable[[], Awaitable[Any]],
    fallback: Any,
) -> Any:
    """Return cached/default data when an optional DJI endpoint is unavailable."""
    try:
        return await call()
    except DjiRomoAuthError:
        raise
    except DjiRomoApiError as err:
        _LOGGER.debug("DJI Romo optional %s endpoint is unavailable: %s", label, err)
        return fallback


def _meaningful_state_changed(previous: RomoSnapshot, current: RomoSnapshot) -> bool:
    """Return True when a meaningful entity state changed."""
    if any(
        getattr(previous, key) != getattr(current, key) for key in MEANINGFUL_STATE_KEYS
    ):
        return True

    if _position_changed(previous.dock_x, current.dock_x) or _position_changed(
        previous.dock_y,
        current.dock_y,
    ):
        return True

    # Localization jitters slightly while the robot is charging. The first docked
    # update is already published through the activity change; ignore subsequent
    # robot-pose noise until it leaves the dock.
    if previous.activity == current.activity == "docked":
        return False

    return (
        _position_changed(previous.robot_x, current.robot_x)
        or _position_changed(previous.robot_y, current.robot_y)
        or _yaw_changed(previous.robot_yaw, current.robot_yaw)
    )


def _position_changed(previous: float | None, current: float | None) -> bool:
    """Return whether a position moved far enough to warrant an entity update."""
    if previous is None or current is None:
        return previous != current
    return abs(current - previous) >= POSITION_UPDATE_THRESHOLD


def _yaw_changed(previous: float | None, current: float | None) -> bool:
    """Return whether a heading changed enough, accounting for 360-degree wrap."""
    if previous is None or current is None:
        return previous != current
    delta = abs((current - previous + 180) % 360 - 180)
    return delta >= YAW_UPDATE_THRESHOLD


def _rebase_rest_fields(
    base: RomoSnapshot,
    fetched: RomoSnapshot,
    latest: RomoSnapshot,
) -> RomoSnapshot:
    """Merge REST changes without overwriting newer MQTT-owned fields."""
    merged = replace(latest)
    for snapshot_field in fields(RomoSnapshot):
        name = snapshot_field.name
        if name in LIVE_SEEDED_FIELDS:
            continue
        value = getattr(fetched, name)
        if value != getattr(base, name):
            setattr(merged, name, value)
    merged.current_room = _current_cleaning_room(merged)
    return merged


def _set_list_from_layer(
    snapshot: RomoSnapshot,
    attribute: str,
    layer: Any,
    key: str,
) -> bool:
    """Copy a list from a valid map layer and report whether it changed."""
    if not isinstance(layer, dict):
        return False
    value = layer.get(key)
    if not isinstance(value, list) or value == getattr(snapshot, attribute):
        return False
    setattr(snapshot, attribute, value)
    return True


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
        if target in flattened:
            return flattened[target]
        suffix = f".{target}"
        matches = (
            (key.count(".") + key.count("["), value)
            for key, value in flattened.items()
            if key.endswith(suffix)
        )
        try:
            return min(matches, key=lambda item: item[0])[1]
        except ValueError:
            continue
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

    # The robot can keep reporting the previous mission status after it has
    # physically docked. The charger flag comes from the same device_osd host
    # payload and is the authoritative indication that the robot is at the base.
    if charger_connected == 1:
        return "docked"

    if mission_status == 3:
        return "returning"
    if mission_status in {2, 5}:
        return "cleaning"
    if mission_status == 1:
        return "paused"
    if mission_status == 8:
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


def _gate_event_activity(
    candidate: str | None,
    charger_connected: int | None,
) -> str | None:
    """Reject delayed active-job events while the robot is physically docked."""
    if charger_connected == 1 and candidate in {"cleaning", "paused", "returning"}:
        return None
    return candidate
