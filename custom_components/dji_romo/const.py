"""Constants for the DJI Romo integration."""

from __future__ import annotations

import json
from datetime import timedelta

DOMAIN = "dji_romo"

CONF_API_URL = "api_url"
CONF_COMMAND_MAPPING = "command_mapping"
CONF_COMMAND_TOPIC = "command_topic"
CONF_CREDENTIALS_TEXT = "credentials_text"
CONF_DEVICE_NAME = "device_name"
CONF_DEVICE_SN = "device_sn"
CONF_ROOM_CLEAN_MODE = "room_clean_mode"
CONF_ROOM_CLEAN_NUM = "room_clean_num"
CONF_ROOM_CLEAN_SPEED = "room_clean_speed"
CONF_ROOM_FAN_SPEED = "room_fan_speed"
CONF_ROOM_WATER_LEVEL = "room_water_level"
CONF_LOCALE = "locale"
CONF_SUBSCRIPTION_TOPICS = "subscription_topics"
CONF_USER_TOKEN = "user_token"

DEFAULT_API_URL = "https://home-api-vg.djigate.com"
DEFAULT_LOCALE = "en_US"
DEFAULT_COMMAND_TOPIC = "forward/cr800/thing/product/{device_sn}/services"
DEFAULT_SUBSCRIPTION_TOPICS = [
    "forward/cr800/thing/product/{device_sn}/#",
    "thing/product/{device_sn}/#",
]
DEFAULT_COMMAND_MAPPING = {
    "start": {"method": "start_clean"},
    "pause": {"method": "pause_clean"},
    "stop": {"method": "stop_clean"},
    "return_to_base": {"method": "back_charge"},
    "locate": {"method": "find_robot"},
}
DEFAULT_COMMAND_MAPPING_JSON = json.dumps(
    DEFAULT_COMMAND_MAPPING, indent=2, sort_keys=True
)

# plan_name_key values DJI ships for the built-in cleaning programs. These are
# stable across locales (the human-readable plan_name is localized, often to
# Chinese), so we prefer them both for naming and for picking a sensible
# "clean everything" default when Home Assistant asks the vacuum to start.
PLAN_NAME_KEYS = {
    "default_plan_name_daliy": "Daily Cleaning",
    "default_plan_name_deep": "Deep Cleaning",
    "default_plan_name_quick": "Fine Vacuum",
    "default_plan_name_disinfect": "Floor Disinfection",
    "default_plan_name_temp": "Single Cleaning",
}
# Order of preference when HA's generic "start" is pressed: a whole-home daily
# clean first, then deep, then a fine vacuum.
DEFAULT_START_PLAN_KEYS = (
    "default_plan_name_daliy",
    "default_plan_name_deep",
    "default_plan_name_quick",
)

# A running status string has varied between captured API responses. Treat any
# status outside this verified terminal set as active instead of guessing one
# specific value.
TERMINAL_JOB_STATUSES = frozenset(
    {
        "ok",
        "canceled",
        "cancelled",
        "completed",
        "complete",
        "failed",
        "fail",
        "error",
        "stopped",
        "stop",
        "timeout",
        "interrupted",
        "abort",
        "aborted",
    }
)

# DJI room-type IDs -> English labels (user_label / poly_label numbering).
ROOM_LABELS = {
    1: "Kitchen",
    2: "Toilet",
    3: "Living Room",
    4: "Dining Room",
    5: "Master Bedroom",
    6: "Bedroom",
    7: "Study",
    8: "Children's Room",
    9: "Balcony",
    10: "Bathroom",
    11: "Foyer",
    12: "Office",
    13: "Corridor",
    14: "Hallway",
    15: "Other",
}

PLATFORMS = [
    "vacuum",
    "sensor",
    "binary_sensor",
    "button",
    "select",
    "number",
    "switch",
    "time",
    "image",
    "event",
]

# Persisted trajectory (so the map survives a Home Assistant restart).
TRAJECTORY_STORAGE_VERSION = 1
TRAJECTORY_STORAGE_KEY = f"{DOMAIN}_trajectory"
# We keep the full real trace (every path point, like the DJI app). This cap is only a
# memory safety net so a pathologically long session can't grow unbounded; at ~2.9 pts/s
# it covers a ~7.5h run before the oldest points start dropping.
TRAJECTORY_MAX_POINTS = 80000
# Cap on points actually written to disk (the live trace keeps full resolution);
# a long session is downsampled to this before persisting.
TRAJECTORY_STORAGE_POINTS = 8000
TRAJECTORY_SAVE_DELAY = 30  # seconds; debounced disk writes
CLEAN_PASS_TYPES = frozenset({48, 80, 112})

# Home Assistant service to clean several named rooms in one job.
SERVICE_CLEAN_ROOMS = "clean_rooms"
ATTR_ROOMS = "rooms"
COORDINATOR_REFRESH_INTERVAL = timedelta(minutes=5)
# Settings, consumables and shortcuts barely change, so they are fetched less
# often than live properties and cleaning jobs.
STATIC_REFRESH_INTERVAL = timedelta(minutes=30)
MQTT_CREDENTIAL_REFRESH_MARGIN = timedelta(minutes=15)
# Fallback lifetime used only if the cloud stops returning an explicit expiry.
MQTT_CREDENTIAL_ASSUMED_LIFETIME = timedelta(hours=4)

# Once the broker disconnects, keep the last-known state briefly before marking
# the robot unavailable. A connected docked robot may legitimately stay silent.
OFFLINE_AFTER = timedelta(seconds=90)
# How often to re-evaluate offline-by-silence without making any network calls.
AVAILABILITY_CHECK_INTERVAL = timedelta(seconds=60)
# A connected-but-silent MQTT session this long is treated as a "zombie": force a
# rebuild. Kept at 10 min (a docked robot legitimately goes quiet when asleep, so a
# shorter window would reconnect needlessly); we still flag offline first at 90 s.
MQTT_STALE_AFTER = timedelta(minutes=10)
# Consecutive REST refresh failures before marking the coordinator update failed
# (entities go unavailable) instead of silently keeping stale data.
CLOUD_REFRESH_FAILURE_LIMIT = 3

# Home Assistant event fired when the robot reports a health-management (HMS)
# alert, so automations can notify the user that the robot needs attention.
EVENT_HMS = f"{DOMAIN}_hms"
