"""Tests for validating room requests before starting the robot."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.dji_romo.coordinator import DjiRomoCoordinator


def _coordinator():
    api = SimpleNamespace(
        async_get_shortcuts=AsyncMock(
            return_value=[
                {
                    "room_map": {
                        "map_index": 10,
                        "device_map_rooms": [
                            {"poly_index": 1, "custom_name": "Kitchen"},
                            {"poly_index": 2, "custom_name": "Hall"},
                        ],
                    },
                    "plan_area_configs": [],
                }
            ]
        ),
        async_start_rooms=AsyncMock(),
    )
    coordinator = object.__new__(DjiRomoCoordinator)
    coordinator.api = api
    coordinator.entry = SimpleNamespace(data={}, options={})
    return coordinator


def test_unknown_room_prevents_partial_clean() -> None:
    """A misspelled room must not leave the robot running a partial request."""
    coordinator = _coordinator()

    missing = asyncio.run(coordinator.async_clean_rooms_by_name(["Kitchen", "Hlal"]))

    assert missing == ["Hlal"]
    coordinator.api.async_start_rooms.assert_not_awaited()


def test_all_unknown_rooms_return_names_without_starting() -> None:
    coordinator = _coordinator()

    missing = asyncio.run(coordinator.async_clean_rooms_by_name(["Unknown"]))

    assert missing == ["Unknown"]
    coordinator.api.async_start_rooms.assert_not_awaited()


def test_valid_rooms_start_in_requested_order() -> None:
    coordinator = _coordinator()

    assert (
        asyncio.run(coordinator.async_clean_rooms_by_name([" Hall ", "kitchen"])) == []
    )

    configs, room_map, name = coordinator.api.async_start_rooms.call_args.args
    assert [room["poly_index"] for room in configs] == [2, 1]
    assert room_map["map_index"] == 10
    assert name == "Hall + kitchen"
