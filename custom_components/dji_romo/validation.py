"""Validation helpers for user-configurable DJI Romo endpoints and MQTT data."""

from __future__ import annotations

from collections.abc import Mapping
from string import Formatter
from typing import Any
from urllib.parse import urlparse


def validate_api_url(value: Any) -> str:
    """Return a normalized HTTPS URL hosted below DJI's djigate.com domain."""
    raw = str(value or "").strip().rstrip("/")
    parsed = urlparse(raw)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    is_dji_host = hostname == "djigate.com" or hostname.endswith(".djigate.com")
    if (
        parsed.scheme != "https"
        or not is_dji_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("API URL must be an HTTPS endpoint on djigate.com")
    return raw


def format_mqtt_topic(
    value: Any,
    device_sn: str,
    *,
    allow_wildcards: bool = True,
) -> str:
    """Validate and resolve one configurable MQTT topic."""
    topic = str(value or "").strip()
    if not topic or any(char in topic for char in ("\0", "\r", "\n")):
        raise ValueError("MQTT topic is empty or contains invalid characters")

    try:
        parts = list(Formatter().parse(topic))
    except ValueError as err:
        raise ValueError("MQTT topic contains an invalid placeholder") from err
    if any(
        field_name is not None
        and (field_name != "device_sn" or format_spec or conversion)
        for _literal, field_name, format_spec, conversion in parts
    ):
        raise ValueError("MQTT topic only supports the {device_sn} placeholder")

    resolved = topic.format(device_sn=device_sn)
    if not resolved or any(char in resolved for char in ("\0", "\r", "\n")):
        raise ValueError("MQTT topic is empty or contains invalid characters")
    if len(resolved.encode()) > 65_535:
        raise ValueError("MQTT topic is too long")
    if not allow_wildcards and any(char in resolved for char in ("#", "+")):
        raise ValueError("MQTT publish topics cannot contain wildcards")
    return resolved


def validate_subscription_topics(values: Any) -> list[str]:
    """Validate an unformatted list of MQTT subscription topics."""
    if not isinstance(values, list) or not values:
        raise ValueError("At least one MQTT subscription topic is required")
    topics = [str(value).strip() for value in values]
    for topic in topics:
        format_mqtt_topic(topic, "VALIDATION")
    return topics


def validate_command_mapping(value: Any) -> dict[str, Any]:
    """Validate configurable logical-to-MQTT command mappings."""
    if not isinstance(value, Mapping):
        raise ValueError("Command mapping must be a JSON object")

    result: dict[str, Any] = {}
    for raw_key, raw_command in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ValueError("Command mapping keys must be non-empty strings")
        key = raw_key.strip()
        if isinstance(raw_command, str):
            if not raw_command.strip():
                raise ValueError(f"Command '{key}' has an empty method")
            result[key] = raw_command.strip()
            continue
        if not isinstance(raw_command, Mapping):
            raise ValueError(f"Command '{key}' must be a string or JSON object")
        command = dict(raw_command)
        method = command.get("method")
        if not isinstance(method, str) or not method.strip():
            raise ValueError(f"Command '{key}' requires a non-empty method")
        if "data" in command and not isinstance(command["data"], (dict, list)):
            raise ValueError(f"Command '{key}' data must be an object or array")
        command["method"] = method.strip()
        result[key] = command
    return result
