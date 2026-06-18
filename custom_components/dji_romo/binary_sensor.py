"""Binary sensors for DJI Romo."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DjiRomoCoordinator
from .entity import DjiRomoCoordinatorEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class DjiRomoBinarySensorDescription(BinarySensorEntityDescription):
    """Entity description for Romo binary sensors."""

    value_fn: Callable[[DjiRomoCoordinator], bool | None]
    # Connectivity sensors must keep reporting while the robot is offline.
    available_when_offline: bool = False


def _truthy(value: Any) -> bool | None:
    """Coerce DJI's mix of bool/int flags into a tri-state boolean."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return None


def _setting(coordinator: DjiRomoCoordinator, *path: str) -> Any:
    """Return a value from the REST settings payload by nested key path."""
    current: Any = coordinator.data.cloud_data.get("settings", {})
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


BINARY_SENSORS: tuple[DjiRomoBinarySensorDescription, ...] = (
    DjiRomoBinarySensorDescription(
        key="online",
        name="Online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        available_when_offline=True,
        value_fn=lambda coordinator: coordinator.available,
    ),
    DjiRomoBinarySensorDescription(
        key="charging",
        name="Charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        # Live from the device_osd stream (~1 s); seeded from REST before first osd.
        value_fn=lambda coordinator: _truthy(coordinator.data.charger_connected),
    ),
    DjiRomoBinarySensorDescription(
        key="dust_bag_installed",
        name="Dust Bag Installed",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _truthy(
            coordinator.property_value("dust_bag_install")
        ),
    ),
    DjiRomoBinarySensorDescription(
        key="problem",
        name="Problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda coordinator: bool(coordinator.data.hms_alerts)
        or coordinator.data.activity == "error",
    ),
    DjiRomoBinarySensorDescription(
        key="auto_dust_box_drying",
        name="Auto Dust Box Drying",
        entity_category=EntityCategory.DIAGNOSTIC,
        # The app setting that enables post-clean dust box drying + UV sterilization.
        # This mirrors the on/off setting, NOT the live drying activity (see the
        # "Drying Status" sensor for that), so no RUNNING device class.
        value_fn=lambda coordinator: _truthy(
            _setting(coordinator, "drying", "dust_box_drying")
        ),
    ),
    DjiRomoBinarySensorDescription(
        key="dust_bag_uv",
        name="Dust Bag UV Lamp",
        device_class=BinarySensorDeviceClass.LIGHT,
        entity_category=EntityCategory.DIAGNOSTIC,
        # Live lamp state (osd, ~1 s): only on during the dust box drying cycle.
        value_fn=lambda coordinator: _truthy(coordinator.data.dust_bag_uv_enable),
    ),
    DjiRomoBinarySensorDescription(
        key="battery_care",
        name="Battery Care",
        entity_category=EntityCategory.DIAGNOSTIC,
        # Live from the device_osd stream (~1 s).
        value_fn=lambda coordinator: _truthy(coordinator.data.battery_care_active),
    ),
    DjiRomoBinarySensorDescription(
        key="child_lock",
        name="Child Lock",
        entity_category=EntityCategory.DIAGNOSTIC,
        # Plain flag: on = child lock enabled.
        value_fn=lambda coordinator: _truthy(
            _setting(coordinator, "is_child_lock_open")
        ),
    ),
    DjiRomoBinarySensorDescription(
        key="do_not_disturb",
        name="Do Not Disturb",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _truthy(
            _setting(coordinator, "no_disturb", "is_open")
        ),
    ),
    # --- Read-only mirrors of the app's on/off settings (controls TBD) ---
    DjiRomoBinarySensorDescription(
        key="carpet_detection",
        name="Carpet Detection",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _truthy(_setting(coordinator, "meet_carpet_mode")),
    ),
    DjiRomoBinarySensorDescription(
        key="pet_care",
        name="Pet Care",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _truthy(_setting(coordinator, "is_pet_care")),
    ),
    DjiRomoBinarySensorDescription(
        key="ai_obstacle_recognition",
        name="AI Obstacle Recognition",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _truthy(
            _setting(coordinator, "ai_recognition", "is_open")
        ),
    ),
    DjiRomoBinarySensorDescription(
        key="auto_mop_wash",
        name="Auto Mop Wash",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _truthy(_setting(coordinator, "auto_wash")),
    ),
    DjiRomoBinarySensorDescription(
        key="mop_ozone_deodorizer",
        name="Mop Ozone Deodorizer",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _truthy(
            _setting(coordinator, "mop_ozone_deodorizer")
        ),
    ),
    DjiRomoBinarySensorDescription(
        key="mop_deodorizer",
        name="Mop Deodorizer",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _truthy(
            _setting(coordinator, "deodorizer_mop", "mode")
        ),
    ),
    DjiRomoBinarySensorDescription(
        key="enhanced_particle_cleaning",
        name="Enhanced Particle Cleaning",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _truthy(
            _setting(coordinator, "enhance_particle_clean")
        ),
    ),
    DjiRomoBinarySensorDescription(
        key="enhanced_stain_cleaning",
        name="Enhanced Stain Cleaning",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _truthy(
            _setting(coordinator, "enhance_stain_clean")
        ),
    ),
    DjiRomoBinarySensorDescription(
        key="status_light",
        name="Status Light",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _truthy(
            _setting(coordinator, "instruct_light_status")
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Romo binary sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        DjiRomoBinarySensor(coordinator, description)
        for description in BINARY_SENSORS
    )


class DjiRomoBinarySensor(DjiRomoCoordinatorEntity, BinarySensorEntity):
    """Coordinator-backed Romo binary sensor."""

    entity_description: DjiRomoBinarySensorDescription

    def __init__(
        self,
        coordinator: DjiRomoCoordinator,
        description: DjiRomoBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_translation_key = description.key
        self._attr_unique_id = f"{coordinator.device_sn}_{description.key}"

    @property
    def available(self) -> bool:
        """Connectivity sensors stay available so they can report 'offline'."""
        if self.entity_description.available_when_offline:
            return self.coordinator.last_update_success
        return super().available

    @property
    def is_on(self) -> bool | None:
        """Return the current binary state."""
        return self.entity_description.value_fn(self.coordinator)
