"""Tests for the DJI Romo map's displayed-job identity."""

from custom_components.dji_romo.coordinator import RomoSnapshot
from custom_components.dji_romo.image import _latest_known_uuid


def test_active_map_prefers_live_mqtt_mission() -> None:
    """The live MQTT mission stays authoritative while the robot is cleaning."""
    data = RomoSnapshot(
        activity="cleaning",
        mission_bid="live-mqtt-bid",
        last_job={"uuid": "previous-job"},
    )

    assert _latest_known_uuid(data) == "live-mqtt-bid"


def test_docked_keeps_live_mission_until_rest_catches_up() -> None:
    """A still-set MQTT mission_bid remains authoritative even when docked.

    The map only switches to the completed report once the REST ``last_job``
    matches, so a live bid that differs from the REST job must not be masked
    by the REST uuid here.
    """
    data = RomoSnapshot(
        activity="docked",
        mission_bid="stale-mqtt-bid",
        last_job={"uuid": "completed-job"},
    )

    assert _latest_known_uuid(data) == "stale-mqtt-bid"


def test_falls_back_to_rest_job_without_live_mission() -> None:
    """Without a live mission_bid, the REST last_job identifies the map."""
    data = RomoSnapshot(
        activity="docked",
        mission_bid=None,
        last_job={"uuid": "completed-job"},
    )

    assert _latest_known_uuid(data) == "completed-job"
