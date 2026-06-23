"""std:value_to_service converter.

Maps Tuya enum values to specific HA service calls.
Used for devices like vacuum where each command maps to a distinct service.

converter_config options:
- value_map: {tuya_enum_value: "domain.service"} (e.g. {"start": "vacuum.start"})
- state_map: {"ha_state": "tuya_value"} reverse mapping for to_tuya
"""

from __future__ import annotations

from typing import Any


class StdValueToService:

    def to_ha_service(
        self,
        tuya_value: Any,
        config: dict[str, Any],
        entity_id: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if tuya_value is None:
            return None

        value_map = config.get("value_map", {})
        service_ref = value_map.get(str(tuya_value))
        if not service_ref:
            return None

        parts = service_ref.split(".", 1)
        if len(parts) != 2:
            return None

        domain, service = parts
        return {
            "domain": domain,
            "service": service,
            "service_data": {"entity_id": entity_id},
        }

    def to_tuya_value(
        self,
        ha_state: dict[str, Any],
        ha_attributes: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Any:
        state_map = config.get("state_map")
        if state_map:
            state = ha_state.get("state", "")
            return state_map.get(state)

        # Auto-reverse from value_map: find which tuya value maps to a
        # service matching the current HA state
        value_map = config.get("value_map", {})
        state = ha_state.get("state", "")
        for tuya_val, service_ref in value_map.items():
            if service_ref.endswith(f".{state}") or tuya_val == state:
                return tuya_val

        return None
