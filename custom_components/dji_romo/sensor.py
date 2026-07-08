"""Sensors for DJI Romo."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfArea,
    UnitOfLength,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DjiRomoCoordinator
from .entity import DjiRomoCoordinatorEntity

PARALLEL_UPDATES = 0
# Verified live 2026-07-08: 4 is "Vacuum then Mop" (not "Super clean"); 0 is
# not a valid mode.
CLEAN_MODE_LABELS = {
    1: "Vacuum and Mop",
    2: "Vacuum Only",
    3: "Mop Only",
    4: "Vacuum then Mop",
}
FAN_SPEED_LABELS = {
    1: "Quiet",
    2: "Standard",
    3: "Max",
}
WATER_LEVEL_LABELS = {
    1: "Low",
    2: "Medium",
    3: "High",
}
# clean_speed is the DJI app's "Route" setting (path fineness); it applies to
# vacuum-only cleans too. Verified live 2026-07-08.
CLEAN_SPEED_LABELS = {
    0: "Standard",
    1: "Fast",
    2: "Fine",
}
DRYING_STAGE_LABELS = {
    "drying_box": "Drying Dust Box",
    "drying_mop": "Drying Mop Pads",
    "drying_mop_box": "Drying Mop & Dust Box",
}


@dataclass(frozen=True, kw_only=True)
class DjiRomoSensorDescription(SensorEntityDescription):
    """Entity description for Romo sensors."""

    value_fn: Callable[[DjiRomoCoordinator], Any]
    attrs_fn: Callable[[DjiRomoCoordinator], dict[str, Any]] | None = None
    # Show the last known value after a restart while the live value is still None.
    restore: bool = True


SENSORS: tuple[DjiRomoSensorDescription, ...] = (
    DjiRomoSensorDescription(
        key="battery",
        name="Battery",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        value_fn=lambda coordinator: coordinator.data.battery_level,
    ),
    DjiRomoSensorDescription(
        key="current_cleaning_mode",
        name="Current Cleaning Mode",
        value_fn=lambda coordinator: _label(
            coordinator.data.clean_mode,
            CLEAN_MODE_LABELS,
        ),
        attrs_fn=lambda coordinator: _raw_value_attr(
            "clean_mode",
            coordinator.data.clean_mode,
        ),
    ),
    DjiRomoSensorDescription(
        key="current_suction_power",
        name="Current Suction Power",
        value_fn=lambda coordinator: _mode_aware_label(
            coordinator,
            coordinator.data.fan_speed,
            FAN_SPEED_LABELS,
            (3,),  # Mop Only
        ),
        attrs_fn=lambda coordinator: _raw_value_attr(
            "fan_speed",
            coordinator.data.fan_speed,
        ),
    ),
    DjiRomoSensorDescription(
        key="current_water_level",
        name="Current Water Level",
        value_fn=lambda coordinator: _mode_aware_label(
            coordinator,
            coordinator.data.water_level,
            WATER_LEVEL_LABELS,
            (2,),  # Vacuum Only
        ),
        attrs_fn=lambda coordinator: _raw_value_attr(
            "water_level",
            coordinator.data.water_level,
        ),
    ),
    DjiRomoSensorDescription(
        key="current_cleaning_passes",
        name="Current Cleaning Passes",
        value_fn=lambda coordinator: coordinator.data.clean_num,
    ),
    DjiRomoSensorDescription(
        key="current_mopping_speed",
        name="Current Route",
        value_fn=lambda coordinator: _label(
            coordinator.data.clean_speed,
            CLEAN_SPEED_LABELS,
        ),
        attrs_fn=lambda coordinator: _raw_value_attr(
            "clean_speed",
            coordinator.data.clean_speed,
        ),
    ),
    DjiRomoSensorDescription(
        key="firmware",
        name="Firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _cloud_path(
            coordinator, "properties.device_base_info.device_version.firmware_version"
        ),
    ),
    DjiRomoSensorDescription(
        key="dock_serial",
        name="Dock Serial",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _cloud_path(coordinator, "properties.dock_sn"),
    ),
    DjiRomoSensorDescription(
        key="clean_water_tank",
        name="Clean Water Tank",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _dock_value(
            coordinator, "clean_water_tank", "percentage"
        ),
        attrs_fn=lambda coordinator: _dock_attrs(coordinator, "clean_water_tank"),
    ),
    DjiRomoSensorDescription(
        key="dirty_water_tank",
        name="Dirty Water Tank",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _dock_value(
            coordinator, "dirty_water_tank", "percentage"
        ),
        attrs_fn=lambda coordinator: _dock_attrs(coordinator, "dirty_water_tank"),
    ),
    DjiRomoSensorDescription(
        key="dock_cleaning_solution",
        name="Dock Cleaning Solution",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _dock_value(
            coordinator, "main_cleaner", "percentage"
        ),
        attrs_fn=lambda coordinator: _dock_attrs(coordinator, "main_cleaner"),
    ),
    DjiRomoSensorDescription(
        key="dock_dust_bag",
        name="Dock Dust Bag",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _dock_value(
            coordinator, "dust_bag_consumable", "percentage"
        ),
        attrs_fn=lambda coordinator: _dock_attrs(coordinator, "dust_bag_consumable"),
    ),
    DjiRomoSensorDescription(
        key="mop_pad",
        name="Mop Pad",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _consumable_value(
            coordinator, "mop_runtime", "percentage"
        ),
        attrs_fn=lambda coordinator: _consumable_attrs(coordinator, "mop_runtime"),
    ),
    DjiRomoSensorDescription(
        key="side_brush",
        name="Side Brush",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _consumable_value(
            coordinator, "side_brush_runtime", "percentage"
        ),
        attrs_fn=lambda coordinator: _consumable_attrs(
            coordinator, "side_brush_runtime"
        ),
    ),
    DjiRomoSensorDescription(
        key="filter",
        name="High-Efficiency Filter",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _consumable_value(
            coordinator, "dust_box_filter_life", "percentage"
        ),
        attrs_fn=lambda coordinator: _consumable_attrs(
            coordinator, "dust_box_filter_life"
        ),
    ),
    DjiRomoSensorDescription(
        key="roller_brush",
        name="Roller Brush",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _consumable_value(
            coordinator, "mid_brush_runtime", "percentage"
        ),
        attrs_fn=lambda coordinator: _consumable_attrs(
            coordinator, "mid_brush_runtime"
        ),
    ),
    DjiRomoSensorDescription(
        key="dust_bag",
        name="Dust Bag",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _consumable_value(
            coordinator, "dust_bag_life", "percentage"
        ),
        attrs_fn=lambda coordinator: _consumable_attrs(coordinator, "dust_bag_life"),
    ),
    DjiRomoSensorDescription(
        key="cleaning_solution",
        name="Cleaning Solution",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _consumable_value(
            coordinator, "sterilizing_liquid_life", "percentage"
        ),
        attrs_fn=lambda coordinator: _consumable_attrs(
            coordinator, "sterilizing_liquid_life"
        ),
    ),
    DjiRomoSensorDescription(
        key="antibacterial_solution",
        name="Antibacterial Cleaning Solution",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _consumable_value(
            coordinator, "secondary_cleaner_life", "percentage"
        ),
        attrs_fn=lambda coordinator: _consumable_attrs(
            coordinator, "secondary_cleaner_life"
        ),
    ),
    DjiRomoSensorDescription(
        key="base_washboard",
        name="Base Station Washboard",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _consumable_value(
            coordinator, "self_clean_cnt", "percentage"
        ),
        attrs_fn=lambda coordinator: _consumable_attrs(coordinator, "self_clean_cnt"),
    ),
    DjiRomoSensorDescription(
        key="consumable_alerts",
        name="Consumable Alerts",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: len(
            coordinator.data.cloud_data.get("consumable_alerts", [])
        ),
        attrs_fn=lambda coordinator: {
            "alerts": coordinator.data.cloud_data.get("consumable_alerts", [])
        },
    ),
    DjiRomoSensorDescription(
        key="active_alerts",
        name="Active Alerts",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: len(coordinator.data.hms_alerts),
        attrs_fn=lambda coordinator: {"alerts": coordinator.data.hms_alerts},
    ),
    DjiRomoSensorDescription(
        key="current_room",
        name="Current Cleaning Room",
        icon="mdi:floor-plan",
        # No restore: this must be empty when the robot isn't cleaning, not show a
        # stale room from the previous run.
        restore=False,
        value_fn=lambda coordinator: coordinator.data.current_room,
    ),
    DjiRomoSensorDescription(
        key="mapped_area",
        name="Mapped Area",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        icon="mdi:ruler-square",
        value_fn=lambda coordinator: _mapped_area(coordinator),
        attrs_fn=lambda coordinator: {
            "rooms": len(coordinator.data.rooms),
            "areas": {r["name"]: r["area"] for r in coordinator.data.rooms},
        },
    ),
    DjiRomoSensorDescription(
        key="room_count",
        name="Room Count",
        icon="mdi:home-floor-g",
        value_fn=lambda coordinator: len(coordinator.data.rooms) or None,
    ),
    DjiRomoSensorDescription(
        key="distance_to_dock",
        name="Distance to Dock",
        entity_registry_enabled_default=False,
        native_unit_of_measurement=UnitOfLength.METERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-marker-distance",
        value_fn=lambda coordinator: _distance_to_dock(coordinator),
    ),
    DjiRomoSensorDescription(
        key="robot_position",
        name="Robot Position",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:robot-vacuum",
        value_fn=lambda coordinator: _position_text(coordinator),
        attrs_fn=lambda coordinator: _position_attrs(coordinator),
    ),
    DjiRomoSensorDescription(
        key="last_clean_area",
        name="Last Clean Area",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator: _job_value(coordinator, "cleaned_acreage"),
    ),
    DjiRomoSensorDescription(
        key="last_clean_duration",
        name="Last Clean Duration",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        value_fn=lambda coordinator: _job_duration_minutes(coordinator),
    ),
    DjiRomoSensorDescription(
        key="last_clean_status",
        name="Last Clean Status",
        value_fn=lambda coordinator: _job_value(coordinator, "status"),
        attrs_fn=lambda coordinator: _last_job_attrs(coordinator),
    ),
    DjiRomoSensorDescription(
        key="last_clean_start",
        name="Last Clean Start",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda coordinator: _job_timestamp(coordinator, "start_time"),
    ),
    DjiRomoSensorDescription(
        key="last_clean_end",
        name="Last Clean End",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda coordinator: _job_timestamp(coordinator, "end_time"),
    ),
    DjiRomoSensorDescription(
        key="total_cleanings",
        name="Total Cleanings",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda coordinator: coordinator.data.total_cleanings,
    ),
    # --- Group A: live progress of the current cleaning job ---
    DjiRomoSensorDescription(
        key="cleaning_progress",
        name="Cleaning Progress",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:progress-check",
        # 0 (not unknown) when no clean is in progress.
        value_fn=lambda coordinator: coordinator.data.clean_progress or 0,
    ),
    DjiRomoSensorDescription(
        key="current_clean_area",
        name="Current Clean Area",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:ruler-square",
        # 0 (not unknown) when no clean is in progress.
        value_fn=lambda coordinator: coordinator.data.cleaned_area or 0,
    ),
    DjiRomoSensorDescription(
        key="current_clean_duration",
        name="Current Clean Duration",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        icon="mdi:timer-outline",
        # 0 (not unknown) when no clean is in progress.
        value_fn=lambda coordinator: _seconds_to_minutes(
            coordinator.data.clean_duration_s
        ) or 0,
    ),
    DjiRomoSensorDescription(
        key="clean_time_remaining",
        name="Clean Time Remaining",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        icon="mdi:timer-sand",
        # 0 (not unknown) when no clean is in progress.
        value_fn=lambda coordinator: _seconds_to_minutes(
            coordinator.data.clean_remaining_s
        ) or 0,
    ),
    # --- Group B: stats of the most recent finished job ---
    DjiRomoSensorDescription(
        key="last_clean_battery_used",
        name="Last Clean Battery Used",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:battery-minus-variant",
        value_fn=lambda coordinator: _job_value(coordinator, "battery_consumption"),
    ),
    DjiRomoSensorDescription(
        key="last_clean_dust_collections",
        name="Last Clean Dust Collections",
        icon="mdi:delete-restore",
        value_fn=lambda coordinator: _job_value(coordinator, "dust_collect_times"),
    ),
    DjiRomoSensorDescription(
        key="last_clean_mop_washes",
        name="Last Clean Mop Washes",
        icon="mdi:water-sync",
        value_fn=lambda coordinator: _job_value(coordinator, "wash_back_times"),
    ),
    DjiRomoSensorDescription(
        key="last_clean_dock_returns",
        name="Last Clean Dock Returns",
        icon="mdi:home-import-outline",
        value_fn=lambda coordinator: _job_value(coordinator, "return_charge_times"),
    ),
    # --- Group C: diagnostic status ---
    DjiRomoSensorDescription(
        key="hatch_status",
        name="Hatch",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:window-shutter",
        value_fn=lambda coordinator: _hatch_status(coordinator),
    ),
    DjiRomoSensorDescription(
        key="network_status",
        name="Network Status",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:wifi",
        value_fn=lambda coordinator: coordinator.property_value("network_status"),
    ),
    DjiRomoSensorDescription(
        key="drying_status",
        name="Drying Status",
        icon="mdi:weather-windy",
        value_fn=lambda coordinator: _drying_status(coordinator),
    ),
    DjiRomoSensorDescription(
        key="drying_progress",
        name="Drying Progress",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:water-percent",
        # 0 (not unknown) when no drying is in progress.
        value_fn=lambda coordinator: coordinator.data.drying_percent or 0,
    ),
    DjiRomoSensorDescription(
        key="drying_time_remaining",
        name="Drying Time Remaining",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        icon="mdi:timer-sand",
        value_fn=lambda coordinator: _drying_remaining_minutes(coordinator),
    ),
    # Device volume is now a writable number (see number.py).
    # Voice language is now a writable select (see select.py).
    # Dust collection mode is now a writable select (see select.py).
    # Auto drying is now a writable switch (see switch.py).
    # Hot water mopping is now a writable switch (see switch.py).
    # Auto add cleaner is now a writable switch (see switch.py).
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Romo sensors."""
    coordinator = entry.runtime_data
    async_add_entities(DjiRomoSensor(coordinator, description) for description in SENSORS)


