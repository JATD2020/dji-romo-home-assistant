"""Tests for starting saved DJI Home cleaning plans."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from custom_components.dji_romo.client import DjiRomoApiClient, DjiRomoApiError


def test_shortcut_keeps_area_geometry_and_map_metadata() -> None:
    """Starting a saved area must retain the fields returned by DJI."""
    shortcut = {
        "plan_uuid": "saved-plan",
        "plan_name": "Kitchen area",
        "plan_type": 3,
        "clean_area_type": 1,
        "area_config_type": 1,
        "plan_area_configs": [
            {
                "config_uuid": "saved-config",
                "poly_type": 1,
                "poly_index": 4,
                "vertices": [
                    {"x": 0, "y": 0},
                    {"x": 1, "y": 0},
                    {"x": 1, "y": 1},
                    {"x": 0, "y": 1},
                ],
                "skip_area": 1,
                "clean_mode": 3,
                "order_id": 2,
            }
        ],
        "room_map": {
            "map_index": 10,
            "map_version": 2,
            "file_id": "map-file",
            "slot_id": 0,
            "device_map_rooms": [{"poly_index": 4, "user_label": 7}],
        },
    }
    original = deepcopy(shortcut)
    client = DjiRomoApiClient(None, "test-token", device_sn="test-serial")
    client._device_request = AsyncMock()

    asyncio.run(client.async_start_shortcut(shortcut))

    method, path = client._device_request.call_args.args
    body = client._device_request.call_args.kwargs["json"]
    data = body["data"]
    area = data["plan_area_configs"][0]
    assert (method, path) == ("POST", "jobs/cleans/start")
    assert body["method"] == "room_clean"
    assert data["plan_uuid"] == "saved-plan"
    assert data["clean_area_type"] == 1
    assert data["area_config_type"] == 1
    assert data["room_map"] == shortcut["room_map"]
    for key, value in shortcut["plan_area_configs"][0].items():
        if key != "config_uuid":
            assert area[key] == value
    assert area["config_uuid"] != "saved-config"
    assert area["fan_speed"] == 2
    assert shortcut == original
    area["vertices"][0]["x"] = 5
    assert shortcut == original


def test_room_shortcut_retains_existing_defaults() -> None:
    """Sparse room plans still receive the established start-job defaults."""
    client = DjiRomoApiClient(None, "test-token", device_sn="test-serial")
    client._device_request = AsyncMock()

    asyncio.run(client.async_start_shortcut({"plan_area_configs": [{"poly_index": 7}]}))

    data = client._device_request.call_args.kwargs["json"]["data"]
    area = data["plan_area_configs"][0]
    assert area["poly_index"] == 7
    assert area["poly_type"] == 2
    assert area["skip_area"] == 0
    assert area["clean_mode"] == 2
    assert area["clean_num"] == 1
    assert data["room_map"] == {
        "map_index": 0,
        "map_version": 0,
        "file_id": "",
        "slot_id": 0,
    }


@pytest.mark.parametrize("configs", [None, [], [None], ["invalid"], {"poly_index": 7}])
def test_invalid_plan_does_not_send_a_cleaning_job(configs) -> None:
    """Malformed saved plans must fail before submitting any command."""
    client = DjiRomoApiClient(None, "test-token", device_sn="test-serial")
    client._device_request = AsyncMock()

    with pytest.raises(DjiRomoApiError):
        asyncio.run(client.async_start_shortcut({"plan_area_configs": configs}))

    client._device_request.assert_not_awaited()


def test_invalid_map_does_not_send_a_cleaning_job() -> None:
    client = DjiRomoApiClient(None, "test-token", device_sn="test-serial")
    client._device_request = AsyncMock()

    with pytest.raises(DjiRomoApiError, match="invalid map data"):
        asyncio.run(
            client.async_start_shortcut({"plan_area_configs": [{}], "room_map": None})
        )

    client._device_request.assert_not_awaited()
