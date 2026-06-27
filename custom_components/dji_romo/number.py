"""Number entities for DJI Romo room cleaning options."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import CONF_ROOM_CLEAN_NUM
from .coordinator import DjiRomoCoordinator
from .entity import DjiRomoCoordinatorEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class DjiRomoNumberDescription(NumberEntityDescription):
    """Entity description for numeric room-cleaning options."""


NUMBERS: tuple[DjiRomoNumberDescription, ...] = (
    DjiRomoNumberDescription(
        key=CONF_ROOM_CLEAN_NUM,
        name="Room Cleaning Passes",
        icon="mdi:counter",
        native_min_value=1,
        native_max_value=3,
        native_step=1,
    ),
)


@dataclass(frozen=True, kw_only=True)
class DjiRomoSettingNumberDescription(NumberEntityDescription):
    """Describes a device-setting number (writes the REST settings endpoint).

    ``param_fn`` builds the ``param`` body for a chosen value (it gets the
    coordinator so nested settings can preserve their sibling fields); ``value_fn``
    reads the current value from the coordinator (None when unknown).
    """

    value_fn: Callable[[DjiRomoCoordinator], float | None]
    param_fn: Callable[[DjiRomoCoordinator, int], dict[str, Any]]


def _setting(coordinator: DjiRomoCoordinator, *path: str) -> Any:
    """Return a value from the REST settings payload by nested key path."""
    current: Any = coordinator.data.cloud_data.get("settings", {})
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


SETTING_NUMBERS: tuple[DjiRomoSettingNumberDescription, ...] = (
    DjiRomoSettingNumberDescription(
        key="device_volume",
        translation_key="device_volume",
        name="Volume",
        icon="mdi:volume-high",
        entity_category=EntityCategory.CONFIG,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        # Flat key, 0-100. Captured via MITM 2026-06-22.
        value_fn=lambda coordinator: _setting(coordinator, "device_volume"),
        param_fn=lambda coordinator, val: {"device_volume": val},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Romo number entities."""
    coordinator = entry.runtime_data
    entities: list[NumberEntity] = [
        DjiRomoRoomOptionNumber(coordinator, description) for description in NUMBERS
    ]
    entities.extend(
        DjiRomoSettingNumber(coordinator, description)
        for description in SETTING_NUMBERS
    )
    async_add_entities(entities)


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
        self._attr_translation_key = description.key
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


class DjiRomoSettingNumber(DjiRomoCoordinatorEntity, NumberEntity):
    """A device setting exposed as a number (writes the REST settings endpoint)."""

    entity_description: DjiRomoSettingNumberDescription

    def __init__(
        self,
        coordinator: DjiRomoCoordinator,
        description: DjiRomoSettingNumberDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_sn}_{description.key}"

    @property
    def native_value(self) -> float | None:
        """Return the current setting value (None when not yet known)."""
        value = self.entity_description.value_fn(self.coordinator)
        return None if value is None else float(value)

    async def async_set_native_value(self, value: float) -> None:
        """Write the chosen value to the device settings."""
        target = int(value)
        try:
            # Builder evaluated under the coordinator's write lock (see switches).
            await self.coordinator.async_set_device_setting(
                lambda: self.entity_description.param_fn(self.coordinator, target)
            )
        except UpdateFailed as err:
            raise HomeAssistantError(
                f"Failed to set DJI Romo '{self.name}': {err}"
            ) from err
