"""Tuya regional endpoint mapping for API gateway and MQTT."""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_REGION_KEY = "cn"


@dataclass(frozen=True, slots=True)
class TuyaRegionEndpoints:
    """Resolved Tuya endpoints for one region."""

    key: str
    api_gateway_host: str
    mqtt_broker: str


_REGION_ENDPOINTS: dict[str, TuyaRegionEndpoints] = {
    "cn": TuyaRegionEndpoints(
        key="cn",
        api_gateway_host="openapi.tuyacn.com",
        mqtt_broker="m1.tuyacn.com",
    ),
    "us": TuyaRegionEndpoints(
        key="us",
        api_gateway_host="openapi.tuyaus.com",
        mqtt_broker="m1.tuyaus.com",
    ),
    "eu": TuyaRegionEndpoints(
        key="eu",
        api_gateway_host="openapi.tuyaeu.com",
        mqtt_broker="m1.tuyaeu.com",
    ),
    "in": TuyaRegionEndpoints(
        key="in",
        api_gateway_host="openapi.tuyain.com",
        mqtt_broker="m1.tuyain.com",
    ),
    "sg": TuyaRegionEndpoints(
        key="sg",
        api_gateway_host="openapi-sg.iotbing.com",
        mqtt_broker="m1-sg.lifeaiot.com",
    ),
}


# Tuya API keys use the two letters after `sk-` to identify region.
_API_KEY_REGION_PREFIXES: dict[str, str] = {
    "AY": "cn",
    "AZ": "us",
    "EU": "eu",
    "IN": "in",
    "SG": "sg",
}


def _extract_api_key_region_prefix(api_key: str | None) -> str | None:
    """Extract the 2-letter region prefix from a Tuya API key."""
    if not isinstance(api_key, str):
        return None

    normalized = api_key.strip()
    if len(normalized) < 5 or not normalized.lower().startswith("sk-"):
        return None

    prefix = normalized[3:5].upper()
    return prefix if prefix.isalpha() else None


def is_api_key_region_supported(api_key: str | None) -> bool:
    """Return True if the API key maps to a supported region."""
    prefix = _extract_api_key_region_prefix(api_key)
    if prefix is None:
        return False
    return prefix in _API_KEY_REGION_PREFIXES


def get_region_key_for_api_key(api_key: str | None) -> str:
    """Return the resolved region key for a Tuya API key."""
    if (prefix := _extract_api_key_region_prefix(api_key)) is not None:
        return _API_KEY_REGION_PREFIXES.get(prefix, DEFAULT_REGION_KEY)

    return DEFAULT_REGION_KEY


def get_region_endpoints_for_api_key(api_key: str | None) -> TuyaRegionEndpoints:
    """Return Tuya regional endpoints for a Tuya API key."""
    return _REGION_ENDPOINTS[get_region_key_for_api_key(api_key)]
