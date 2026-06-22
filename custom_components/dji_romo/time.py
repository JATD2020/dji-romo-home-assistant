"""Time entities for DJI Romo schedule settings (writes the REST settings endpoint).

Each entity maps an hour/minute pair inside a settings object (e.g. the
Do-Not-Disturb window in ``no_disturb``) to a single HH:MM picker. Writes go
through the coordinator like the other settings entities: the whole parent object
is sent (preserving its sibling fields) under the coordinator's write lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Any

from homeassistant.components.time import TimeEntity, TimeEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import UpdateFailed

from .coordinator import DjiRomoCoordinator
from .entity import DjiRomoCoordinatorEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class DjiRomoSettingTimeDescription(TimeEntityDescription):
    """Describes an HH:MM setting stored as an hour+minute pair in an object."""

    obj_key: str  # nested settings object, e.g. "no_disturb"
    hour_key: str  # field holding the hour, e.g. "start_hour"
    minute_key: str  # field holding the minute, e.g. "start_minute"


def _setting(coordinator: DjiRomoCoordinator, *path: str) -> Any:
    """Return a value from the REST settings payload by nested key path."""
    current: Any = coordinator.data.cloud_data.get("settings", {})
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


TIMES: tuple[DjiRomoSettingTimeDescription, ...] = (
    DjiRomoSettingTimeDescription(
        key="dnd_start",
        translation_key="dnd_start",
        name="Do Not Disturb Start",
        icon="mdi:weather-night",
        entity_category=EntityCategory.CONFIG,
        obj_key="no_disturb",
        hour_key="start_hour",
        minute_key="start_minute",
    ),
    DjiRomoSettingTimeDescription(
        key="dnd_end",
        translation_key="dnd_end",
        name="Do Not Disturb End",
        icon="mdi:weather-sunny",
        entity_category=EntityCategory.CONFIG,
        obj_key="no_disturb",
        hour_key="end_hour",
        minute_key="end_minute",
    ),
    DjiRomoSettingTimeDescription(
        key="dust_collect_time",
        translation_key="dust_collect_time",
        name="Dust Collection Time",
        icon="mdi:clock-outline",
        entity_category=EntityCategory.CONFIG,
        # Only meaningful when Dust Collection Mode is Scheduled. Nested in
        # dust_collect; preserves collect_mode + week_repeat.
        obj_key="dust_collect",
        hour_key="start_hour",
        minute_key="start_minute",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Romo time entities."""
    coordinator = entry.runtime_data
    async_add_entities(DjiRomoSettingTime(coordinator, description) for description in TIMES)


class DjiRomoSettingTime(DjiRomoCoordinatorEntity, TimeEntity):
    """An hour/minute settings pair exposed as a single HH:MM picker."""

    entity_description: DjiRomoSettingTimeDescription

    def __init__(
        self,
        coordinator: DjiRomoCoordinator,
        description: DjiRomoSettingTimeDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_sn}_{description.key}"

    @property
    def native_value(self) -> time | None:
        """Return the stored HH:MM (None when not yet known)."""
        obj = _setting(self.coordinator, self.entity_description.obj_key)
        if not isinstance(obj, dict):
            return None
        hour = obj.get(self.entity_description.hour_key)
        minute = obj.get(self.entity_description.minute_key)
        if hour is None or minute is None:
            return None
        try:
            return time(hour=int(hour), minute=int(minute))
        except (TypeError, ValueError):
            return None

    async def async_set_value(self, value: time) -> None:
        """Write the new HH:MM, preserving the object's sibling fields."""
        desc = self.entity_description

        def build() -> dict[str, Any]:
            obj = _setting(self.coordinator, desc.obj_key) or {}
            return {
                desc.obj_key: {
                    **obj,
                    desc.hour_key: value.hour,
                    desc.minute_key: value.minute,
                }
            }

        try:
            # Builder evaluated under the coordinator's write lock (see switches).
            await self.coordinator.async_set_device_setting(build)
        except UpdateFailed as err:
            raise HomeAssistantError(
                f"Failed to set DJI Romo '{self.name}': {err}"
            ) from err
