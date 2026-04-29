"""Sensors for DJI Romo."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ATTR_LAST_UPDATED, DOMAIN
from .coordinator import DjiRomoCoordinator
from .entity import DjiRomoCoordinatorEntity

CLEAN_MODE_LABELS = {
    0: "Vacuum then Mop",
    1: "Vacuum and Mop",
    2: "Vacuum Only",
    3: "Mop Only",
    4: "Super clean",
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
CLEAN_SPEED_LABELS = {
    0: "Not Applicable",
    1: "Slow",
    2: "Standard",
    3: "Fast",
}


@dataclass(frozen=True, kw_only=True)
class DjiRomoSensorDescription(SensorEntityDescription):
    """Entity description for Romo sensors."""

    value_fn: Callable[[DjiRomoCoordinator], Any]
    attrs_fn: Callable[[DjiRomoCoordinator], dict[str, Any]] | None = None


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
        value_fn=lambda coordinator: _label(
            coordinator.data.fan_speed,
            FAN_SPEED_LABELS,
        ),
        attrs_fn=lambda coordinator: _raw_value_attr(
            "fan_speed",
            coordinator.data.fan_speed,
        ),
    ),
    DjiRomoSensorDescription(
        key="current_water_level",
        name="Current Water Level",
        value_fn=lambda coordinator: _label(
            coordinator.data.water_level,
            WATER_LEVEL_LABELS,
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
        name="Current Mopping Speed",
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
        key="device_volume",
        name="Device Volume",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _cloud_path(coordinator, "settings.device_volume"),
    ),
    DjiRomoSensorDescription(
        key="device_language",
        name="Device Language",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _cloud_path(coordinator, "settings.device_language"),
    ),
    DjiRomoSensorDescription(
        key="auto_dust_collect",
        name="Auto Dust Collect",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _cloud_path(
            coordinator, "settings.dust_collect.collect_mode"
        ),
        attrs_fn=lambda coordinator: _cloud_path(
            coordinator, "settings.dust_collect"
        )
        or {},
    ),
    DjiRomoSensorDescription(
        key="auto_drying",
        name="Auto Drying",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _cloud_path(
            coordinator, "settings.drying.auto_enable"
        ),
        attrs_fn=lambda coordinator: _cloud_path(coordinator, "settings.drying") or {},
    ),
    DjiRomoSensorDescription(
        key="hot_water_mop",
        name="Hot Water Mopping",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _cloud_path(
            coordinator, "settings.wash_mop_with_hot_water"
        ),
    ),
    DjiRomoSensorDescription(
        key="auto_add_cleaner",
        name="Auto Add Cleaner",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _cloud_path(
            coordinator, "settings.add_cleaner_auto.is_add_in_mop"
        ),
        attrs_fn=lambda coordinator: _cloud_path(
            coordinator, "settings.add_cleaner_auto"
        )
        or {},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Romo sensors."""
    coordinator: DjiRomoCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(DjiRomoSensor(coordinator, description) for description in SENSORS)


class DjiRomoSensor(DjiRomoCoordinatorEntity, SensorEntity):
    """Coordinator-backed Romo sensor."""

    entity_description: DjiRomoSensorDescription

    def __init__(
        self,
        coordinator: DjiRomoCoordinator,
        description: DjiRomoSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_sn}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the current sensor state."""
        return self.entity_description.value_fn(self.coordinator)

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


def _label(value: int | None, labels: dict[int, str]) -> str | None:
    """Return a display label for a numeric DJI setting."""
    if value is None:
        return None
    return labels.get(value, str(value))


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
