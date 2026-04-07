"""Constants for the Tuya HA New gateway integration."""

from __future__ import annotations

import logging

DOMAIN = "tuya_cloud_ha_bridge"
LOGGER = logging.getLogger(__package__)
TUYA_CLOUD_API_KEY_GUIDE_URL = "https://tuya.ai/"

# Config entry keys (gateway device credentials from Tuya cloud)
CONF_PRODUCT_ID = "product_id"
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_SECRET = "device_secret"
CONF_DEVICE_NAME = "device_name"
CONF_QR_CODE_DATA = "qr_code_data"

# OpenAPI gateway creation path
TUYA_OPENAPI_GATEWAY_ACTIVE_PATH = "/v1.0/end-user/devices/ha/gateway/active"
TUYA_OPENAPI_SUB_DEVICE_LIMIT_PATH = "/v1.0/end-user/devices/ha/sub/device/limit"
TUYA_OPENAPI_CATEGORY_PID_MAPPINGS_PATH = (
    "/v1.0/end-user/services/ha/category/pid/mappings"
)


# Local config file for virtual gateway credentials.
LOCAL_CREDENTIALS_FILE = f"{DOMAIN}_virtual_gateway.yaml"

# Default Tuya Link MQTT broker fallback (China region)
TUYA_LINK_MQTT_BROKER = "m1.tuyacn.com"
TUYA_LINK_MQTT_PORT = 8883
TUYA_LINK_CLIENT_ID_PREFIX = "tuyalink_"

# Topic patterns (Tuya Link thing model)
TUYA_LINK_TOPIC_SUBDEVICE_BIND = "tylink/{device_id}/device/sub/bind"
TUYA_LINK_TOPIC_SUBDEVICE_BIND_RESPONSE = (
    "tylink/{device_id}/device/sub/bind_response"
)
TUYA_LINK_TOPIC_SUBDEVICE_DELETE = "tylink/{device_id}/device/sub/delete"
TUYA_LINK_TOPIC_SUBDEVICE_DELETE_RESPONSE = (
    "tylink/{device_id}/device/sub/delete_response"
)
TUYA_LINK_TOPIC_TOPO_CHANGE = "tylink/{device_id}/device/topo/change"
TUYA_LINK_TOPIC_TOPO_GET = "tylink/{device_id}/device/topo/get"
TUYA_LINK_TOPIC_TOPO_GET_RESPONSE = "tylink/{device_id}/device/topo/get_response"
TUYA_LINK_TOPIC_SUBDEVICE_LOGIN = "tylink/{device_id}/device/sub/login"
TUYA_LINK_TOPIC_SUBDEVICE_LOGOUT = "tylink/{device_id}/device/sub/logout"
TUYA_LINK_TOPIC_BATCH_REPORT = "tylink/{device_id}/thing/data/batch_report"
TUYA_LINK_TOPIC_ACTION_EXECUTE = "tylink/{device_id}/thing/action/execute"
TUYA_LINK_TOPIC_PROPERTY_SET = "tylink/{device_id}/thing/property/set"
TUYA_LINK_TOPIC_PROPERTY_REPORT = "tylink/{device_id}/thing/property/report"
TUYA_LINK_TOPIC_PROPERTY_REPORT_RESPONSE = (
    "tylink/{device_id}/thing/property/report_response"
)

# Cloud → gateway: gateway unbind notice (app deleted the gateway)
TUYA_LINK_TOPIC_DEVICE_UNBIND_NOTICE = "tylink/{device_id}/device/unbind/notice"

# Gateway → cloud: report eligible HA sub-devices (device discovery)
TUYA_LINK_TOPIC_HA_DEVICES = "tylink/{device_id}/device/list/found"
# Cloud → gateway: confirm adding sub-devices
TUYA_LINK_TOPIC_SUBDEVICE_TO_BIND = "tylink/{device_id}/device/topo/add/notify"
# Gateway → cloud: response after processing topo/add/notify
TUYA_LINK_TOPIC_SUBDEVICE_TO_BIND_RESPONSE = (
    "tylink/{device_id}/device/topo/add/notify_response"
)

# OpenAPI gateway deletion path
# Periodic cloud topo sync interval (seconds)
TOPO_SYNC_INTERVAL_SECONDS = 3600  # 1 hour

# Maximum sub-device IDs per topo/get request
TOPO_GET_PAGE_SIZE = 100

# Domain priority order for PID inference — later entries have HIGHER priority.
# When a device exposes multiple domains the last matching domain wins.
# e.g. if camera and switch both exist, camera is selected.
DOMAIN_PRIORITY_ORDER: tuple[str, ...] = (
    "switch",
    "light",
    "climate",
    "cover",
    "humidifier",
    "dehumidifier",
    "vacuum",
    "media_player",
    "water_heater",
    "camera",
    "fan",
)

TUYA_OPENAPI_GATEWAY_DELETE_PATH = "/v1.0/end-user/devices/ha/gateway"
