"""MQTT session handling for DJI Romo."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging
import ssl
from typing import Any

import paho.mqtt.client as mqtt

from .client import DjiMqttCredentials

_LOGGER = logging.getLogger(__name__)

MessageCallback = Callable[[str, Any], None]


class DjiRomoMqttClient:
    """Manage a TLS MQTT session against DJI's cloud broker."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        on_message: MessageCallback,
    ) -> None:
        self._loop = loop
        self._on_message = on_message
        self._client: mqtt.Client | None = None
        self._connected = asyncio.Event()
        self._current_credentials: tuple[str, int, str, str, str] | None = None
        self._subscriptions: tuple[str, ...] = ()

    async def async_connect(
        self,
        credentials: DjiMqttCredentials,
        subscriptions: list[str],
    ) -> None:
        """Connect or reconnect if broker credentials changed."""
        new_credentials = (
            credentials.domain,
            credentials.port,
            credentials.client_id,
            credentials.username,
            credentials.password,
        )
        if (
            self._client is not None
            and self._current_credentials == new_credentials
            and self._subscriptions == tuple(subscriptions)
            and self._connected.is_set()
        ):
            return

        await self.async_disconnect()

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=credentials.client_id,
            protocol=mqtt.MQTTv311,
        )
        client.enable_logger(_LOGGER)
        client.username_pw_set(credentials.username, credentials.password)
        client.tls_set_context(ssl.create_default_context())
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_paho_message

        self._client = client
        self._connected.clear()
        self._subscriptions = tuple(subscriptions)
        self._current_credentials = new_credentials

        client.connect_async(credentials.domain, credentials.port, keepalive=60)
        client.loop_start()

        await asyncio.wait_for(self._connected.wait(), timeout=30)

    async def async_disconnect(self) -> None:
        """Tear down the MQTT client."""
        if self._client is None:
            return

        client = self._client
        self._client = None
        self._connected.clear()
        self._current_credentials = None
        self._subscriptions = ()

        await self._loop.run_in_executor(None, client.disconnect)
        await self._loop.run_in_executor(None, client.loop_stop)

    async def async_publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Publish a command payload."""
        if self._client is None or not self._connected.is_set():
            raise RuntimeError("DJI Romo MQTT session is not connected.")

        def _publish() -> None:
            msg_info = self._client.publish(  # type: ignore[union-attr]
                topic,
                payload=json.dumps(payload, separators=(",", ":")),
                qos=1,
            )
            msg_info.wait_for_publish()

        await self._loop.run_in_executor(None, _publish)

    def _on_connect(
        self,
        client: mqtt.Client,
        _userdata: Any,
        _flags: Any,
        reason_code: mqtt.ReasonCode,
        _properties: Any,
    ) -> None:
        """Handle MQTT connect callback."""
        is_failure = getattr(reason_code, "is_failure", None)
        if is_failure is None:
            is_failure = str(reason_code) not in {"Success", "0"}

        if is_failure:
            _LOGGER.error("DJI Romo MQTT connect failed: %s", reason_code)
            return

        _LOGGER.debug("DJI Romo MQTT connected")
        for topic in self._subscriptions:
            client.subscribe(topic, qos=1)
        self._loop.call_soon_threadsafe(self._connected.set)

    def _on_disconnect(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        _flags: Any,
        reason_code: mqtt.ReasonCode,
        _properties: Any,
    ) -> None:
        """Handle MQTT disconnect callback."""
        _LOGGER.debug("DJI Romo MQTT disconnected: %s", reason_code)
        self._loop.call_soon_threadsafe(self._connected.clear)

    def _on_paho_message(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        message: mqtt.MQTTMessage,
    ) -> None:
        """Forward MQTT messages into the HA event loop."""
        raw_payload = message.payload.decode("utf-8", errors="ignore")
        try:
            payload: Any = json.loads(raw_payload)
        except json.JSONDecodeError:
            payload = raw_payload

        self._loop.call_soon_threadsafe(
            self._on_message,
            message.topic,
            payload,
        )
