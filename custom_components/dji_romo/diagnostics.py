"""Diagnostics support for DJI Romo."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .privacy import DIAGNOSTIC_FIELDS_TO_REDACT


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    snapshot = _snapshot_summary(coordinator.data)
    last_cloud_success = coordinator.last_cloud_success_at

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), DIAGNOSTIC_FIELDS_TO_REDACT),
            "options": async_redact_data(
                dict(entry.options), DIAGNOSTIC_FIELDS_TO_REDACT
            ),
        },
        "device_info": async_redact_data(
            coordinator.device_info_payload, DIAGNOSTIC_FIELDS_TO_REDACT
        ),
        "coordinator": {
            "available": coordinator.available,
            "last_update_success": coordinator.last_update_success,
            "mqtt_connected": coordinator.mqtt_connected,
            "cloud_refresh_failures": coordinator.cloud_refresh_failures,
            "last_cloud_success": (
                last_cloud_success.isoformat() if last_cloud_success else None
            ),
        },
        "snapshot": async_redact_data(snapshot, DIAGNOSTIC_FIELDS_TO_REDACT),
    }


def _snapshot_summary(snapshot: Any) -> dict[str, Any]:
    """Return useful state without maps, coordinates, paths, or identifiers."""
    if snapshot is None:
        return {}

    scalar_fields = (
        "battery_level",
        "activity",
        "status_text",
        "cleaned_area",
        "fan_speed",
        "clean_mode",
        "water_level",
        "clean_num",
        "clean_speed",
        "online",
        "active_step",
        "total_cleanings",
        "drying_active",
        "drying_stage",
        "drying_percent",
        "drying_remaining_s",
        "clean_progress",
        "clean_duration_s",
        "clean_remaining_s",
        "charger_connected",
        "battery_care_active",
        "dust_bag_uv_enable",
        "hatch_status",
    )
    summary = {name: getattr(snapshot, name) for name in scalar_fields}
    for name in ("last_osd_at", "last_updated", "cloud_last_updated"):
        value = getattr(snapshot, name)
        summary[name] = value.isoformat() if value else None

    cloud_data = snapshot.cloud_data if isinstance(snapshot.cloud_data, dict) else {}
    consumables = cloud_data.get("consumables")
    summary["cloud_data"] = {
        "sections": sorted(cloud_data),
        "consumable_count": len(consumables) if isinstance(consumables, dict) else 0,
    }
    summary["rooms"] = {"count": len(snapshot.rooms)}
    summary["hms_alerts"] = {"count": len(snapshot.hms_alerts)}
    summary["trajectory"] = {"point_count": len(snapshot.trajectory)}
    summary["map"] = {
        "room_polygon_count": len(snapshot.floor_plan_polys),
        "carpet_count": len(snapshot.carpet_polys),
        "restricted_zone_count": len(snapshot.restricted_polys),
        "virtual_wall_count": len(snapshot.virtual_walls),
        "obstacle_count": len(snapshot.obstacles),
        "has_grid": bool(snapshot.grid_map_data),
        "has_last_clean_map": bool(snapshot.last_clean_map),
    }
    summary["active_job"] = _job_summary(snapshot.active_job)
    summary["last_job"] = _job_summary(snapshot.last_job)
    return summary


def _job_summary(job: Any) -> dict[str, Any]:
    """Return non-identifying fields from a cleaning job."""
    if not isinstance(job, dict):
        return {}
    keys = (
        "status",
        "cleaned_area",
        "clean_area",
        "duration",
        "job_duration",
        "start_time",
        "end_time",
        "created_at",
    )
    return {key: job[key] for key in keys if key in job}
