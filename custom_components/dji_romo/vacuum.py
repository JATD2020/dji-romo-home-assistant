"""Vacuum platform for DJI Romo."""

from __future__ import annotations

from typing import Any

from homeassistant.components.vacuum import StateVacuumEntity
from homeassistant.components.vacuum.const import VacuumActivity, VacuumEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import UpdateFailed
import voluptuous as vol

from .const import (
    ATTR_LAST_TOPIC,
    ATTR_LAST_UPDATED,
    ATTR_ROOMS,
    ATTR_SELECTED_TOPIC,
    CONF_ROOM_FAN_SPEED,
    SERVICE_CLEAN_ROOMS,
)
from .coordinator import DjiRomoCoordinator
from .entity import DjiRomoCoordinatorEntity

PARALLEL_UPDATES = 0
FAN_SPEED_OPTIONS = {
    1: "Quiet",
    2: "Standard",
    3: "Max",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the DJI Romo vacuum entity."""
    coordinator = entry.runtime_data
    async_add_entities([DjiRomoVacuum(coordinator)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_CLEAN_ROOMS,
        {vol.Required(ATTR_ROOMS): vol.All(cv.ensure_list, [cv.string], vol.Length(min=1))},
        "async_clean_rooms",
    )


class DjiRomoVacuum(DjiRomoCoordinatorEntity, StateVacuumEntity):
    """Representation of a DJI Romo robot."""

    _attr_name = None
    _attr_supported_features = (
        VacuumEntityFeature.STATE
        | VacuumEntityFeature.START
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.LOCATE
        | VacuumEntityFeature.SEND_COMMAND
        | VacuumEntityFeature.FAN_SPEED
    )
    _attr_fan_speed_list = list(FAN_SPEED_OPTIONS.values())

    def __init__(self, coordinator: DjiRomoCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_sn}_vacuum"

    @property
    def activity(self) -> VacuumActivity | None:
        """Return the vacuum activity, ignoring any unexpected raw value."""
        try:
            return VacuumActivity(self.coordinator.data.activity)
        except ValueError:
            return None

    @property
    def fan_speed(self) -> str | None:
        """Return the suction mode: the robot's live value, else the room default.

        ``async_set_fan_speed`` writes the room-clean option, so when the robot
        isn't reporting a live suction we surface that configured default to keep
        the selector in sync with what it controls.
        """
        value = self.coordinator.data.fan_speed
        if value is None:
            value = self.coordinator.room_cleaning_options[CONF_ROOM_FAN_SPEED]
        return FAN_SPEED_OPTIONS.get(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose parsed state details without storing full raw MQTT payloads."""
        attrs = dict(super().extra_state_attributes)
        if self.coordinator.data.cleaned_area is not None:
            attrs["cleaned_area"] = self.coordinator.data.cleaned_area
        if self.coordinator.data.status_text is not None:
            attrs["status_text"] = self.coordinator.data.status_text
        if self.coordinator.data.selected_topic is not None:
            attrs[ATTR_SELECTED_TOPIC] = self.coordinator.data.selected_topic
            attrs[ATTR_LAST_TOPIC] = self.coordinator.data.selected_topic
        if self.coordinator.data.last_updated is not None:
            attrs[ATTR_LAST_UPDATED] = self.coordinator.data.last_updated.isoformat()
        return attrs

    async def async_start(self, **kwargs: Any) -> None:
        """Start cleaning."""
        await self.coordinator.async_send_named_command("start")

    async def async_pause(self, **kwargs: Any) -> None:
        """Pause cleaning."""
        await self.coordinator.async_send_named_command("pause")

    async def async_stop(self, **kwargs: Any) -> None:
        """Stop cleaning."""
        await self.coordinator.async_send_named_command("stop")

    async def async_return_to_base(self, **kwargs: Any) -> None:
        """Send the robot back to its dock."""
        await self.coordinator.async_send_named_command("return_to_base")

    async def async_locate(self, **kwargs: Any) -> None:
        """Make the robot announce its location."""
        await self.coordinator.async_send_named_command("locate")

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        """Set the suction power used by Home Assistant room clean buttons."""
        for value, name in FAN_SPEED_OPTIONS.items():
            if name == fan_speed:
                await self.coordinator.async_set_room_cleaning_option(
                    CONF_ROOM_FAN_SPEED,
                    value,
                )
                return

    async def async_send_command(
        self,
        command: str,
        params: dict[str, Any] | list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Send a raw command via MQTT."""
        await self.coordinator.async_send_raw_command(command, params)

    async def async_clean_rooms(self, rooms: list[str]) -> None:
        """Service handler: start a clean covering the named rooms."""
        try:
            missing = await self.coordinator.async_clean_rooms_by_name(rooms)
        except UpdateFailed as err:
            raise HomeAssistantError(str(err)) from err
        if missing:
            raise HomeAssistantError(
                f"These rooms were not found on the map: {', '.join(missing)}"
            )
