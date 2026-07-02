"""Shared entity helpers for DJI Romo."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DjiRomoCoordinator


class DjiRomoCoordinatorEntity(CoordinatorEntity[DjiRomoCoordinator]):
    """Base entity bound to the Romo coordinator."""

    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Build device info fresh so model/firmware/name reflect later refreshes."""
        payload = self.coordinator.device_info_payload
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.device_sn)},
            manufacturer="DJI",
            model=payload.get("model") or "Romo",
            name=self.coordinator.device_name,
            serial_number=self.coordinator.device_sn,
            sw_version=payload.get("firmware"),
            configuration_url="https://home-api-vg.djigate.com/",
        )

    @property
    def available(self) -> bool:
        """Entities follow the robot's reachability, not just the REST poll."""
        return super().available and self.coordinator.available

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Base attributes shared by all entities (subclasses extend this)."""
        return {}
