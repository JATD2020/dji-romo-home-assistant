"""Cleaning option values used by the DJI Home API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import (
    CONF_ROOM_CLEAN_MODE,
    CONF_ROOM_CLEAN_NUM,
    CONF_ROOM_CLEAN_SPEED,
    CONF_ROOM_FAN_SPEED,
    CONF_ROOM_WATER_LEVEL,
)

ROOM_CLEANING_OPTION_KEYS = (
    CONF_ROOM_CLEAN_MODE,
    CONF_ROOM_FAN_SPEED,
    CONF_ROOM_WATER_LEVEL,
    CONF_ROOM_CLEAN_NUM,
    CONF_ROOM_CLEAN_SPEED,
)

# Values verified against DJI Home room plans and completed test jobs. Value 0 is
# not a valid clean_mode and causes the robot to abort during its preflight check.
ROOM_CLEAN_MODE_OPTIONS: dict[str, int] = {
    "Vacuum and Mop": 1,
    "Vacuum Only": 2,
    "Mop Only": 3,
    "Vacuum then Mop": 4,
}

# DJI calls clean_speed the route: it controls path density, not mop speed.
ROOM_ROUTE_OPTIONS: dict[str, int] = {
    "Standard": 0,
    "Fast": 1,
    "Fine": 2,
}

CLEAN_MODE_LABELS = {value: label for label, value in ROOM_CLEAN_MODE_OPTIONS.items()}
ROUTE_LABELS = {value: label for label, value in ROOM_ROUTE_OPTIONS.items()}

# Version 1 exposed incorrect labels and values. Preserve the user's apparent
# intent while converting stored selections to the confirmed API values.
_LEGACY_MODE_VALUES = {0: 4, 1: 1, 2: 2, 3: 3, 4: 4}
_LEGACY_ROUTE_VALUES = {
    1: 2,  # Slow -> Fine
    2: 0,  # Standard -> Standard
    3: 1,  # Fast -> Fast
}


def migrate_legacy_room_options(
    values: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Return config values with version 1 room options converted."""
    migrated = dict(values)
    changed = False

    for key, value_map, fallback in (
        (CONF_ROOM_CLEAN_MODE, _LEGACY_MODE_VALUES, 2),
        (CONF_ROOM_CLEAN_SPEED, _LEGACY_ROUTE_VALUES, 0),
    ):
        if key not in migrated:
            continue
        try:
            old_value = int(migrated[key])
        except (TypeError, ValueError):
            new_value = fallback
        else:
            new_value = value_map.get(old_value, fallback)
        if migrated[key] != new_value:
            migrated[key] = new_value
            changed = True

    return migrated, changed


def migrate_legacy_entry_values(
    data: Mapping[str, Any],
    options: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Move all effective legacy room options atomically into entry options."""
    effective = {
        key: options[key] if key in options else data[key]
        for key in ROOM_CLEANING_OPTION_KEYS
        if key in options or key in data
    }
    migrated, _ = migrate_legacy_room_options(effective)

    new_data = {
        key: value
        for key, value in data.items()
        if key not in ROOM_CLEANING_OPTION_KEYS
    }
    new_options = {
        key: value
        for key, value in options.items()
        if key not in ROOM_CLEANING_OPTION_KEYS
    }
    new_options.update(migrated)
    return new_data, new_options
