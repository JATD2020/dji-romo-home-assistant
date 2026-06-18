"""Shared helpers to derive per-room cleaning configs from DJI shortcuts.

The DJI ``room_map`` lists rooms with a type label and a ``poly_index`` but no
geometry; a cleaning shortcut's ``plan_area_configs`` carries the per-room clean
settings. These helpers pick the most complete shortcut as a template and build
one normalized config per room, with a stable human-readable name. Used by both
the per-room buttons and the ``clean_rooms`` service so naming stays consistent.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

from .const import ROOM_LABELS


def room_template_shortcut(shortcuts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the shortcut whose room_map best describes every room.

    Among equally complete shortcuts, prefer a vacuum-only program because its
    per-room defaults are the safest template.
    """

    def room_count(shortcut: dict[str, Any]) -> int:
        room_map = shortcut.get("room_map", {})
        return len(room_map.get("device_map_rooms", []))

    candidates = [s for s in shortcuts if room_count(s) > 0]
    if not candidates:
        return shortcuts[0] if shortcuts else None

    def sort_key(shortcut: dict[str, Any]) -> tuple[int, int]:
        configs = shortcut.get("plan_area_configs", [])
        vacuum_only = bool(configs) and all(
            config.get("clean_mode") == 2 for config in configs
        )
        return (room_count(shortcut), 1 if vacuum_only else 0)

    return max(candidates, key=sort_key)


def room_configs_from_shortcuts(
    shortcuts: list[dict[str, Any]],
) -> Iterable[tuple[dict[str, Any], dict[str, Any], set[int]]]:
    """Build one room-clean entry (config, room_map, duplicate_labels) per room."""
    template = room_template_shortcut(shortcuts)
    if not template:
        return ()
    room_map = template.get("room_map", {})
    rooms = room_map.get("device_map_rooms", [])
    configs = {
        config.get("poly_index"): config
        for config in template.get("plan_area_configs", [])
        if config.get("poly_index") is not None
    }
    all_configs: list[dict[str, Any]] = []
    for index, room in enumerate(sorted(rooms, key=_room_sort_key), start=1):
        poly_index = room.get("poly_index")
        config = dict(configs.get(poly_index) or room)
        config.setdefault("order_id", index)
        config.setdefault("clean_mode", 2)
        config.setdefault("fan_speed", 2)
        config.setdefault("water_level", 2)
        config.setdefault("clean_num", 1)
        config.setdefault("clean_speed", 2)
        all_configs.append(config)

    # Find label IDs that appear more than once so room_name can number them.
    label_counts = Counter(_effective_label_id(c) for c in all_configs)
    duplicate_labels = {label for label, count in label_counts.items() if count > 1}

    return [(config, room_map, duplicate_labels) for config in all_configs]


def duplicate_label_ids(rooms: Iterable[dict[str, Any]]) -> set[int]:
    """Return room-type label IDs shared by more than one room.

    Used so the same label appearing on several rooms gets numbered ("Bathroom1",
    "Bathroom2"). Works on any dicts carrying user_label/poly_label (device_map_rooms,
    seg_map poly_info, or plan configs).
    """
    counts = Counter(_effective_label_id(r) for r in rooms)
    return {label for label, count in counts.items() if count > 1}


def room_name(room_config: dict[str, Any], duplicate_labels: set[int]) -> str:
    """Return a stable, human-readable name for a room config."""
    custom_name = str(room_config.get("custom_name") or "").strip()
    if custom_name:
        return custom_name
    label_id = _effective_label_id(room_config)
    base_name = ROOM_LABELS.get(label_id, f"Room {room_config.get('poly_index')}")
    # When several rooms share the same label (e.g. two Bathrooms), number all of
    # them ("Bathroom1", "Bathroom2") matching exactly what the DJI app shows.
    if label_id in duplicate_labels:
        name_index = room_config.get("poly_name_index")
        try:
            name_index = int(name_index)
        except (TypeError, ValueError):
            name_index = 0
        return f"{base_name}{name_index + 1}"
    return base_name


def _room_sort_key(room: dict[str, Any]) -> tuple[int, int]:
    order_id = room.get("order_id")
    return (
        int(order_id) if isinstance(order_id, int) and order_id >= 0 else 999,
        int(room.get("poly_index") or 0),
    )


def _effective_label_id(room_config: dict[str, Any]) -> int:
    """Return the label ID used for room-name lookup.

    ``user_label == -1`` means DJI auto-assigned the label; ``poly_label`` then
    holds the same room-type ID (same ROOM_LABELS numbering).
    """
    label = room_config.get("user_label")
    try:
        label_id = int(label)
    except (TypeError, ValueError):
        label_id = 0
    if label_id == -1:
        poly_label = room_config.get("poly_label")
        try:
            label_id = int(poly_label)
        except (TypeError, ValueError):
            label_id = 0
    return label_id
