"""Service helpers for Tuya HA."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, DOMAIN_PRIORITY_ORDER, LOGGER

ALLOWED_SERVICE_DOMAINS: frozenset[str] = frozenset(
    (*DOMAIN_PRIORITY_ORDER, "homeassistant", "scene", "automation", "script")
)


async def async_execute_service_calls(
    hass: HomeAssistant,
    service_calls: list[dict[str, Any]],
) -> None:
    """Execute pre-mapped HA service calls."""
    for service_call in service_calls:
        domain = service_call.get("domain")
        service = service_call.get("service")
        service_data = service_call.get("service_data", {})
        if not isinstance(domain, str) or not isinstance(service, str):
            continue
        if domain not in ALLOWED_SERVICE_DOMAINS:
            LOGGER.warning("Blocked service call to disallowed domain: %s.%s", domain, service)
            continue
        if not isinstance(service_data, dict):
            service_data = {}
        try:
            await hass.services.async_call(
                domain,
                service,
                service_data,
                blocking=True,
            )
        except HomeAssistantError as err:
            LOGGER.warning(
                "Failed to execute %s.%s for %s: %s",
                domain,
                service,
                service_data.get("entity_id", "unknown"),
                err,
            )
        except Exception:
            LOGGER.exception(
                "Unexpected error executing %s.%s for %s",
                domain,
                service,
                service_data.get("entity_id", "unknown"),
            )


def async_setup_services(hass: HomeAssistant) -> None:
    """Register Tuya HA services."""
