"""std:numeric_scale converter.

Handles numeric DP ↔ HA attribute with linear scaling.

converter_config options:
- ha_attr: HA attribute to read (e.g. "percentage", "brightness", "temperature")
- service: HA service to call (e.g. "set_percentage", "set_temperature")
- service_data_key: key in service_data for the value
- tuya_min: Tuya value range minimum (default: 0)
- tuya_max: Tuya value range maximum (default: 100)
- ha_min: HA value range minimum (default: 0)
- ha_max: HA value range maximum (default: 100)
- scale: explicit scale factor (overrides min/max calculation)
- offset: offset added after scaling to_ha, subtracted before scaling to_tuya (default: 0)
- default: default Tuya value when HA attr is missing
- round_digits: decimal places to round to (default: 0 = integer)
"""

from __future__ import annotations

from typing import Any


class StdNumericScale:

    def to_ha_service(
        self,
        tuya_value: Any,
        config: dict[str, Any],
        entity_id: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if tuya_value is None:
            return None

        try:
            numeric = float(tuya_value)
        except (TypeError, ValueError):
            return None

        ha_value = self._tuya_to_ha(numeric, config)

        service = config.get("service")
        if not service:
            return None

        domain = entity_id.split(".")[0] if "." in entity_id else "switch"
        service_data_key = config.get("service_data_key", config.get("ha_attr", "value"))

        return {
            "domain": domain,
            "service": service,
            "service_data": {
                "entity_id": entity_id,
                service_data_key: ha_value,
            },
        }

    def to_tuya_value(
        self,
        ha_state: dict[str, Any],
        ha_attributes: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Any:
        # Source of the numeric value: either an entity attribute (``ha_attr``)
        # or the entity STATE itself (``state_attr``). ``state_attr: "state"`` is
        # used by read-only sensor DPs (temperature/humidity/battery) whose Tuya
        # value mirrors the entity's current state — HA exposes the reading as
        # the entity state, not as an attribute.
        state_attr = config.get("state_attr")
        ha_attr = config.get("ha_attr")
        if state_attr:
            ha_value = (
                ha_state.get("state")
                if state_attr == "state"
                else ha_attributes.get(state_attr)
            )
        elif ha_attr:
            ha_value = ha_attributes.get(ha_attr)
        else:
            return config.get("default")

        if ha_value is None:
            return config.get("default")

        try:
            numeric = float(ha_value)
        except (TypeError, ValueError):
            return config.get("default")

        return self._ha_to_tuya(numeric, config)

    def _tuya_to_ha(self, tuya_value: float, config: dict[str, Any]) -> int | float:
        scale = config.get("scale")
        offset = config.get("offset", 0)
        round_digits = config.get("round_digits", 0)

        if scale is not None:
            result = tuya_value / float(scale) + offset
        else:
            tuya_min = config.get("tuya_min", 0)
            tuya_max = config.get("tuya_max", 100)
            ha_min = config.get("ha_min", 0)
            ha_max = config.get("ha_max", 100)

            tuya_range = tuya_max - tuya_min
            ha_range = ha_max - ha_min

            if tuya_range == 0:
                result = float(ha_min)
            else:
                result = (tuya_value - tuya_min) / tuya_range * ha_range + ha_min + offset

        if round_digits == 0:
            return int(round(result))
        return round(result, round_digits)

    def _ha_to_tuya(self, ha_value: float, config: dict[str, Any]) -> int:
        scale = config.get("scale")
        offset = config.get("offset", 0)

        if scale is not None:
            result = (ha_value - offset) * float(scale)
        else:
            tuya_min = config.get("tuya_min", 0)
            tuya_max = config.get("tuya_max", 100)
            ha_min = config.get("ha_min", 0)
            ha_max = config.get("ha_max", 100)

            ha_range = ha_max - ha_min
            tuya_range = tuya_max - tuya_min

            if ha_range == 0:
                result = float(tuya_min)
            else:
                result = (ha_value - offset - ha_min) / ha_range * tuya_range + tuya_min

        # A Tuya value DP always carries an INTEGER raw value — that is precisely
        # why the thing model has a `scale`. So `round_digits` governs only the
        # HA-side precision (Tuya→HA above); this direction always rounds to int.
        # Sharing one round_digits across both directions broke fractional HA
        # attributes: media_player `volume_level` is 0.0-1.0, so it needs
        # round_digits=2 inbound, which would otherwise emit 35.1 to a value DP.
        return int(round(result))
