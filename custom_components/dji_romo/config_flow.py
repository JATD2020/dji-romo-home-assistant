"""Config flow for DJI Romo."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig

from .client import DjiRomoApiClient, DjiRomoApiError
from .const import (
    CONF_API_URL,
    CONF_COMMAND_MAPPING,
    CONF_COMMAND_TOPIC,
    CONF_CREDENTIALS_TEXT,
    CONF_DEVICE_NAME,
    CONF_DEVICE_SN,
    CONF_LOCALE,
    CONF_SUBSCRIPTION_TOPICS,
    CONF_USER_TOKEN,
    DEFAULT_API_URL,
    DEFAULT_COMMAND_MAPPING_JSON,
    DEFAULT_COMMAND_TOPIC,
    DEFAULT_LOCALE,
    DEFAULT_SUBSCRIPTION_TOPICS,
    DOMAIN,
)


class DjiRomoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for DJI Romo."""

    VERSION = 1
    _reauth_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                data = await _validate_user_input(self.hass, user_input)
            except DjiRomoApiError:
                errors["base"] = "cannot_connect"
            except CannotDiscoverDeviceError:
                errors["base"] = "cannot_discover_device"
            except MissingTokenError:
                errors["base"] = "missing_token"
            else:
                await self.async_set_unique_id(data[CONF_DEVICE_SN])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=data[CONF_DEVICE_NAME],
                    data=data,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_CREDENTIALS_TEXT): TextSelector(
                        TextSelectorConfig(multiline=True)
                    ),
                    vol.Optional(CONF_USER_TOKEN): str,
                    vol.Optional(CONF_DEVICE_SN): str,
                    vol.Optional(CONF_NAME): str,
                    vol.Optional(CONF_LOCALE, default=DEFAULT_LOCALE): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],
    ) -> config_entries.ConfigFlowResult:
        """Handle an expired DJI Home token."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Ask for a fresh token or pasted extractor output."""
        errors: dict[str, str] = {}

        if user_input is not None and self._reauth_entry is not None:
            merged_input = {
                CONF_DEVICE_SN: self._reauth_entry.data[CONF_DEVICE_SN],
                CONF_NAME: self._reauth_entry.data[CONF_DEVICE_NAME],
                CONF_LOCALE: self._reauth_entry.data.get(CONF_LOCALE, DEFAULT_LOCALE),
                **user_input,
            }
            try:
                data = await _validate_user_input(self.hass, merged_input)
            except DjiRomoApiError:
                errors["base"] = "cannot_connect"
            except CannotDiscoverDeviceError:
                errors["base"] = "cannot_discover_device"
            except MissingTokenError:
                errors["base"] = "missing_token"
            else:
                if data[CONF_DEVICE_SN] != self._reauth_entry.data[CONF_DEVICE_SN]:
                    errors["base"] = "wrong_device"
                else:
                    self.hass.config_entries.async_update_entry(
                        self._reauth_entry,
                        data={**self._reauth_entry.data, **data},
                    )
                    await self.hass.config_entries.async_reload(
                        self._reauth_entry.entry_id
                    )
                    return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_CREDENTIALS_TEXT): TextSelector(
                        TextSelectorConfig(multiline=True)
                    ),
                    vol.Optional(CONF_USER_TOKEN): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return DjiRomoOptionsFlow(config_entry)


