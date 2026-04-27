"""Vacuum platform for DJI Romo."""

from __future__ import annotations

from typing import Any

from homeassistant.components.vacuum import StateVacuumEntity
from homeassistant.components.vacuum.const import VacuumActivity, VacuumEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    ATTR_LAST_TOPIC,
    ATTR_LAST_UPDATED,
    ATTR_RAW_STATE,
    ATTR_SELECTED_TOPIC,
    CONF_ROOM_FAN_SPEED,
    DOMAIN,
)
from .coordinator import DjiRomoCoordinator
from .entity import DjiRomoCoordinatorEntity

FAN_SPEED_OPTIONS = {
    1: "Stil",
    2: "Standaard",
    3: "Max",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the DJI Romo vacuum entity."""
    coordinator: DjiRomoCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DjiRomoVacuum(coordinator)])


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
        """Return the vacuum activity."""
        return VacuumActivity(self.coordinator.data.activity)

    @property
    def fan_speed(self) -> str | None:
        """Return best-effort fan/suction mode."""
        value = (
            self.coordinator.data.fan_speed
            or self.coordinator.room_cleaning_options[CONF_ROOM_FAN_SPEED]
        )
        return FAN_SPEED_OPTIONS.get(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose parsed and raw payload fragments for debugging."""
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
        attrs[ATTR_RAW_STATE] = self.coordinator.data.raw_state
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
