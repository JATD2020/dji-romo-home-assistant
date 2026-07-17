"""DJI Romo custom integration."""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .cleaning import migrate_legacy_entry_values
from .client import DjiRomoApiClient
from .const import (
    CONF_API_URL,
    CONF_COMMAND_MAPPING,
    CONF_COMMAND_TOPIC,
    CONF_DEVICE_NAME,
    CONF_DEVICE_SN,
    CONF_LOCALE,
    CONF_SUBSCRIPTION_TOPICS,
    CONF_USER_TOKEN,
    DEFAULT_API_URL,
    PLATFORMS,
)
from .coordinator import DjiRomoConfigEntry, DjiRomoCoordinator
from .validation import validate_api_url

_LOGGER = logging.getLogger(__name__)

_RELOAD_OPTION_KEYS = (
    CONF_API_URL,
    CONF_COMMAND_MAPPING,
    CONF_COMMAND_TOPIC,
    CONF_DEVICE_NAME,
    CONF_LOCALE,
    CONF_SUBSCRIPTION_TOPICS,
    CONF_USER_TOKEN,
)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate stored room-cleaning values to the confirmed DJI API values."""
    if entry.version > 2:
        _LOGGER.error(
            "Cannot migrate DJI Romo config entry from unsupported version %s",
            entry.version,
        )
        return False
    if entry.version == 2:
        return True

    data, options = migrate_legacy_entry_values(entry.data, entry.options)
    hass.config_entries.async_update_entry(
        entry,
        data=data,
        options=options,
        version=2,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DjiRomoConfigEntry) -> bool:
    """Set up DJI Romo from a config entry."""
    session = async_get_clientsession(hass)
    configured_api_url = entry.options.get(
        CONF_API_URL,
        entry.data.get(CONF_API_URL, DEFAULT_API_URL),
    )
    try:
        api_url = validate_api_url(configured_api_url)
    except ValueError:
        _LOGGER.error(
            "Ignoring unsafe DJI Romo API URL and using the default DJI endpoint"
        )
        api_url = DEFAULT_API_URL
    api = DjiRomoApiClient(
        session,
        entry.data[CONF_USER_TOKEN],
        device_sn=entry.data[CONF_DEVICE_SN],
        api_url=api_url,
        locale=entry.options.get(CONF_LOCALE, entry.data[CONF_LOCALE]),
    )
    coordinator = DjiRomoCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(
        entry.add_update_listener(_options_update_listener(_reload_options(entry)))
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DjiRomoConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _reload_options(entry: ConfigEntry) -> dict[str, object]:
    """Return options that require rebuilding the API or MQTT clients."""
    return {
        key: entry.options.get(key, entry.data.get(key)) for key in _RELOAD_OPTION_KEYS
    }


def _options_update_listener(
    initial: dict[str, object],
) -> Callable[[HomeAssistant, ConfigEntry], Coroutine[Any, Any, None]]:
    """Build a listener that ignores live room-cleaning option writes."""

    async def _async_options_updated(
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        if _reload_options(entry) != initial:
            await hass.config_entries.async_reload(entry.entry_id)

    return _async_options_updated
