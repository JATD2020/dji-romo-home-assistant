"""Select entities for DJI Romo room cleaning options."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_ROOM_CLEAN_MODE,
    CONF_ROOM_CLEAN_SPEED,
    CONF_ROOM_FAN_SPEED,
    CONF_ROOM_WATER_LEVEL,
    DOMAIN,
)
from .coordinator import DjiRomoCoordinator
from .entity import DjiRomoCoordinatorEntity


@dataclass(frozen=True, kw_only=True)
class DjiRomoSelectDescription(SelectEntityDescription):
    """Entity description for selectable room-cleaning options."""

    option_map: dict[str, int]


SELECTS: tuple[DjiRomoSelectDescription, ...] = (
    DjiRomoSelectDescription(
        key=CONF_ROOM_CLEAN_MODE,
        name="Room Cleaning Mode",
        icon="mdi:robot-vacuum",
        option_map={
            "Vacuum then Mop": 0,
            "Vacuum and Mop": 1,
            "Vacuum Only": 2,
            "Mop Only": 3,
            "Super clean": 4,
        },
    ),
    DjiRomoSelectDescription(
        key=CONF_ROOM_FAN_SPEED,
        name="Room Suction Power",
        icon="mdi:fan",
        option_map={
            "Quiet": 1,
            "Standard": 2,
            "Max": 3,
        },
    ),
    DjiRomoSelectDescription(
        key=CONF_ROOM_WATER_LEVEL,
        name="Room Water Level",
        icon="mdi:water",
        option_map={
            "Low": 1,
            "Medium": 2,
            "High": 3,
        },
    ),
    DjiRomoSelectDescription(
        key=CONF_ROOM_CLEAN_SPEED,
        name="Room Mopping Speed",
        icon="mdi:speedometer",
        option_map={
            "Slow": 1,
            "Standard": 2,
            "Fast": 3,
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Romo select entities."""
    coordinator: DjiRomoCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(DjiRomoRoomOptionSelect(coordinator, description) for description in SELECTS)


class DjiRomoRoomOptionSelect(DjiRomoCoordinatorEntity, SelectEntity):
    """Select entity backed by config entry options."""

    entity_description: DjiRomoSelectDescription

    def __init__(
        self,
        coordinator: DjiRomoCoordinator,
        description: DjiRomoSelectDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_sn}_{description.key}"
        self._attr_options = list(description.option_map)

    @property
    def current_option(self) -> str | None:
        """Return the currently selected option."""
        value = self.coordinator.room_cleaning_options[self.entity_description.key]
        for option, option_value in self.entity_description.option_map.items():
            if option_value == value:
                return option
        return None

    async def async_select_option(self, option: str) -> None:
        """Persist a selected option."""
        await self.coordinator.async_set_room_cleaning_option(
            self.entity_description.key,
            self.entity_description.option_map[option],
        )
