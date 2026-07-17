"""Tests for building room-cleaning configs from DJI shortcuts."""

from custom_components.dji_romo.rooms import room_configs_from_shortcuts, room_name


def test_room_metadata_is_preserved_when_plan_config_is_sparse() -> None:
    """Cleaning values must not replace the room's label and custom name."""
    shortcuts = [
        {
            "room_map": {
                "device_map_rooms": [
                    {
                        "poly_index": 7,
                        "order_id": "2",
                        "user_label": 1,
                        "custom_name": "Bijkeuken",
                    }
                ]
            },
            "plan_area_configs": [
                {
                    "poly_index": 7,
                    "clean_mode": 2,
                    "fan_speed": 3,
                }
            ],
        }
    ]

    entries = list(room_configs_from_shortcuts(shortcuts))
    config, _room_map, duplicate_labels = entries[0]

    assert config["custom_name"] == "Bijkeuken"
    assert config["user_label"] == 1
    assert config["fan_speed"] == 3
    assert config["clean_speed"] == 0
    assert room_name(config, duplicate_labels) == "Bijkeuken"


def test_rooms_are_sorted_when_order_ids_are_strings() -> None:
    """DJI JSON string order values should retain app room order."""
    shortcuts = [
        {
            "room_map": {
                "device_map_rooms": [
                    {"poly_index": 2, "order_id": "2", "user_label": 2},
                    {"poly_index": 1, "order_id": "1", "user_label": 1},
                ]
            },
            "plan_area_configs": [],
        }
    ]

    entries = list(room_configs_from_shortcuts(shortcuts))

    assert [entry[0]["poly_index"] for entry in entries] == [1, 2]
