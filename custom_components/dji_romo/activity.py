"""Filtering for DJI Romo activity updates from mixed MQTT streams."""

from __future__ import annotations

from dataclasses import dataclass

ACTIVITY_CONFIRMATION_COUNT = 2
HELD_ACTIVITY_CONFIRMATION_COUNT = 5


@dataclass(slots=True)
class ActivityFilter:
    """Keep authoritative pause and return events stable across stale messages."""

    pending: str | None = None
    pending_count: int = 0
    held: str | None = None

    def override(self, activity: str, *, hold: bool = False) -> None:
        """Reset filtering after a successful user command."""
        self.pending = None
        self.pending_count = 0
        self.held = activity if hold else None

    def update(self, previous: str, candidate: str, *, source: str) -> str:
        """Return a stable activity for one new MQTT observation."""
        if (
            source == "events"
            and previous in {"docked", "idle"}
            and candidate
            in {
                "paused",
                "returning",
            }
        ):
            return previous

        if self.held is not None:
            if candidate == self.held:
                self.pending = None
                self.pending_count = 0
                return self.held

            if candidate == "error":
                self.override(candidate)
                return candidate

            if (
                source == "property"
                and self.held == "returning"
                and candidate == "docked"
            ):
                self.override(candidate)
                return candidate

            if source != "property":
                return self.held

            self._record_candidate(candidate)
            if self.pending_count < HELD_ACTIVITY_CONFIRMATION_COUNT:
                return self.held

            self.override(
                candidate,
                hold=candidate in {"cleaning", "paused", "returning"},
            )
            return candidate

        if candidate == previous:
            self.pending = None
            self.pending_count = 0
            if source == "property" and candidate in {
                "cleaning",
                "paused",
                "returning",
            }:
                self.held = candidate
            return candidate

        if candidate == "error" or (
            candidate == "docked" and previous in {"returning", "idle", "docked"}
        ):
            self.override(candidate)
            return candidate

        self._record_candidate(candidate)
        if self.pending_count >= ACTIVITY_CONFIRMATION_COUNT:
            self.override(
                candidate,
                hold=candidate in {"cleaning", "paused", "returning"},
            )
            return candidate

        return previous

    def _record_candidate(self, candidate: str) -> None:
        """Count consecutive observations of the same candidate."""
        if candidate == self.pending:
            self.pending_count += 1
        else:
            self.pending = candidate
            self.pending_count = 1
