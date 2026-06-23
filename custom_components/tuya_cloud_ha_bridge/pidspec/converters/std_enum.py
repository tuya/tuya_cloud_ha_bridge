"""std:enum_passthrough converter.

Handles enum DP ↔ HA attribute/service with optional value mapping.

converter_config options:
- ha_attr: HA attribute to read/write (e.g. "preset_mode", "fan_mode", "direction")
- service: HA service to call (e.g. "set_preset_mode", "set_fan_mode").
  **Representative / default only.** The runtime dispatcher uses ``value_map``
  to pick the service per Tuya enum value; the top-level ``service`` field is
  not consulted for dispatch. Kept in config for human readability and for
  callers that walk the config without iterating value_map.
- service_data_key: key in service_data for the value (e.g. "preset_mode").
  Ignored when ``target_only`` is True.
- target_only: bool (default False). When True, the resolved service accepts
  only the ``entity_id`` target — value is omitted from service_data to
  avoid HA's "extra keys not allowed" error. Cover services (open_cover /
  close_cover / stop_cover / toggle) are target-only.
- value_map: {tuya_value: ha_value} mapping. Tuya values NOT in the map cause
  the converter to return None (no service call dispatched) — prevents
  mis-action when a device cannot support a particular command.
- reverse_map: {ha_value: tuya_value} mapping (auto-generated from value_map if absent)
- default: default Tuya value when HA attr is missing
"""

from __future__ import annotations

from typing import Any


class StdEnumPassthrough:

    def to_ha_service(
        self,
        tuya_value: Any,
        config: dict[str, Any],
        entity_id: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if tuya_value is None:
            return None

        # value_map is the single source of truth. A Tuya value with no
        # entry means "this device cannot perform this command" — drop the
        # call rather than misdispatching to the top-level service.
        value_map = config.get("value_map")
        if value_map is not None:
            if str(tuya_value) not in value_map:
                return None
            ha_value = value_map[str(tuya_value)]
        else:
            ha_value = tuya_value

        # Resolve which service to call. Prefer the value-mapped name when
        # value_map is present (each Tuya value can target a different
        # service); fall back to the top-level service field otherwise.
        if value_map is not None:
            service = ha_value
        else:
            service = config.get("service")
        if not service:
            return None

        domain = entity_id.split(".")[0] if "." in entity_id else "switch"
        service_data: dict[str, Any] = {"entity_id": entity_id}

        # For target-only services (cover.open_cover / close_cover / stop_cover
        # / toggle / etc.) HA rejects any extra key in service_data. The Skill
        # can opt into this via the ``target_only`` flag in converter_config;
        # when not set, we default to the legacy behaviour of writing
        # service_data_key: ha_value for non-target-only services.
        if not config.get("target_only", False):
            service_data_key = config.get(
                "service_data_key", config.get("ha_attr", "value")
            )
            service_data[service_data_key] = ha_value

        return {
            "domain": domain,
            "service": service,
            "service_data": service_data,
        }

    def to_tuya_value(
        self,
        ha_state: dict[str, Any],
        ha_attributes: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Any:
        # Source of the HA value: either an entity attribute (``ha_attr``) or the
        # entity STATE itself (``state_attr``). ``state_attr`` is used by
        # read-only DPs whose Tuya value mirrors the entity's current state —
        # e.g. water_heater ``work_state`` reflects the current operation, which
        # HA exposes as the entity state, not as an attribute.
        def _read(src: str) -> Any:
            # "state" → entity state; anything else → that attribute.
            return ha_state.get("state") if src == "state" else ha_attributes.get(src)

        state_attr = config.get("state_attr")
        ha_attr = config.get("ha_attr")
        if state_attr:
            ha_value = _read(state_attr)
        elif ha_attr:
            ha_value = ha_attributes.get(ha_attr)
        else:
            ha_value = None

        # Fallback source: when the primary value is unavailable (e.g. the
        # OPTIONAL climate ``hvac_action`` attribute is missing on this device),
        # derive the value from a secondary source — typically the entity
        # state/mode — via ``fallback_map``. This keeps the reported value in
        # the primary's vocabulary (e.g. hvac_mode "cool" → action "cooling"),
        # so the declared enum range stays consistent.
        if ha_value is None:
            fb_attr = config.get("fallback_attr")
            if fb_attr:
                fb_value = _read(fb_attr)
                if fb_value is not None:
                    fb_map = config.get("fallback_map")
                    if isinstance(fb_map, dict):
                        return fb_map.get(str(fb_value), config.get("default"))
                    return fb_value

        if ha_value is None:
            return config.get("default")

        reverse_map = config.get("reverse_map")
        if reverse_map:
            if str(ha_value) not in reverse_map:
                # HA exposes a value that this PID's tuya_enum does not
                # support — surface as None so the dispatcher drops it,
                # rather than reporting a stray value to Tuya.
                return None
            return reverse_map[str(ha_value)]

        value_map = config.get("value_map")
        if value_map:
            inv = {v: k for k, v in value_map.items()}
            if str(ha_value) not in inv:
                return None
            return inv[str(ha_value)]

        return ha_value
