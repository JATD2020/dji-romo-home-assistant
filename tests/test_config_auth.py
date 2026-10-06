"""Tests for auth failures during config entry validation."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.dji_romo.client import DjiRomoApiError, DjiRomoAuthError
from custom_components.dji_romo.config_flow import (
    _discover_device_nickname,
    _validate_user_input,
)
from custom_components.dji_romo.const import (
    CONF_DEVICE_NAME,
    CONF_DEVICE_SN,
    CONF_USER_TOKEN,
    DEFAULT_API_URL,
    DEFAULT_LOCALE,
)


@pytest.mark.parametrize("device_sn", ["test-serial", None])
def test_device_discovery_auth_failure_is_not_ignored(device_sn) -> None:
    """An expired token cannot be accepted through the regional fallback."""
    with (
        patch("custom_components.dji_romo.config_flow.async_get_clientsession"),
        patch("custom_components.dji_romo.config_flow.DjiRomoApiClient") as client_type,
    ):
        client = client_type.return_value
        client.async_get_mqtt_credentials = AsyncMock()
        client.async_resolve_device = AsyncMock(side_effect=DjiRomoAuthError("expired"))
        client.async_get_properties = AsyncMock(return_value={})
        with pytest.raises(DjiRomoAuthError, match="expired"):
            asyncio.run(
                _validate_user_input(
                    None, {CONF_USER_TOKEN: "test-token", CONF_DEVICE_SN: device_sn}
                )
            )


@pytest.mark.parametrize("error", [DjiRomoAuthError("expired"), DjiRomoApiError("404")])
def test_nickname_lookup_only_ignores_optional_endpoint_errors(error) -> None:
    with patch(
        "custom_components.dji_romo.config_flow.DjiRomoApiClient"
    ) as client_type:
        client_type.return_value.async_get_properties = AsyncMock(side_effect=error)
        lookup = _discover_device_nickname(
            None, "test-token", "test-serial", DEFAULT_LOCALE, DEFAULT_API_URL
        )
        if isinstance(error, DjiRomoAuthError):
            with pytest.raises(DjiRomoAuthError, match="expired"):
                asyncio.run(lookup)
        else:
            assert asyncio.run(lookup) is None


def test_unavailable_homes_endpoint_still_accepts_known_serial() -> None:
    """Regions without homes discovery can keep using the properties fallback."""
    with (
        patch("custom_components.dji_romo.config_flow.async_get_clientsession"),
        patch("custom_components.dji_romo.config_flow.DjiRomoApiClient") as client_type,
    ):
        client = client_type.return_value
        client.async_get_mqtt_credentials = AsyncMock()
        client.async_resolve_device = AsyncMock(side_effect=DjiRomoApiError("404"))
        client.async_get_properties = AsyncMock(
            return_value={"device_base_info": {"name": "Romo"}}
        )

        data = asyncio.run(
            _validate_user_input(
                None, {CONF_USER_TOKEN: "test-token", CONF_DEVICE_SN: "test-serial"}
            )
        )

        assert data[CONF_DEVICE_SN] == "test-serial"
        assert data[CONF_DEVICE_NAME] == "Romo"
