"""Tests for interpreting DJI Romo activity payloads."""

from custom_components.dji_romo.coordinator import (
    RomoSnapshot,
    _flatten_dict,
    _gate_event_activity,
    _infer_property_activity,
    _meaningful_state_changed,
)


def _device_osd(*, mission_status: int, charger_connected: int) -> dict:
    """Build the property payload shape emitted by the robot."""
    return _flatten_dict(
        {
            "method": "device_osd",
            "data": {
                "host": {
                    "mission_status": mission_status,
                    "charger_connected": charger_connected,
                }
            },
        }
    )


def test_charger_flag_wins_over_stale_cleaning_status() -> None:
    """A stale mission status must not show a docked robot as cleaning."""
    flattened = _device_osd(mission_status=2, charger_connected=1)

    assert _infer_property_activity(flattened, "2", "cleaning") == "docked"


def test_cleaning_status_is_used_after_robot_leaves_dock() -> None:
    """Mission status 2 means cleaning once the charger is disconnected."""
    flattened = _device_osd(mission_status=2, charger_connected=0)

    assert _infer_property_activity(flattened, "2", "docked") == "cleaning"


def test_brush_clean_and_drying_statuses_are_recognized() -> None:
    """Known mission statuses 5 and 8 map to their Home Assistant activity."""
    brush_clean = _device_osd(mission_status=5, charger_connected=0)
    drying = _device_osd(mission_status=8, charger_connected=0)

    assert _infer_property_activity(brush_clean, "5", "idle") == "cleaning"
    assert _infer_property_activity(drying, "8", "idle") == "docked"


def test_delayed_active_event_is_rejected_while_charging() -> None:
    """Stale progress events cannot restart an already docked activity."""
    assert _gate_event_activity("cleaning", 1) is None
    assert _gate_event_activity("paused", 1) is None
    assert _gate_event_activity("returning", 1) is None
    assert _gate_event_activity("error", 1) == "error"
    assert _gate_event_activity("cleaning", 0) == "cleaning"


def test_docked_robot_pose_noise_is_not_meaningful() -> None:
    """Localization movement at the charger must not publish every second."""
    previous = RomoSnapshot(
        activity="docked",
        robot_x=1.0,
        robot_y=2.0,
        robot_yaw=359.5,
        dock_x=0.8,
        dock_y=2.0,
    )
    current = RomoSnapshot(
        activity="docked",
        robot_x=1.1,
        robot_y=2.1,
        robot_yaw=4.0,
        dock_x=0.805,
        dock_y=2.005,
    )

    assert not _meaningful_state_changed(previous, current)


def test_real_cleaning_movement_remains_meaningful() -> None:
    """Movement remains live while a cleaning mission is active."""
    previous = RomoSnapshot(
        activity="cleaning",
        robot_x=1.0,
        robot_y=2.0,
        robot_yaw=359.5,
    )

    assert not _meaningful_state_changed(
        previous,
        RomoSnapshot(
            activity="cleaning",
            robot_x=1.01,
            robot_y=2.0,
            robot_yaw=0.5,
        ),
    )
    assert _meaningful_state_changed(
        previous,
        RomoSnapshot(
            activity="cleaning",
            robot_x=1.03,
            robot_y=2.0,
            robot_yaw=0.5,
        ),
    )
