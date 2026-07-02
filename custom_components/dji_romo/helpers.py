"""Small shared helpers for DJI Romo entity platforms."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .coordinator import DjiRomoCoordinator


def setting_value(coordinator: DjiRomoCoordinator, *path: str) -> Any:
    """Return a value from the REST settings payload by nested key path."""
    current: Any = coordinator.data.cloud_data.get("settings", {})
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def truthy(value: Any) -> bool | None:
    """Coerce DJI's mix of bool/int flags into a tri-state boolean."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return None