class DjiRomoSensor(DjiRomoCoordinatorEntity, RestoreSensor):
    """Coordinator-backed Romo sensor."""

    entity_description: DjiRomoSensorDescription

    def __init__(
        self,
        coordinator: DjiRomoCoordinator,
        description: DjiRomoSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_translation_key = description.key
        self._attr_unique_id = f"{coordinator.device_sn}_{description.key}"
        self._restored_value: Any = None

    async def async_added_to_hass(self) -> None:
        """Load the last value so it shows after a restart until live data arrives."""
        await super().async_added_to_hass()
        if self.entity_description.restore:
            last = await self.async_get_last_sensor_data()
            if last is not None:
                self._restored_value = last.native_value

    @property
    def native_value(self) -> Any:
        """Return the live value, falling back to the last restored one if None."""
        value = self.entity_description.value_fn(self.coordinator)
        if value is None and self.entity_description.restore:
            return self._restored_value
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return sensor-specific attributes."""
        attrs = dict(super().extra_state_attributes)
        if self.entity_description.attrs_fn is not None:
            attrs.update(self.entity_description.attrs_fn(self.coordinator))
        return attrs


def _cloud_path(coordinator: DjiRomoCoordinator, path: str) -> Any:
    """Return a value from the slower REST cloud payload."""
    current: Any = coordinator.data.cloud_data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _mapped_area(coordinator: DjiRomoCoordinator) -> float | None:
    """Total mapped floor area across all rooms, in m²."""
    rooms = coordinator.data.rooms
    if not rooms:
        return None
    return round(sum(r.get("area") or 0.0 for r in rooms), 1)


def _distance_to_dock(coordinator: DjiRomoCoordinator) -> float | None:
    """Straight-line distance from the robot to its dock, in metres."""
    data = coordinator.data
    if None in (data.robot_x, data.robot_y, data.dock_x, data.dock_y):
        return None
    from math import hypot

    return round(hypot(data.robot_x - data.dock_x, data.robot_y - data.dock_y), 2)


def _position_text(coordinator: DjiRomoCoordinator) -> str | None:
    """Compact "x, y" label for the robot position sensor state."""
    data = coordinator.data
    if data.robot_x is None or data.robot_y is None:
        return None
    return f"{data.robot_x}, {data.robot_y}"


def _position_attrs(coordinator: DjiRomoCoordinator) -> dict[str, Any]:
    """Expose robot/dock coordinates and heading."""
    data = coordinator.data
    attrs: dict[str, Any] = {}
    for key, value in (
        ("x", data.robot_x),
        ("y", data.robot_y),
        ("heading", data.robot_yaw),
        ("dock_x", data.dock_x),
        ("dock_y", data.dock_y),
    ):
        if value is not None:
            attrs[key] = value
    return attrs


def _job_value(coordinator: DjiRomoCoordinator, key: str) -> Any:
    """Return a field from the most recent cleaning job."""
    return coordinator.data.last_job.get(key)


def _job_duration_minutes(coordinator: DjiRomoCoordinator) -> int | None:
    """Return the last job duration in whole minutes."""
    seconds = coordinator.data.last_job.get("job_duration")
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    return round(seconds / 60)


def _job_timestamp(coordinator: DjiRomoCoordinator, key: str) -> datetime | None:
    """Return a job epoch-second field as an aware datetime."""
    value = coordinator.data.last_job.get(key)
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value, tz=UTC)


def _last_job_attrs(coordinator: DjiRomoCoordinator) -> dict[str, Any]:
    """Expose the headline details of the most recent job."""
    job = coordinator.data.last_job
    return {
        key: job.get(key)
        for key in ("name", "plan_name_key", "uuid", "startup_type")
        if job.get(key) not in (None, "")
    }


def _seconds_to_minutes(seconds: Any) -> int | None:
    """Convert a seconds value to whole minutes, or None when unavailable."""
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return None
    return round(seconds / 60)


def _hatch_status(coordinator: DjiRomoCoordinator) -> str | None:
    """Return the robot hatch state (Open/Closed) from the live osd stream."""
    value = coordinator.data.hatch_status
    if value is None:
        return None
    return "Closed" if value == 0 else "Open"


def _drying_status(coordinator: DjiRomoCoordinator) -> str:
    """Return a human label for the dock drying state."""
    data = coordinator.data
    if not data.drying_active:
        return "Idle"
    return DRYING_STAGE_LABELS.get(data.drying_stage or "", "Drying")


def _drying_remaining_minutes(coordinator: DjiRomoCoordinator) -> int:
    """Return the drying time remaining in whole minutes (0 when not drying).

    Reports 0 rather than None when no drying is in progress, so the sensor shows
    "0 min" (finished/idle) instead of going to an "unknown" state.
    """
    seconds = coordinator.data.drying_remaining_s
    if (
        not coordinator.data.drying_active
        or not isinstance(seconds, (int, float))
        or seconds < 0
    ):
        return 0
    return round(seconds / 60)


def _label(value: int | None, labels: dict[int, str]) -> str | None:
    """Return a display label for a numeric DJI setting."""
    if value is None:
        return None
    return labels.get(value, str(value))


def _mode_aware_label(
    coordinator: DjiRomoCoordinator,
    value: int | None,
    labels: dict[int, str],
    na_modes: tuple[int, ...],
) -> str | None:
    """Label that becomes "Not Applicable" in clean modes where it isn't used.

    The robot still reports a mopping speed / water level in Vacuum Only, and a
    suction power in Mop Only, even though they aren't used. ``na_modes`` lists the
    clean_mode values for which this setting should read "Not Applicable":
    Vacuum Only = 2 (no mopping), Mop Only = 3 (no vacuuming). See CLEAN_MODE_LABELS.
    """
    if coordinator.data.clean_mode in na_modes:
        return "Not Applicable"
    return _label(value, labels)


def _raw_value_attr(key: str, value: int | None) -> dict[str, Any]:
    """Expose the raw numeric setting alongside the translated label."""
    if value is None:
        return {}
    return {key: value}


def _consumable_value(
    coordinator: DjiRomoCoordinator,
    code: str,
    key: str,
) -> Any:
    """Return a value from the consumables payload."""
    item = coordinator.data.cloud_data.get("consumables", {}).get(code)
    if isinstance(item, dict):
        return item.get(key)
    return None


def _consumable_attrs(coordinator: DjiRomoCoordinator, code: str) -> dict[str, Any]:
    """Return useful consumable metadata."""
    item = coordinator.data.cloud_data.get("consumables", {}).get(code)
    if not isinstance(item, dict):
        return {}
    return {
        key: item.get(key)
        for key in (
            "code",
            "name",
            "remaining_available",
            "alarm",
            "alarm_message",
            "maintain_text",
            "maintain_url",
        )
        if item.get(key) not in (None, "")
    }


def _dock_value(
    coordinator: DjiRomoCoordinator,
    code: str,
    key: str,
) -> Any:
    """Return a value from the dock consumables payload."""
    item = coordinator.data.cloud_data.get("dock_consumables", {}).get(code)
    if isinstance(item, dict):
        return item.get(key)
    return None


def _dock_attrs(coordinator: DjiRomoCoordinator, code: str) -> dict[str, Any]:
    """Return useful dock consumable metadata."""
    item = coordinator.data.cloud_data.get("dock_consumables", {}).get(code)
    if not isinstance(item, dict):
        return {}

    attrs = {
        key: item.get(key)
        for key in ("installed", "type", "percentage")
        if item.get(key) is not None
    }
    consumable = item.get("cleaner_consumable") or item
    if isinstance(consumable, dict):
        attrs.update(
            {
                key: consumable.get(key)
                for key in (
                    "code",
                    "name",
                    "remaining_available",
                    "alarm",
                    "alarm_message",
                    "maintain_text",
                    "maintain_url",
                )
                if consumable.get(key) not in (None, "")
            }
        )
    return attrs
