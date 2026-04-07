"""Tuya Link MQTT client for HA plugin gateway.

Implements Tuya Link protocol (HMAC-SHA256 sign) and MQTT connection
to Tuya cloud, based on the official TuyaLink Java demo.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import ssl
import threading
import time
from typing import Any, Callable

from .const import (
    TUYA_LINK_TOPIC_ACTION_EXECUTE,
    TUYA_LINK_CLIENT_ID_PREFIX,
    TUYA_LINK_TOPIC_DEVICE_UNBIND_NOTICE,
    TUYA_LINK_MQTT_BROKER,
    TUYA_LINK_MQTT_PORT,
    TUYA_LINK_TOPIC_HA_DEVICES,
    TUYA_LINK_TOPIC_PROPERTY_REPORT,
    TUYA_LINK_TOPIC_PROPERTY_REPORT_RESPONSE,
    TUYA_LINK_TOPIC_PROPERTY_SET,
    TUYA_LINK_TOPIC_SUBDEVICE_BIND,
    TUYA_LINK_TOPIC_SUBDEVICE_BIND_RESPONSE,
    TUYA_LINK_TOPIC_SUBDEVICE_DELETE,
    TUYA_LINK_TOPIC_SUBDEVICE_DELETE_RESPONSE,
    TUYA_LINK_TOPIC_SUBDEVICE_LOGIN,
    TUYA_LINK_TOPIC_SUBDEVICE_LOGOUT,
    TUYA_LINK_TOPIC_SUBDEVICE_TO_BIND,
    TUYA_LINK_TOPIC_SUBDEVICE_TO_BIND_RESPONSE,
    TUYA_LINK_TOPIC_TOPO_CHANGE,
    TUYA_LINK_TOPIC_TOPO_GET,
    TUYA_LINK_TOPIC_TOPO_GET_RESPONSE,
)

_LOGGER = logging.getLogger(__name__)


def _reason_code_details(reason_code: Any) -> tuple[int, str, bool]:
    """Normalize paho reason codes across callback API variants."""
    if hasattr(reason_code, "value"):
        value = reason_code.value
        text = str(reason_code)
        is_failure = bool(getattr(reason_code, "is_failure", value >= 0x80))
        return value, text, is_failure

    value = int(reason_code)
    return value, str(reason_code), value != 0


def tuya_link_sign(
    product_id: str, device_id: str, device_secret: str
) -> tuple[str, str, str]:
    """Compute Tuya Link MQTT credentials (username, password, client_id).

    Matches the Java TuyaMqttSign.calculate() logic:
    - username: deviceId|signMethod=hmacSha256,timestamp=...,secureMode=1,accessType=1
    - clientId: tuyalink_<deviceId>
    - password: HMAC-SHA256(plainPasswd, deviceSecret) as 64-char hex
    """
    timestamp = str(int(time.time() * 1000))
    username = (
        f"{device_id}|signMethod=hmacSha256,timestamp={timestamp},"
        "secureMode=1,accessType=1"
    )
    client_id = f"{TUYA_LINK_CLIENT_ID_PREFIX}{device_id}"
    plain_passwd = (
        f"deviceId={device_id},timestamp={timestamp},"
        "secureMode=1,accessType=1"
    )
    password = (
        hmac.new(
            device_secret.encode(),
            plain_passwd.encode(),
            hashlib.sha256,
        )
        .hexdigest()
        .lower()
    )
    # Java uses %064x (zero-pad to 64 hex chars); Python hexdigest is already 64
    if len(password) < 64:
        password = password.zfill(64)
    return username, password, client_id


def _default_ssl_context() -> ssl.SSLContext:
    """Return TLS context for Tuya MQTT (verify server certificate).

    Some Tuya MQTT brokers do not handle TLS 1.3 negotiation correctly,
    so we cap at TLS 1.2 to avoid UNEXPECTED_EOF_WHILE_READING errors.
    TODO: Remove this cap once Tuya brokers support TLS 1.3.
    """
    ctx = ssl.create_default_context()
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class TuyaLinkMqttClient:
    """Tuya Link MQTT client: connect, subscribe, publish, disconnect.

    Runs paho-mqtt in a thread; connect/disconnect and callbacks are
    invoked from the executor to avoid blocking the event loop.
    """

    def __init__(
        self,
        product_id: str,
        device_id: str,
        device_secret: str,
        *,
        broker: str = TUYA_LINK_MQTT_BROKER,
        port: int = TUYA_LINK_MQTT_PORT,
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[int, str | None], None] | None = None,
        on_message: Callable[[str, bytes], None] | None = None,
    ) -> None:
        """Initialize the client with device credentials and callbacks."""
        self._product_id = product_id
        self._device_id = device_id
        self._device_secret = device_secret
        self._broker = broker
        self._port = port
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._on_message = on_message
        self._client: Any = None
        self._connected = False
        self._connect_event = threading.Event()
        self._connect_reason_code = 0
        self._connect_error_message: str | None = None
        self._pending_subdevice_delete_requests: dict[str, list[str]] = {}
        self._subscribed_child_ids: set[str] = set()

    @property
    def connected(self) -> bool:
        """Return whether the client is connected."""
        return self._connected

    def set_callbacks(
        self,
        *,
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[int, str | None], None] | None = None,
        on_message: Callable[[str, bytes], None] | None = None,
    ) -> None:
        """Update callbacks, including for an already connected client."""
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._on_message = on_message

        if self._client is not None:
            self._client.on_connect = self._handle_connect
            self._client.on_disconnect = self._handle_disconnect
            self._client.on_message = self._handle_message

    def connect(self, timeout: float = 10.0) -> None:
        """Connect to Tuya Link MQTT and wait for the broker response."""
        from paho.mqtt import client as mqtt

        self._connect_event.clear()
        self._connect_reason_code = 0
        self._connect_error_message = None

        username, password, client_id = tuya_link_sign(
            self._product_id,
            self._device_id,
            self._device_secret,
        )
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
            protocol=mqtt.MQTTv311,
        )
        self._client.reconnect_delay_set(min_delay=1, max_delay=120)
        self._client.username_pw_set(username, password)
        self._client.on_connect = self._handle_connect
        self._client.on_disconnect = self._handle_disconnect
        self._client.on_message = self._handle_message
        self._client.tls_set_context(_default_ssl_context())
        self._client.connect(self._broker, self._port, keepalive=60)
        self._client.loop_start()

        if not self._connect_event.wait(timeout):
            self.disconnect()
            raise TimeoutError("Timed out waiting for Tuya Link MQTT connection")

        if self._connect_reason_code != 0:
            self.disconnect()
            raise ConnectionError(
                "Tuya Link MQTT connection rejected: "
                f"{self._connect_error_message or self._connect_reason_code}"
            )

    def _handle_connect(
        self,
        _client: Any,
        _userdata: Any,
        _flags: Any,
        reason_code: Any,
        _properties: Any = None,
    ) -> None:
        """Handle MQTT connect (runs in paho thread)."""
        reason, reason_text, is_failure = _reason_code_details(reason_code)
        self._connect_reason_code = reason
        self._connect_error_message = reason_text

        if not is_failure:
            self._connected = True
            _LOGGER.debug("Tuya Link MQTT connected")
            topic_set = TUYA_LINK_TOPIC_PROPERTY_SET.format(device_id=self._device_id)
            topic_bind_response = TUYA_LINK_TOPIC_SUBDEVICE_BIND_RESPONSE.format(
                device_id=self._device_id
            )
            topic_delete = TUYA_LINK_TOPIC_SUBDEVICE_DELETE.format(
                device_id=self._device_id
            )
            topic_delete_response = TUYA_LINK_TOPIC_SUBDEVICE_DELETE_RESPONSE.format(
                device_id=self._device_id
            )
            topic_topo_change = TUYA_LINK_TOPIC_TOPO_CHANGE.format(
                device_id=self._device_id
            )
            topic_topo_get_response = TUYA_LINK_TOPIC_TOPO_GET_RESPONSE.format(
                device_id=self._device_id
            )
            topic_to_bind = TUYA_LINK_TOPIC_SUBDEVICE_TO_BIND.format(
                device_id=self._device_id
            )
            topic_unbind_notice = TUYA_LINK_TOPIC_DEVICE_UNBIND_NOTICE.format(
                device_id=self._device_id
            )
            self._client.subscribe(topic_set, qos=1)
            self._client.subscribe(topic_bind_response, qos=1)
            self._client.subscribe(topic_delete, qos=1)
            self._client.subscribe(topic_delete_response, qos=1)
            self._client.subscribe(topic_topo_change, qos=1)
            self._client.subscribe(topic_topo_get_response, qos=1)
            self._client.subscribe(topic_to_bind, qos=1)
            self._client.subscribe(topic_unbind_notice, qos=1)
            _LOGGER.debug("Subscribed to %s", topic_set)
            _LOGGER.debug("Subscribed to %s", topic_bind_response)
            _LOGGER.debug("Subscribed to %s", topic_delete)
            _LOGGER.debug("Subscribed to %s", topic_delete_response)
            _LOGGER.debug("Subscribed to %s", topic_topo_change)
            _LOGGER.debug("Subscribed to %s", topic_topo_get_response)
            _LOGGER.debug("Subscribed to %s", topic_to_bind)
            _LOGGER.debug("Subscribed to %s", topic_unbind_notice)
            for child_id in self._subscribed_child_ids:
                self._subscribe_child_topics(child_id)
            if self._subscribed_child_ids:
                _LOGGER.debug(
                    "Re-subscribed to %d child device topic(s) after reconnect",
                    len(self._subscribed_child_ids),
                )
            if self._on_connect:
                self._on_connect()
        else:
            _LOGGER.warning(
                "Tuya Link MQTT connect failed, reason_code=%s", reason_code
            )
        self._connect_event.set()

    def _handle_disconnect(
        self,
        _client: Any,
        _userdata: Any,
        disconnect_flags: int,
        reason_code: Any,
        _properties: Any = None,
    ) -> None:
        """Handle MQTT disconnect (runs in paho thread)."""
        self._connected = False
        reason, _, _ = _reason_code_details(reason_code)
        _LOGGER.debug(
            "Tuya Link MQTT disconnected, reason_code=%s", reason_code
        )
        if self._on_disconnect:
            self._on_disconnect(reason, None)

    def _handle_message(
        self,
        _client: Any,
        _userdata: Any,
        message: Any,
    ) -> None:
        """Handle incoming MQTT message (runs in paho thread)."""
        _LOGGER.debug(
            "Tuya Link message topic=%s payload=%s",
            message.topic,
            message.payload,
        )
        if self._on_message:
            self._on_message(message.topic, message.payload)

    def publish_subdevice_bind(self, children: list[dict[str, Any]]) -> None:
        """Publish a child-device bind request.

        Each child dict may contain: productId, clientId, deviceName,
        defaultProperties (list of {code, value}).
        """
        if not self._client or not self._connected:
            _LOGGER.warning("Cannot publish child bind: not connected")
            return
        topic = TUYA_LINK_TOPIC_SUBDEVICE_BIND.format(device_id=self._device_id)
        payload = json.dumps(
            {
                "msgId": str(int(time.time() * 1000)),
                "time": int(time.time() * 1000),
                "version": "1.0",
                "data": children,
            }
        )
        _LOGGER.debug("Publishing subdevice bind topic=%s payload=%s", topic, payload)
        self._client.publish(topic, payload, qos=1)

    def publish_ha_devices(
        self, devices: list[dict[str, str]]
    ) -> None:
        """Publish eligible HA sub-devices for the app to select.

        Topic: tylink/${deviceId}/device/list/found
        Each device dict contains: clientId, productId, deviceName, deviceType.
        """
        if not self._client or not self._connected:
            _LOGGER.warning("Cannot publish HA devices: not connected")
            return
        topic = TUYA_LINK_TOPIC_HA_DEVICES.format(device_id=self._device_id)
        payload = json.dumps(
            {
                "msgId": str(int(time.time() * 1000)),
                "time": int(time.time() * 1000),
                "version": "1.0",
                "data": devices,
            }
        )
        _LOGGER.debug("Publishing HA devices topic=%s payload=%s", topic, payload)
        self._client.publish(topic, payload, qos=1)

    def publish_topo_add_notify_response(
        self, msg_id: str, children: list[dict[str, str]]
    ) -> None:
        """Publish response to topo/add/notify after processing.

        Topic: tylink/${deviceId}/device/topo/add/notify_response
        """
        if not self._client or not self._connected:
            _LOGGER.warning("Cannot publish topo add notify response: not connected")
            return
        topic = TUYA_LINK_TOPIC_SUBDEVICE_TO_BIND_RESPONSE.format(
            device_id=self._device_id
        )
        payload = json.dumps(
            {
                "msgId": msg_id,
                "time": int(time.time() * 1000),
                "version": "1.0",
                "data": children,
            }
        )
        _LOGGER.debug(
            "Publishing topo add notify response topic=%s payload=%s", topic, payload
        )
        self._client.publish(topic, payload, qos=1)

    def publish_subdevice_delete(self, child_device_ids: list[str]) -> None:
        """Publish a child-device delete request."""
        if not self._client or not self._connected:
            _LOGGER.warning("Cannot publish child delete: not connected")
            return
        topic = TUYA_LINK_TOPIC_SUBDEVICE_DELETE.format(device_id=self._device_id)
        msg_id = str(int(time.time() * 1000))
        self._pending_subdevice_delete_requests[msg_id] = list(child_device_ids)
        payload = json.dumps(
            {
                "msgId": msg_id,
                "time": int(time.time() * 1000),
                "version": "1.0",
                "data": child_device_ids,
            }
        )
        _LOGGER.debug("Publishing subdevice delete topic=%s payload=%s", topic, payload)
        self._client.publish(topic, payload, qos=1)

    def has_pending_subdevice_delete_request(self, msg_id: str) -> bool:
        """Return if a delete request originated from this gateway."""
        return msg_id in self._pending_subdevice_delete_requests

    def pop_pending_subdevice_delete_request(self, msg_id: str) -> list[str]:
        """Return and clear a tracked delete request by message ID."""
        return self._pending_subdevice_delete_requests.pop(msg_id, [])

    def clear_pending_subdevice_delete_request(self, msg_id: str) -> None:
        """Clear a tracked delete request once the result is processed."""
        self._pending_subdevice_delete_requests.pop(msg_id, None)

    def publish_subdevice_login(self, child_device_ids: list[str]) -> None:
        """Publish a child-device online request."""
        if not self._client or not self._connected:
            _LOGGER.warning("Cannot publish child login: not connected")
            return
        topic = TUYA_LINK_TOPIC_SUBDEVICE_LOGIN.format(device_id=self._device_id)
        payload = json.dumps(
            {
                "msgId": str(int(time.time() * 1000)),
                "time": int(time.time() * 1000),
                "version": "1.0",
                "data": child_device_ids,
            }
        )
        _LOGGER.debug("Publishing subdevice login topic=%s payload=%s", topic, payload)
        self._client.publish(topic, payload, qos=1)

    def publish_topo_get(
        self,
        dev_ids: list[str] | None = None,
        *,
        start_id: int | None = None,
        page_size: int | None = None,
    ) -> str:
        """Publish a topo/get request to query bound sub-devices from cloud.

        Returns the msgId used in the request so the caller can correlate
        the response.

        ``dev_ids``, ``start_id``, and ``page_size`` are all optional and
        follow Tuya's topo/get protocol:
        - omit ``dev_ids`` to query all sub-devices under the gateway
        - use ``start_id`` + ``page_size`` for pagination
        """
        if not self._client or not self._connected:
            _LOGGER.warning("Cannot publish topo/get: not connected")
            return ""
        topic = TUYA_LINK_TOPIC_TOPO_GET.format(device_id=self._device_id)
        msg_id = str(int(time.time() * 1000))
        data: dict[str, Any] = {}
        if (
            isinstance(start_id, int)
            and not isinstance(start_id, bool)
            and start_id >= 0
        ):
            data["startId"] = start_id
        if (
            isinstance(page_size, int)
            and not isinstance(page_size, bool)
            and page_size > 0
        ):
            data["pageSize"] = page_size
        if dev_ids:
            data["devIds"] = dev_ids
        payload = json.dumps(
            {
                "msgId": msg_id,
                "time": int(time.time() * 1000),
                "version": "1.0",
                "data": data,
            }
        )
        _LOGGER.debug("Publishing topo/get topic=%s payload=%s", topic, payload)
        self._client.publish(topic, payload, qos=1)
        return msg_id

    def publish_subdevice_logout(self, child_device_ids: list[str]) -> None:
        """Publish a child-device offline request."""
        if not self._client or not self._connected:
            _LOGGER.warning("Cannot publish child logout: not connected")
            return
        topic = TUYA_LINK_TOPIC_SUBDEVICE_LOGOUT.format(device_id=self._device_id)
        payload = json.dumps(
            {
                "msgId": str(int(time.time() * 1000)),
                "time": int(time.time() * 1000),
                "version": "1.0",
                "data": child_device_ids,
            }
        )
        _LOGGER.debug("Publishing subdevice logout topic=%s payload=%s", topic, payload)
        self._client.publish(topic, payload, qos=1)

    def publish_child_property_report(
        self, child_device_id: str, properties: dict[str, Any]
    ) -> None:
        """Publish a single child-device property report (blocking).

        Topic: tylink/${childDeviceId}/thing/property/report
        Uses the per-device report topic instead of the gateway batch topic.
        """
        if not self._client or not self._connected:
            _LOGGER.warning("Cannot publish child property report: not connected")
            return
        topic = TUYA_LINK_TOPIC_PROPERTY_REPORT.format(device_id=child_device_id)
        payload = json.dumps(
            {
                "msgId": str(int(time.time() * 1000)),
                "time": int(time.time() * 1000),
                "data": properties,
                "sys": {
                    "ack":1
                }
            }
        )
        _LOGGER.debug(
            "Publishing child property report topic=%s payload=%s", topic, payload
        )
        self._client.publish(topic, payload, qos=1)

    def _subscribe_child_topics(self, child_device_id: str) -> None:
        """Subscribe to MQTT topics for a single child device."""
        property_topic = TUYA_LINK_TOPIC_PROPERTY_SET.format(device_id=child_device_id)
        action_topic = TUYA_LINK_TOPIC_ACTION_EXECUTE.format(device_id=child_device_id)
        report_response_topic = TUYA_LINK_TOPIC_PROPERTY_REPORT_RESPONSE.format(
            device_id=child_device_id
        )
        self._client.subscribe(property_topic, qos=1)
        self._client.subscribe(action_topic, qos=1)
        self._client.subscribe(report_response_topic, qos=1)
        _LOGGER.debug("Subscribed to %s", property_topic)
        _LOGGER.debug("Subscribed to %s", action_topic)
        _LOGGER.debug("Subscribed to %s", report_response_topic)

    def subscribe_child_device_messages(self, child_device_id: str) -> None:
        """Subscribe to child-device control and response topics."""
        if not self._client:
            return
        self._subscribed_child_ids.add(child_device_id)
        self._subscribe_child_topics(child_device_id)

    def unsubscribe_child_device_messages(self, child_device_id: str) -> None:
        """Unsubscribe from child-device control and response topics."""
        self._subscribed_child_ids.discard(child_device_id)
        if not self._client:
            return
        property_topic = TUYA_LINK_TOPIC_PROPERTY_SET.format(device_id=child_device_id)
        action_topic = TUYA_LINK_TOPIC_ACTION_EXECUTE.format(device_id=child_device_id)
        report_response_topic = TUYA_LINK_TOPIC_PROPERTY_REPORT_RESPONSE.format(
            device_id=child_device_id
        )
        self._client.unsubscribe(property_topic)
        self._client.unsubscribe(action_topic)
        self._client.unsubscribe(report_response_topic)
        _LOGGER.debug("Unsubscribed from %s", property_topic)
        _LOGGER.debug("Unsubscribed from %s", action_topic)
        _LOGGER.debug("Unsubscribed from %s", report_response_topic)

    def disconnect(self) -> None:
        """Disconnect and stop loop (blocking). Run in executor."""
        if self._client:
            self._client.disconnect()
            self._client.loop_stop()
            self._client = None
        self._connected = False
        self._connect_event.clear()
        self._pending_subdevice_delete_requests.clear()
        self._subscribed_child_ids.clear()
        _LOGGER.debug("Tuya Link MQTT disconnected")
