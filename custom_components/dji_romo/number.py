"""Number entities for DJI Romo room cleaning options."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ROOM_CLEAN_NUM, DOMAIN
from .coordinator import DjiRomoCoordinator
from .entity import DjiRomoCoordinatorEntity


@dataclass(frozen=True, kw_only=True)
class DjiRomoNumberDescription(NumberEntityDescription):
    """Entity description for numeric room-cleaning options."""


NUMBERS: tuple[DjiRomoNumberDescription, ...] = (
    DjiRomoNumberDescription(
        key=CONF_ROOM_CLEAN_NUM,
        name="Kamer Schoonmaakrondes",
        icon="mdi:counter",
        native_min_value=1,
        native_max_value=3,
        native_step=1,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Romo number entities."""
    coordinator: DjiRomoCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(DjiRomoRoomOptionNumber(coordinator, description) for description in NUMBERS)


class DjiRomoRoomOptionNumber(DjiRomoCoordinatorEntity, NumberEntity):
    """Number entity backed by config entry options."""

    entity_description: DjiRomoNumberDescription

    def __init__(
        self,
        coordinator: DjiRomoCoordinator,
        description: DjiRomoNumberDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_sn}_{description.key}"

    @property
    def native_value(self) -> float | None:
        """Return the selected value."""
        return float(self.coordinator.room_cleaning_options[self.entity_description.key])

    async def async_set_native_value(self, value: float) -> None:
        """Persist a numeric option."""
        await self.coordinator.async_set_room_cleaning_option(
            self.entity_description.key,
            int(value),
        )
