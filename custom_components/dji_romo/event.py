"""Event entity for DJI Romo health-management (HMS) alerts."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DjiRomoCoordinator
from .entity import DjiRomoCoordinatorEntity

PARALLEL_UPDATES = 0

EVENT_ALERT = "alert"
EVENT_CLEARED = "cleared"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Romo HMS event entity."""
    coordinator = entry.runtime_data
    async_add_entities([DjiRomoHmsEvent(coordinator)])


class DjiRomoHmsEvent(DjiRomoCoordinatorEntity, EventEntity):
    """Fires when the robot raises or clears a health-management alert."""

    _attr_translation_key = "hms"
    _attr_icon = "mdi:robot-vacuum-alert"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_event_types = [EVENT_ALERT, EVENT_CLEARED]

    def __init__(self, coordinator: DjiRomoCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Health Alert"
        self._attr_unique_id = f"{coordinator.device_sn}_hms"
        self._previous_alerts: list[dict[str, Any]] = list(coordinator.data.hms_alerts)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Trigger an event when the HMS alert list changes."""
        alerts = self.coordinator.data.hms_alerts
        if alerts != self._previous_alerts:
            if alerts:
                self._trigger_event(
                    EVENT_ALERT,
                    {"alerts": alerts, "count": len(alerts)},
                )
            else:
                self._trigger_event(EVENT_CLEARED, {"count": 0})
            self._previous_alerts = list(alerts)
        super()._handle_coordinator_update()
