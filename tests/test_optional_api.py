"""Tests for optional DJI cloud endpoints."""

import asyncio

import pytest

from custom_components.dji_romo.client import DjiRomoApiError, DjiRomoAuthError
from custom_components.dji_romo.coordinator import _async_optional_api_call


def test_optional_api_error_uses_cached_value() -> None:
    """An unsupported optional endpoint must not block integration setup."""

    async def unavailable() -> dict:
        raise DjiRomoApiError("not found")

    cached = {"existing": True}

    assert (
        asyncio.run(_async_optional_api_call("settings", unavailable, cached)) is cached
    )


def test_optional_api_auth_error_is_not_hidden() -> None:
    """Authentication failures must still start Home Assistant reauth."""

    async def unauthorized() -> dict:
        raise DjiRomoAuthError("expired")

    with pytest.raises(DjiRomoAuthError, match="expired"):
        asyncio.run(_async_optional_api_call("settings", unauthorized, {}))
