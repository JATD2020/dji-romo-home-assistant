"""DJI Romo custom integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import DjiRomoApiClient
from .const import (
    CONF_API_URL,
    CONF_DEVICE_SN,
    CONF_LOCALE,
    CONF_USER_TOKEN,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import DjiRomoCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up DJI Romo from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    session = async_get_clientsession(hass)
    api = DjiRomoApiClient(
        session,
        entry.data[CONF_USER_TOKEN],
        device_sn=entry.data[CONF_DEVICE_SN],
        api_url=entry.options.get(CONF_API_URL, entry.data[CONF_API_URL]),
        locale=entry.options.get(CONF_LOCALE, entry.data[CONF_LOCALE]),
    )
    coordinator = DjiRomoCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: DjiRomoCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
