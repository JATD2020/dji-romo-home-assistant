"""Tests for safe advanced configuration values."""

import pytest

from custom_components.dji_romo.validation import (
    format_mqtt_topic,
    validate_api_url,
    validate_command_mapping,
    validate_subscription_topics,
)


def test_api_url_only_accepts_https_dji_hosts() -> None:
    """A pasted endpoint cannot redirect the DJI token outside DJI's domain."""
    assert (
        validate_api_url("https://home-api-vg.djigate.com/")
        == "https://home-api-vg.djigate.com"
    )
    with pytest.raises(ValueError):
        validate_api_url("http://home-api-vg.djigate.com")
    with pytest.raises(ValueError):
        validate_api_url("https://djigate.com.example.org")
    with pytest.raises(ValueError):
        validate_api_url("https://home-api-vg.djigate.com/path")


def test_topics_only_allow_the_device_placeholder() -> None:
    """Unknown placeholders are rejected before an entry reloads."""
    assert format_mqtt_topic("thing/{device_sn}/#", "ABC") == "thing/ABC/#"
    assert validate_subscription_topics(["thing/{device_sn}/#"])
    with pytest.raises(ValueError):
        format_mqtt_topic("thing/{serial}/#", "ABC")
    with pytest.raises(ValueError):
        format_mqtt_topic("thing/{device_sn.__class__}/#", "ABC")
    with pytest.raises(ValueError):
        format_mqtt_topic("thing/{device_sn!r}/#", "ABC")
    with pytest.raises(ValueError):
        format_mqtt_topic("thing/{device_sn:>10}/#", "ABC")
    with pytest.raises(ValueError):
        format_mqtt_topic(
            "thing/{device_sn}/#",
            "ABC",
            allow_wildcards=False,
        )
    with pytest.raises(ValueError):
        validate_subscription_topics([])


def test_command_mapping_requires_valid_methods() -> None:
    """JSON values that would crash command dispatch are rejected."""
    assert validate_command_mapping(
        {"start": {"method": "start_clean", "data": {}}}
    ) == {"start": {"method": "start_clean", "data": {}}}
    with pytest.raises(ValueError):
        validate_command_mapping(["start_clean"])
    with pytest.raises(ValueError):
        validate_command_mapping({"start": {"data": {}}})
