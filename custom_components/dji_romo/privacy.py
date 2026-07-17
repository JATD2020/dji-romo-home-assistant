"""Sensitive DJI Romo diagnostic field names."""

from .const import CONF_DEVICE_NAME, CONF_DEVICE_SN, CONF_USER_TOKEN

DIAGNOSTIC_FIELDS_TO_REDACT = {
    CONF_USER_TOKEN,
    CONF_DEVICE_SN,
    CONF_DEVICE_NAME,
    "user_token",
    "password",
    "username",
    "user_uuid",
    "client_id",
    "device_ip",
    "mac_address",
    "sn",
    "serial_number",
    "dock_sn",
    "uuid",
    "bid",
    "mission_bid",
    "file_id",
    "file_header",
    "file_url",
    "maintain_url",
    "name",
    "product_name",
    "x-amz-server-side-encryption-customer-key",
    "x-amz-server-side-encryption-customer-key-MD5",
}
