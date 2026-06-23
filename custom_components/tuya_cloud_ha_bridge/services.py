"""Service helpers for Tuya HA."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, DOMAIN_PRIORITY_ORDER, LOGGER
from .pidspec.service_capability import feature_int_supports

# Auxiliary carrier domains a DP can legitimately route to. These are HA control
# *component* entities (e.g. a vacuum's suction `select`, a target `number`, a
# `button`) that sub-controls of a device get bound to via multi-carrier DP
# routing — distinct from DOMAIN_PRIORITY_ORDER, which only ranks a device's
# *primary* domain. Service calls land on the carrier entity, so these must be
# callable even though they are never a primary domain.
CARRIER_SERVICE_DOMAINS: frozenset[str] = frozenset(
    ("select", "number", "button", "text", "siren", "lock", "valve")
)

ALLOWED_SERVICE_DOMAINS: frozenset[str] = frozenset(
    (
        *DOMAIN_PRIORITY_ORDER,
        *CARRIER_SERVICE_DOMAINS,
        "homeassistant",
        "scene",
        "automation",
        "script",
    )
)


def _entity_supports_service(
    hass: HomeAssistant,
    domain: str,
    service: str,
    entity_id: str | None,
) -> bool:
    """Return True if *entity_id* advertises the feature *service* requires.

    Runtime last-resort net (method A): bind-time trimming
    (``prune_value_map``) is the primary control, this only catches the window
    before a capability change is re-bound. Feature requirements live in the
    shared ``pidspec.service_capability`` module. Calls without a resolvable
    entity / state pass through; the try/except around async_call is the final
    safety net.
    """
    if not entity_id:
        return True
    state = hass.states.get(entity_id)
    if state is None:
        # Entity not loaded yet — don't pre-emptively block; let HA decide.
        return True
    return feature_int_supports(
        domain, service, state.attributes.get("supported_features", 0)
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
        if not isinstance(domain, str) or not isinstance(service, str) or not service:
            continue
        if domain not in ALLOWED_SERVICE_DOMAINS:
            LOGGER.warning("Blocked service call to disallowed domain: %s.%s", domain, service)
            continue
        if not isinstance(service_data, dict):
            service_data = {}
        # Capability gate: skip services the target entity declares it does not
        # support (e.g. a category-level vacuum.pause on a model without the
        # PAUSE feature). Avoids firing a call HA would only reject with
        # "does not support action", which otherwise surfaces as a warning.
        entity_id = service_data.get("entity_id")
        if isinstance(entity_id, list):
            entity_id = entity_id[0] if entity_id else None
        if not _entity_supports_service(hass, domain, service, entity_id):
            LOGGER.debug(
                "Skipping %s.%s for %s: entity does not support this feature",
                domain,
                service,
                entity_id or "unknown",
            )
            continue
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
