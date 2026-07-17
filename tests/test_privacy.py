"""Tests for diagnostic redaction coverage."""

from custom_components.dji_romo.privacy import DIAGNOSTIC_FIELDS_TO_REDACT


def test_robot_identity_fields_are_redacted() -> None:
    """Diagnostics hide both cloud identifiers and user-assigned names."""
    assert {
        "user_token",
        "device_sn",
        "device_name",
        "product_name",
        "name",
        "mission_bid",
        "file_url",
    } <= DIAGNOSTIC_FIELDS_TO_REDACT
