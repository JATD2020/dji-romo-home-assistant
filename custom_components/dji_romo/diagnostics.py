"""Diagnostics support for DJI Romo."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_USER_TOKEN

TO_REDACT = {
    CONF_USER_TOKEN,
    "user_token",
    "password",
    "username",
    "user_uuid",
    "client_id",
    "device_ip",
    "mac_address",
    "file_url",
    "maintain_url",
    "x-amz-server-side-encryption-customer-key",
    "x-amz-server-side-encryption-customer-key-MD5",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    snapshot: dict[str, Any] = {}
    if coordinator.data is not None and is_dataclass(coordinator.data):
        snapshot = asdict(coordinator.data)

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "device_info": async_redact_data(
            coordinator.device_info_payload, TO_REDACT
        ),
        "available": coordinator.available,
        "snapshot": async_redact_data(snapshot, TO_REDACT),
    }
