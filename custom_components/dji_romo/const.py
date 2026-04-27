"""Constants for the DJI Romo integration."""

from __future__ import annotations

from datetime import timedelta
import json

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
DEFAULT_COMMAND_MAPPING_JSON = json.dumps(DEFAULT_COMMAND_MAPPING, indent=2, sort_keys=True)

PLATFORMS = ["vacuum", "sensor", "button", "select", "number"]
COORDINATOR_REFRESH_INTERVAL = timedelta(minutes=30)
MQTT_CREDENTIAL_REFRESH_MARGIN = timedelta(minutes=15)
MQTT_CREDENTIAL_ASSUMED_LIFETIME = timedelta(hours=4)

ATTR_LAST_TOPIC = "last_topic"
ATTR_LAST_UPDATED = "last_updated"
ATTR_MODEL = "model"
ATTR_RAW_STATE = "raw_state"
ATTR_SELECTED_TOPIC = "selected_topic"
