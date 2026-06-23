"""std:bool_switch converter.

Handles boolean DP ↔ HA on/off services.

converter_config options:
- service_on: HA service when True (default: "turn_on")
- service_off: HA service when False (default: "turn_off")
- service_data: extra service_data merged when calling (e.g. {"oscillating": true})
- state_attr: HA attribute to read for to_tuya (default: uses state == "on")
- ha_attr: HA attribute name for to_tuya (if present, reads from attributes)
- invert: if true, invert the boolean (default: false)
"""

from __future__ import annotations

from typing import Any


class StdBoolSwitch:

    def to_ha_service(
        self,
        tuya_value: Any,
        config: dict[str, Any],
        entity_id: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        invert = config.get("invert", False)
        value = bool(tuya_value) != invert

        domain = entity_id.split(".")[0] if "." in entity_id else "switch"
        service_on = config.get("service_on", "turn_on")
        service_off = config.get("service_off", "turn_off")

        service = service_on if value else service_off
        service_data: dict[str, Any] = {"entity_id": entity_id}

        extra = config.get("service_data")
        if isinstance(extra, dict):
            if value:
                service_data.update(extra)
            else:
                inverted_extra = {
                    k: (not v if isinstance(v, bool) else v)
                    for k, v in extra.items()
                }
                service_data.update(inverted_extra)

        return {"domain": domain, "service": service, "service_data": service_data}

    def to_tuya_value(
        self,
        ha_state: dict[str, Any],
        ha_attributes: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Any:
        invert = config.get("invert", False)

        ha_attr = config.get("ha_attr")
        if ha_attr and ha_attr in ha_attributes:
            value = bool(ha_attributes[ha_attr])
        else:
            state_attr = config.get("state_attr")
            if state_attr:
                value = ha_state.get("state") != "off" if state_attr == "hvac_mode" else bool(ha_attributes.get(state_attr))
            else:
                # Default: any non-off state means "on". Covers on/off domains
                # (switch/light/fan/humidifier) AND mode-state domains like
                # water_heater whose state is an operation mode string
                # (performance/eco/...), never literally "on". unavailable/
                # unknown are treated as off so a transient bad state never
                # reports the device as on.
                value = ha_state.get("state") not in ("off", "unavailable", "unknown")

        return value != invert
