"""Buttons for DJI Romo cleaning shortcuts."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import DOMAIN
from .coordinator import DjiRomoCoordinator
from .entity import DjiRomoCoordinatorEntity

ROOM_LABELS_NL = {
    1: "Slaapkamer",
    2: "Logeerkamer",
    3: "Keuken",
    4: "Eetkamer",
    5: "Studeerkamer",
    6: "Woonkamer",
    7: "Gang",
    8: "Berging",
    9: "Balkon",
    10: "Wasruimte",
    11: "Kast",
    12: "Kantoor",
    13: "Entree",
    14: "Badkamer",
    15: "Kinderkamer",
}

PLAN_NAME_TRANSLATIONS = {
    "default_plan_name_quick": "Grondige enkele stofzuigbeurt",
    "精细单扫": "Grondige enkele stofzuigbeurt",
}

DOCK_ACTIONS = (
    {
        "key": "dust_collect",
        "name": "Stof verzamelen",
        "icon": "mdi:delete-sweep",
    },
    {
        "key": "wash_mop_pads",
        "name": "Dweilpads reinigen",
        "icon": "mdi:waves",
    },
    {
        "key": "dry_mop_pads",
        "name": "Dweilpads drogen",
        "icon": "mdi:fan",
    },
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Romo shortcut buttons."""
    coordinator: DjiRomoCoordinator = hass.data[DOMAIN][entry.entry_id]
    shortcuts = await coordinator.api.async_get_shortcuts()
    entities: list[ButtonEntity] = [
        DjiRomoShortcutButton(coordinator, shortcut, index)
        for index, shortcut in enumerate(shortcuts, start=1)
    ]
    entities.extend(
        DjiRomoDockActionButton(coordinator, action)
        for action in DOCK_ACTIONS
    )
    entities.extend(
        DjiRomoRoomButton(coordinator, room, room_map)
        for room, room_map in _room_configs_from_shortcuts(shortcuts)
    )
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
            f"{coordinator.device_sn}_shortcut_"
            f"{shortcut.get('plan_uuid') or index}"
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
    ) -> None:
        super().__init__(coordinator)
        self._room_config = room_config
        self._room_map = room_map
        self._room_name = _room_name(room_config)
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
        self._attr_name = action["name"]
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


def _shortcut_name(shortcut: dict[str, Any], index: int) -> str:
    """Return a useful shortcut name."""
    name = (
        shortcut.get("plan_name")
        or shortcut.get("name")
        or shortcut.get("plan_name_key")
        or f"Cleaning Program {index}"
    )
    plan_name_key = str(shortcut.get("plan_name_key") or "")
    if str(name) == "精细单扫" and plan_name_key:
        return PLAN_NAME_TRANSLATIONS.get(plan_name_key, str(name))
    return PLAN_NAME_TRANSLATIONS.get(str(name), str(name))


def _room_configs_from_shortcuts(
    shortcuts: list[dict[str, Any]],
) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    """Build one room-clean button per room from a suitable shortcut template."""
    template = _room_template_shortcut(shortcuts)
    if not template:
        return ()
    room_map = template.get("room_map", {})
    rooms = room_map.get("device_map_rooms", [])
    configs = {
        config.get("poly_index"): config
        for config in template.get("plan_area_configs", [])
        if config.get("poly_index") is not None
    }
    result = []
    for index, room in enumerate(sorted(rooms, key=_room_sort_key), start=1):
        poly_index = room.get("poly_index")
        config = dict(configs.get(poly_index) or room)
        config.setdefault("order_id", index)
        config.setdefault("clean_mode", 2)
        config.setdefault("fan_speed", 2)
        config.setdefault("water_level", 2)
        config.setdefault("clean_num", 1)
        config.setdefault("clean_speed", 2)
        result.append((config, room_map))
    return result


def _room_template_shortcut(shortcuts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer a vacuum-only shortcut as the template for room buttons."""
    for shortcut in shortcuts:
        if str(shortcut.get("plan_name", "")).lower() == "stofzuigen":
            return shortcut
    for shortcut in shortcuts:
        configs = shortcut.get("plan_area_configs", [])
        if configs and all(config.get("clean_mode") == 2 for config in configs):
            return shortcut
    return shortcuts[0] if shortcuts else None


def _room_sort_key(room: dict[str, Any]) -> tuple[int, int]:
    order_id = room.get("order_id")
    return (
        int(order_id) if isinstance(order_id, int) and order_id > 0 else 999,
        int(room.get("poly_index") or 0),
    )


def _room_name(room_config: dict[str, Any]) -> str:
    custom_name = str(room_config.get("custom_name") or "").strip()
    if custom_name:
        return custom_name
    label = room_config.get("user_label")
    try:
        label_id = int(label)
    except (TypeError, ValueError):
        label_id = 0
    return ROOM_LABELS_NL.get(label_id, f"Kamer {room_config.get('poly_index')}")
