"""DJI Romo custom integration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import DjiRomoApiClient
from .const import (
    CONF_API_URL,
    CONF_DEVICE_SN,
    CONF_LOCALE,
    CONF_USER_TOKEN,
    PLATFORMS,
)
from .coordinator import DjiRomoConfigEntry, DjiRomoCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: DjiRomoConfigEntry) -> bool:
    """Set up DJI Romo from a config entry."""
    session = async_get_clientsession(hass)
    api = DjiRomoApiClient(
        session,
        entry.data[CONF_USER_TOKEN],
        device_sn=entry.data[CONF_DEVICE_SN],
        api_url=entry.options.get(CONF_API_URL, entry.data[CONF_API_URL]),
        locale=entry.options.get(CONF_LOCALE, entry.data[CONF_LOCALE]),
    )
    coordinator = DjiRomoCoordinator(hass, entry, api)
    # Load persisted state (trajectory) before the first refresh so the map is
    # seeded immediately after a restart.
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # NOTE: intentionally NO add_update_listener here. Routine settings (room
    # cleaning options / fan speed) write to entry.data via async_update_entry;
    # an update listener would turn every such write into a full reload, dropping
    # the MQTT session. Options-flow changes therefore need a manual reload.
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DjiRomoConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: DjiRomoConfigEntry) -> None:
    """Reload the config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
