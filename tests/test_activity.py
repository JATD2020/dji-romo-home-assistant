"""Tests for stable DJI Romo activity filtering."""

from custom_components.dji_romo.activity import ActivityFilter


def test_pause_requires_confirmation_and_is_held() -> None:
    """A repeated pause event is confirmed, then stale cleaning stays ignored."""
    activity_filter = ActivityFilter()
    assert activity_filter.update("cleaning", "paused", source="events") == "cleaning"
    assert activity_filter.update("cleaning", "paused", source="events") == "paused"
    assert activity_filter.update("paused", "cleaning", source="events") == "paused"

    for _ in range(4):
        assert (
            activity_filter.update("paused", "cleaning", source="property") == "paused"
        )
    assert activity_filter.update("paused", "cleaning", source="property") == "cleaning"


def test_return_event_changes_to_docked_immediately() -> None:
    """A dock confirmation completes a held return without extra delay."""
    activity_filter = ActivityFilter()
    assert (
        activity_filter.update("cleaning", "returning", source="events") == "cleaning"
    )
    assert (
        activity_filter.update("cleaning", "returning", source="events") == "returning"
    )
    assert activity_filter.update("returning", "docked", source="property") == "docked"


def test_property_pause_becomes_held_after_confirmation() -> None:
    """Two property observations confirm pause and protect it from stale state."""
    activity_filter = ActivityFilter()
    assert activity_filter.update("cleaning", "paused", source="property") == "cleaning"
    assert activity_filter.update("cleaning", "paused", source="property") == "paused"
    assert activity_filter.held == "paused"


def test_docked_from_cleaning_requires_confirmation() -> None:
    """One contradictory dock flag cannot end a cleaning session."""
    activity_filter = ActivityFilter()
    assert activity_filter.update("cleaning", "docked", source="property") == "cleaning"
    assert activity_filter.update("cleaning", "docked", source="property") == "docked"


def test_error_is_immediate() -> None:
    """Errors are never delayed by activity filtering."""
    activity_filter = ActivityFilter(held="paused")
    assert activity_filter.update("paused", "error", source="property") == "error"


def test_successful_resume_override_clears_pause_hold() -> None:
    """An acknowledged Home Assistant resume is reflected immediately."""
    activity_filter = ActivityFilter(held="paused")
    activity_filter.override("cleaning", hold=True)
    assert activity_filter.held == "cleaning"
    assert (
        activity_filter.update("cleaning", "cleaning", source="property") == "cleaning"
    )


def test_stale_pause_event_cannot_undo_confirmed_resume() -> None:
    """A late pause event from the same mission cannot re-pause the entity."""
    activity_filter = ActivityFilter(held="paused")
    activity_filter.override("cleaning", hold=True)

    assert activity_filter.update("cleaning", "paused", source="events") == "cleaning"
    for _ in range(4):
        assert (
            activity_filter.update("cleaning", "paused", source="property")
            == "cleaning"
        )
    assert activity_filter.update("cleaning", "paused", source="property") == "paused"


def test_stale_return_event_is_ignored_while_docked() -> None:
    """A completed return cannot be restarted by a delayed event."""
    activity_filter = ActivityFilter()

    assert activity_filter.update("docked", "returning", source="events") == "docked"