class DjiRomoOptionsFlow(config_entries.OptionsFlow):
    """Edit advanced settings for DJI Romo."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Manage options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                command_mapping = json.loads(user_input[CONF_COMMAND_MAPPING])
                subscription_topics = [
                    topic.strip()
                    for topic in user_input[CONF_SUBSCRIPTION_TOPICS].splitlines()
                    if topic.strip()
                ]
            except json.JSONDecodeError:
                errors[CONF_COMMAND_MAPPING] = "invalid_json"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_DEVICE_NAME: user_input[CONF_DEVICE_NAME],
                        CONF_API_URL: user_input[CONF_API_URL],
                        CONF_LOCALE: user_input[CONF_LOCALE],
                        CONF_COMMAND_TOPIC: user_input[CONF_COMMAND_TOPIC],
                        CONF_SUBSCRIPTION_TOPICS: subscription_topics,
                        CONF_COMMAND_MAPPING: command_mapping,
                    },
                )

        current = self._config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICE_NAME,
                        default=current.get(
                            CONF_DEVICE_NAME,
                            self._config_entry.data[CONF_DEVICE_NAME],
                        ),
                    ): str,
                    vol.Required(
                        CONF_API_URL,
                        default=current.get(CONF_API_URL, DEFAULT_API_URL),
                    ): str,
                    vol.Required(
                        CONF_LOCALE,
                        default=current.get(
                            CONF_LOCALE,
                            self._config_entry.data.get(CONF_LOCALE, DEFAULT_LOCALE),
                        ),
                    ): str,
                    vol.Required(
                        CONF_COMMAND_TOPIC,
                        default=current.get(
                            CONF_COMMAND_TOPIC,
                            self._config_entry.data.get(
                                CONF_COMMAND_TOPIC,
                                DEFAULT_COMMAND_TOPIC,
                            ),
                        ),
                    ): str,
                    vol.Required(
                        CONF_SUBSCRIPTION_TOPICS,
                        default="\n".join(
                            current.get(
                                CONF_SUBSCRIPTION_TOPICS,
                                self._config_entry.data.get(
                                    CONF_SUBSCRIPTION_TOPICS,
                                    DEFAULT_SUBSCRIPTION_TOPICS,
                                ),
                            )
                        ),
                    ): str,
                    vol.Required(
                        CONF_COMMAND_MAPPING,
                        default=json.dumps(
                            current.get(
                                CONF_COMMAND_MAPPING,
                                self._config_entry.data.get(
                                    CONF_COMMAND_MAPPING,
                                    json.loads(DEFAULT_COMMAND_MAPPING_JSON),
                                ),
                            ),
                            indent=2,
                            sort_keys=True,
                        ),
                    ): str,
                }
            ),
            errors=errors,
        )


async def _validate_user_input(
    hass,
    user_input: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the token and resolve a Romo device."""
    parsed_credentials = _parse_credentials_text(
        str(user_input.get(CONF_CREDENTIALS_TEXT) or "")
    )
    user_token = str(
        user_input.get(CONF_USER_TOKEN) or parsed_credentials.get(CONF_USER_TOKEN) or ""
    ).strip()
    if not user_token:
        raise MissingTokenError

    session = async_get_clientsession(hass)
    locale = (
        user_input.get(CONF_LOCALE)
        or parsed_credentials.get(CONF_LOCALE)
        or DEFAULT_LOCALE
    )
    api_url = parsed_credentials.get(CONF_API_URL) or DEFAULT_API_URL
    client = DjiRomoApiClient(
        session,
        user_token,
        api_url=api_url,
        locale=locale,
    )
    # Validate the token using the endpoint we know is working.
    await client.async_get_mqtt_credentials()

    requested_sn = user_input.get(CONF_DEVICE_SN) or parsed_credentials.get(CONF_DEVICE_SN) or None
    device_name = user_input.get(CONF_NAME)
    device_sn = requested_sn

    if requested_sn is not None:
        try:
            device = await client.async_resolve_device(requested_sn)
        except DjiRomoApiError:
            # Some accounts/regions do not expose a usable homes endpoint yet.
            # If the user already knows the serial number, allow setup to continue.
            device = {
                "sn": requested_sn,
                "name": device_name or f"Romo {requested_sn}",
            }
        device_sn = device["sn"]
        device_name = device_name or device.get("name") or f"Romo {device_sn}"
    else:
        try:
            device = await client.async_resolve_device(None)
        except DjiRomoApiError as err:
            raise CannotDiscoverDeviceError from err
        device_sn = device["sn"]
        device_name = device_name or device.get("name") or f"Romo {device_sn}"

    return {
        CONF_USER_TOKEN: user_token,
        CONF_DEVICE_SN: device_sn,
        CONF_DEVICE_NAME: device_name,
        CONF_LOCALE: locale,
        CONF_API_URL: api_url,
        CONF_COMMAND_TOPIC: DEFAULT_COMMAND_TOPIC,
        CONF_SUBSCRIPTION_TOPICS: DEFAULT_SUBSCRIPTION_TOPICS,
        CONF_COMMAND_MAPPING: json.loads(DEFAULT_COMMAND_MAPPING_JSON),
    }


def _parse_credentials_text(raw: str) -> dict[str, str]:
    """Parse .env or dji_credentials.txt content from the extractor."""
    credentials: dict[str, str] = {}
    aliases = {
        "DJI_USER_TOKEN": CONF_USER_TOKEN,
        "USER_TOKEN": CONF_USER_TOKEN,
        "DJI_DEVICE_SN": CONF_DEVICE_SN,
        "DEVICE_SN": CONF_DEVICE_SN,
        "DJI_API_URL": CONF_API_URL,
        "API_URL": CONF_API_URL,
        "DJI_LOCALE": CONF_LOCALE,
        "LOCALE": CONF_LOCALE,
    }
    label_aliases = {
        "user token": CONF_USER_TOKEN,
        "device sn": CONF_DEVICE_SN,
        "robot serial": CONF_DEVICE_SN,
        "device serial": CONF_DEVICE_SN,
        "api url": CONF_API_URL,
        "locale": CONF_LOCALE,
    }

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key, value = stripped.split("=", 1)
            normalized = aliases.get(key.strip())
            if normalized:
                credentials[normalized] = value.strip().strip("\"'")
            continue
        if ":" in stripped:
            key, value = stripped.split(":", 1)
            normalized = label_aliases.get(key.strip().lower())
            if normalized:
                credentials[normalized] = value.strip().strip("\"'")

    return credentials


class CannotDiscoverDeviceError(Exception):
    """Raised when token validation works but serial discovery does not."""


class MissingTokenError(Exception):
    """Raised when no token was entered or pasted."""
