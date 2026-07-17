"""Buttons for DJI Romo cleaning shortcuts."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from .compat import AddConfigEntryEntitiesCallback
from .const import PLAN_NAME_KEYS
from .coordinator import DjiRomoCoordinator
from .entity import DjiRomoCoordinatorEntity
from .rooms import room_configs_from_shortcuts, room_name

PARALLEL_UPDATES = 0

DOCK_ACTIONS = (
    {
        "key": "dust_collect",
        "icon": "mdi:delete-sweep",
    },
    {
        "key": "wash_mop_pads",
        "icon": "mdi:waves",
    },
    {
        "key": "dry_mop_pads",
        "icon": "mdi:fan",
    },
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Romo shortcut buttons."""
    coordinator = entry.runtime_data
    shortcuts = coordinator.shortcuts

    entities: list[ButtonEntity] = [
        DjiRomoShortcutButton(coordinator, shortcut, index)
        for index, shortcut in enumerate(shortcuts, start=1)
    ]
    entities.extend(
        DjiRomoDockActionButton(coordinator, action) for action in DOCK_ACTIONS
    )
    entities.extend(
        DjiRomoRoomButton(coordinator, room, room_map, duplicate_labels)
        for room, room_map, duplicate_labels in room_configs_from_shortcuts(shortcuts)
    )
    entities.append(DjiRomoClearMapButton(coordinator))
    async_add_entities(entities)


class DjiRomoShortcutButton(DjiRomoCoordinatorEntity, ButtonEntity):
    """Button that starts a DJI Home cleaning shortcut."""

    _attr_icon = "mdi:robot-vacuum"

    def __init__(
        self,
        coordinator: DjiRomoCoordinator,
        shortcut: dict[str, Any],
        index: int,
    ) -> None:
        super().__init__(coordinator)
        self._shortcut = shortcut
        self._attr_name = _shortcut_name(shortcut, index)
        self._attr_unique_id = (
            f"{coordinator.device_sn}_shortcut_{shortcut.get('plan_uuid') or index}"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose shortcut metadata useful for dashboards and debugging."""
        attrs = dict(super().extra_state_attributes)
        attrs["plan_uuid"] = self._shortcut.get("plan_uuid")
        attrs["plan_type"] = self._shortcut.get("plan_type")
        attrs["clean_area_type"] = self._shortcut.get("clean_area_type")
        attrs["rooms"] = len(self._shortcut.get("plan_area_configs", []))
        return attrs

    async def async_press(self) -> None:
        """Start the shortcut."""
        try:
            await self.coordinator.async_start_shortcut(self._shortcut)
        except UpdateFailed as err:
            raise HomeAssistantError(
                f"Failed to start DJI Romo shortcut '{self.name}': {err}"
            ) from err


class DjiRomoRoomButton(DjiRomoCoordinatorEntity, ButtonEntity):
    """Button that starts cleaning a single room."""

    _attr_icon = "mdi:floor-plan"

    def __init__(
        self,
        coordinator: DjiRomoCoordinator,
        room_config: dict[str, Any],
        room_map: dict[str, Any],
        duplicate_labels: set[int],
    ) -> None:
        super().__init__(coordinator)
        self._room_config = room_config
        self._room_map = room_map
        self._room_name = room_name(room_config, duplicate_labels)
        self._attr_name = f"Clean {self._room_name}"
        self._attr_unique_id = (
            f"{coordinator.device_sn}_room_{room_config.get('poly_index')}"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose room metadata useful for dashboards and debugging."""
        attrs = dict(super().extra_state_attributes)
        effective_config = self.coordinator.room_cleaning_config(self._room_config)
        attrs["room_name"] = self._room_name
        attrs["map_name"] = self._room_map.get("name")
        attrs["map_index"] = self._room_map.get("map_index")
        attrs["poly_index"] = self._room_config.get("poly_index")
        attrs["user_label"] = self._room_config.get("user_label")
        attrs["clean_mode"] = effective_config.get("clean_mode")
        attrs["fan_speed"] = effective_config.get("fan_speed")
        attrs["water_level"] = effective_config.get("water_level")
        attrs["clean_num"] = effective_config.get("clean_num")
        attrs["clean_speed"] = effective_config.get("clean_speed")
        return attrs

    async def async_press(self) -> None:
        """Start cleaning this room."""
        try:
            await self.coordinator.async_start_room(
                self._room_config,
                self._room_map,
                self._room_name,
            )
        except UpdateFailed as err:
            raise HomeAssistantError(
                f"Failed to start DJI Romo room '{self._room_name}': {err}"
            ) from err


class DjiRomoDockActionButton(DjiRomoCoordinatorEntity, ButtonEntity):
    """Button that starts a dock maintenance action."""

    def __init__(
        self,
        coordinator: DjiRomoCoordinator,
        action: dict[str, str],
    ) -> None:
        super().__init__(coordinator)
        self._action = action
        self._attr_translation_key = action["key"]
        self._attr_icon = action["icon"]
        self._attr_unique_id = f"{coordinator.device_sn}_{action['key']}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose dock action metadata."""
        attrs = dict(super().extra_state_attributes)
        attrs["dock_action"] = self._action["key"]
        return attrs

    async def async_press(self) -> None:
        """Run the dock action."""
        try:
            await self.coordinator.async_run_dock_action(self._action["key"])
        except UpdateFailed as err:
            raise HomeAssistantError(
                f"Failed to run DJI Romo dock action '{self.name}': {err}"
            ) from err


class DjiRomoClearMapButton(DjiRomoCoordinatorEntity, ButtonEntity):
    """Button that clears the accumulated trajectory on the map image."""

    _attr_icon = "mdi:map-marker-off"
    _attr_translation_key = "clear_map"

    def __init__(self, coordinator: DjiRomoCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_sn}_clear_map"

    @property
    def available(self) -> bool:
        """Stay usable even while the robot is offline (it only clears state)."""
        return self.coordinator.last_update_success

    async def async_press(self) -> None:
        """Clear the stored trajectory."""
        await self.coordinator.async_clear_trajectory()


def _shortcut_name(shortcut: dict[str, Any], index: int) -> str:
    """Return a useful shortcut name.

    DJI localizes ``plan_name`` (often to Chinese) but keeps a stable
    ``plan_name_key`` for its built-in programs, so we translate from the key
    when we recognize it and otherwise trust whatever name the account stored.
    """
    plan_name_key = str(shortcut.get("plan_name_key") or "")
    if plan_name_key in PLAN_NAME_KEYS:
        return PLAN_NAME_KEYS[plan_name_key]
    return str(
        shortcut.get("plan_name")
        or shortcut.get("name")
        or plan_name_key
        or f"Cleaning Program {index}"
    )
