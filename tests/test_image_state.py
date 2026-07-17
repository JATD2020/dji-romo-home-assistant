"""Tests for stable DJI Romo map image state."""

from custom_components.dji_romo.coordinator import RomoSnapshot
from custom_components.dji_romo.image import _display_job_uuid


def test_docked_map_ignores_stale_mqtt_mission() -> None:
    """A completed map is keyed by the REST job while the robot is docked."""
    data = RomoSnapshot(
        activity="docked",
        mission_bid="stale-mqtt-bid",
        last_job={"uuid": "completed-job"},
    )

    assert _display_job_uuid(data, is_active=False) == "completed-job"


def test_active_map_prefers_live_mqtt_mission() -> None:
    """The live mission remains authoritative during cleaning."""
    data = RomoSnapshot(
        activity="cleaning",
        mission_bid="live-mqtt-bid",
        last_job={"uuid": "previous-job"},
    )

    assert _display_job_uuid(data, is_active=True) == "live-mqtt-bid"
