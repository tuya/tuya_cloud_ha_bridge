"""Tuya OpenAPI client for creating HA gateway devices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    LOGGER,
    TUYA_OPENAPI_CATEGORY_PID_MAPPINGS_PATH,
    TUYA_OPENAPI_GATEWAY_ACTIVE_PATH,
    TUYA_OPENAPI_GATEWAY_DELETE_PATH,
    TUYA_OPENAPI_SUB_DEVICE_LIMIT_PATH,
)
from .region_mapping import get_region_endpoints_for_api_key


@dataclass(frozen=True, slots=True)
class HaGatewayResult:
    """Result from the create HA gateway API."""

    product_id: str
    device_id: str
    device_name: str
    device_secret: str
    short_url: str


@dataclass(frozen=True, slots=True)
class HaCategoryPidMapping:
    """A single HA category to Tuya product ID mapping."""

    code: str
    product_id: str


class TuyaOpenApiError(Exception):
    """Raised when a Tuya OpenAPI call fails."""

    def __init__(self, error_code: str, error_msg: str) -> None:
        """Initialize the error."""
        super().__init__(f"{error_code}: {error_msg}")
        self.error_code = error_code
        self.error_msg = error_msg


async def async_create_ha_gateway(
    hass: HomeAssistant, api_key: str
) -> HaGatewayResult:
    """Call Tuya OpenAPI to create an HA gateway device.

    POST https://{api_gateway_host}/v1.0/end-user/devices/ha/gateway/active
    Authorization: Bearer {api_key}

    Returns HaGatewayResult with productId, deviceId, deviceName,
    deviceSecret and shortUrl from the cloud response.
    """
    endpoints = get_region_endpoints_for_api_key(api_key)
    url = f"https://{endpoints.api_gateway_host}{TUYA_OPENAPI_GATEWAY_ACTIVE_PATH}"

    LOGGER.debug("POST %s", url)
    session = async_get_clientsession(hass)
    resp = await session.post(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    resp.raise_for_status()
    body: dict[str, Any] = await resp.json()
    LOGGER.debug("POST %s response: %s", url, body)

    if not body.get("success"):
        error_code = body.get("errorCode", "UNKNOWN")
        error_msg = body.get("errorMsg", "Unknown error")
        raise TuyaOpenApiError(str(error_code), str(error_msg))

    result = body.get("result")
    if not isinstance(result, dict):
        raise TuyaOpenApiError("INVALID_RESPONSE", "Missing result in response")

    product_id = result.get("productId", "")
    device_id = result.get("deviceId", "")
    device_secret = result.get("deviceSecret", "")

    if not product_id or not device_id or not device_secret:
        raise TuyaOpenApiError(
            "INVALID_RESPONSE",
            "Missing required fields in gateway result",
        )

    return HaGatewayResult(
        product_id=str(product_id),
        device_id=str(device_id),
        device_name=str(result.get("deviceName") or "Gateway"),
        device_secret=str(device_secret),
        short_url=str(result.get("shortUrl") or ""),
    )


async def async_delete_ha_gateway(
    hass: HomeAssistant, api_key: str, device_id: str
) -> bool:
    """Call Tuya OpenAPI to delete an HA gateway device.

    DELETE https://{api_gateway_host}/v1.0/end-user/devices/ha/gateway
    Authorization: Bearer {api_key}

    Returns True if the gateway was deleted successfully.
    """
    endpoints = get_region_endpoints_for_api_key(api_key)
    url = f"https://{endpoints.api_gateway_host}{TUYA_OPENAPI_GATEWAY_DELETE_PATH}"

    LOGGER.debug("DELETE %s request: deviceId=%s", url, device_id)
    session = async_get_clientsession(hass)
    resp = await session.delete(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"deviceId": device_id},
        timeout=10,
    )
    resp.raise_for_status()
    body: dict[str, Any] = await resp.json()
    LOGGER.debug("DELETE %s response: %s", url, body)

    if not body.get("success"):
        error_code = body.get("errorCode", "UNKNOWN")
        error_msg = body.get("errorMsg", "Unknown error")
        raise TuyaOpenApiError(str(error_code), str(error_msg))

    return bool(body.get("result", False))


async def async_get_sub_device_limit(
    hass: HomeAssistant, api_key: str
) -> int:
    """Call Tuya OpenAPI to get the sub-device count limit.

    GET https://{api_gateway_host}/v1.0/end-user/devices/ha/sub/device/limit
    Authorization: Bearer {api_key}

    Returns the limitCount value from the cloud response.
    """
    endpoints = get_region_endpoints_for_api_key(api_key)
    url = f"https://{endpoints.api_gateway_host}{TUYA_OPENAPI_SUB_DEVICE_LIMIT_PATH}"

    LOGGER.debug("GET %s", url)
    session = async_get_clientsession(hass)
    resp = await session.get(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    resp.raise_for_status()
    body: dict[str, Any] = await resp.json()
    LOGGER.debug("GET %s response: %s", url, body)

    if not body.get("success"):
        error_code = body.get("errorCode", "UNKNOWN")
        error_msg = body.get("errorMsg", "Unknown error")
        raise TuyaOpenApiError(str(error_code), str(error_msg))

    result = body.get("result")
    if not isinstance(result, dict):
        raise TuyaOpenApiError("INVALID_RESPONSE", "Missing result in response")

    limit_count = result.get("limitCount")
    if not isinstance(limit_count, int):
        raise TuyaOpenApiError(
            "INVALID_RESPONSE", "Missing limitCount in response"
        )

    return limit_count


async def async_get_category_pid_mappings(
    hass: HomeAssistant, api_key: str
) -> list[HaCategoryPidMapping]:
    """Fetch HA category-PID mapping list from Tuya cloud.

    GET https://{api_gateway_host}/v1.0/end-user/services/ha/category/pid/mappings
    Authorization: Bearer {api_key}

    Returns a list ordered by domain priority (first = highest).
    """
    endpoints = get_region_endpoints_for_api_key(api_key)
    url = (
        f"https://{endpoints.api_gateway_host}"
        f"{TUYA_OPENAPI_CATEGORY_PID_MAPPINGS_PATH}"
    )

    LOGGER.debug("GET %s", url)
    session = async_get_clientsession(hass)
    resp = await session.get(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    resp.raise_for_status()
    body: dict[str, Any] = await resp.json()
    LOGGER.debug("GET %s response: %s", url, body)

    if not body.get("success"):
        error_code = body.get("errorCode", "UNKNOWN")
        error_msg = body.get("errorMsg", "Unknown error")
        raise TuyaOpenApiError(str(error_code), str(error_msg))

    result = body.get("result")
    if not isinstance(result, list):
        raise TuyaOpenApiError(
            "INVALID_RESPONSE", "Missing result list in response"
        )

    mappings: list[HaCategoryPidMapping] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        product_id = item.get("productId")
        if isinstance(code, str) and code and isinstance(product_id, str) and product_id:
            mappings.append(HaCategoryPidMapping(code=code, product_id=product_id))

    return mappings
