"""Group converters — handle multiple DPs that share coupled state.

Unlike std: converters which process one DP independently, group: converters
receive all DPs belonging to the same group_id as a batch, enabling
cross-DP context (e.g. color_mode determines which DP to write back).

Protocol:
- tuya_to_ha(group_payload, routes, context) → list of service call dicts
- ha_to_tuya(ha_state, ha_attributes, routes, context) → {dpcode: value}

Built-in groups:
- group:light_color — colour_data / bright_value / temp_value
- group:climate_temp — temp_set / temp_set_f / temp_unit_convert
- group:cover_control — position / control coupling
"""

from __future__ import annotations

import json
from typing import Any, Protocol


class GroupConverter(Protocol):
    """Protocol for group-level DP converters."""

    def tuya_to_ha(
        self,
        group_payload: dict[str, Any],
        routes: list[Any],
        context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Convert a batch of Tuya DP values to HA service calls."""
        ...

    def ha_to_tuya(
        self,
        ha_state: dict[str, Any],
        ha_attributes: dict[str, Any],
        routes: list[Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Convert HA state/attributes to multiple Tuya DP values."""
        ...


def _scale(value: float, from_min: float, from_max: float, to_min: float, to_max: float) -> float:
    if from_max == from_min:
        return to_min
    return to_min + (value - from_min) / (from_max - from_min) * (to_max - to_min)


class LightColorGroupConverter:
    """group:light_color — colour_data / bright_value / temp_value coupling.

    Tuya→HA:
    - colour_data present → extract HSV, produce hs_color + brightness
    - bright_value only → produce brightness
    - temp_value only → produce color_temp

    HA→Tuya:
    - color_mode == "hs" → write colour_data JSON
    - color_mode == "color_temp" → write temp_value + bright_value
    - else → write bright_value
    """

    def tuya_to_ha(
        self,
        group_payload: dict[str, Any],
        routes: list[Any],
        context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not routes:
            return []

        entity_id = routes[0].entity_id
        config_by_role = {r.converter_config.get("role"): r.converter_config for r in routes}
        service_data: dict[str, Any] = {"entity_id": entity_id}

        if "colour_data" in group_payload:
            raw = group_payload["colour_data"]
            hsv = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(hsv, dict):
                h = float(hsv.get("h", 0))
                s = float(hsv.get("s", 0))
                v = float(hsv.get("v", 0))

                # s: Tuya 0-1000 → HA 0-100
                cfg_color = config_by_role.get("color_data", {})
                s_ha = _scale(s, cfg_color.get("tuya_min_s", 0), cfg_color.get("tuya_max_s", 1000), 0, 100)

                service_data["hs_color"] = [round(h, 1), round(s_ha, 1)]

                cfg_bri = config_by_role.get("brightness", {})
                brightness = _scale(
                    v,
                    cfg_bri.get("tuya_min", 0), cfg_bri.get("tuya_max", 1000),
                    cfg_bri.get("ha_min", 0), cfg_bri.get("ha_max", 255),
                )
                service_data["brightness"] = int(round(brightness))

        elif "bright_value" in group_payload:
            cfg = config_by_role.get("brightness", {})
            brightness = _scale(
                float(group_payload["bright_value"]),
                cfg.get("tuya_min", 0), cfg.get("tuya_max", 1000),
                cfg.get("ha_min", 0), cfg.get("ha_max", 255),
            )
            service_data["brightness"] = int(round(brightness))

        elif "temp_value" in group_payload:
            cfg = config_by_role.get("color_temp", {})
            color_temp = _scale(
                float(group_payload["temp_value"]),
                cfg.get("tuya_min", 0), cfg.get("tuya_max", 1000),
                cfg.get("ha_min", 153), cfg.get("ha_max", 500),
            )
            service_data["color_temp"] = int(round(color_temp))

        if len(service_data) <= 1:
            return []

        return [{"domain": "light", "service": "turn_on", "service_data": service_data}]

    def ha_to_tuya(
        self,
        ha_state: dict[str, Any],
        ha_attributes: dict[str, Any],
        routes: list[Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config_by_role = {r.converter_config.get("role"): r.converter_config for r in routes}
        dpcode_by_role = {r.converter_config.get("role"): r.dpcode for r in routes}
        result: dict[str, Any] = {}

        color_mode = ha_attributes.get("color_mode")
        brightness = ha_attributes.get("brightness", 255)

        if color_mode == "hs":
            hs = ha_attributes.get("hs_color", (0, 0))
            cfg = config_by_role.get("brightness", {})
            v = _scale(
                float(brightness),
                cfg.get("ha_min", 0), cfg.get("ha_max", 255),
                cfg.get("tuya_min", 0), cfg.get("tuya_max", 1000),
            )
            cfg_color = config_by_role.get("color_data", {})
            s_tuya = _scale(float(hs[1]), 0, 100, cfg_color.get("tuya_min_s", 0), cfg_color.get("tuya_max_s", 1000))
            hsv = {"h": int(round(float(hs[0]))), "s": int(round(s_tuya)), "v": int(round(v))}
            dpcode = dpcode_by_role.get("color_data")
            if dpcode:
                result[dpcode] = json.dumps(hsv, separators=(",", ":"))

        elif color_mode == "color_temp":
            color_temp = ha_attributes.get("color_temp", 300)
            cfg_ct = config_by_role.get("color_temp", {})
            dpcode_ct = dpcode_by_role.get("color_temp")
            if dpcode_ct:
                result[dpcode_ct] = int(round(_scale(
                    float(color_temp),
                    cfg_ct.get("ha_min", 153), cfg_ct.get("ha_max", 500),
                    cfg_ct.get("tuya_min", 0), cfg_ct.get("tuya_max", 1000),
                )))

            cfg_bri = config_by_role.get("brightness", {})
            dpcode_bri = dpcode_by_role.get("brightness")
            if dpcode_bri:
                result[dpcode_bri] = int(round(_scale(
                    float(brightness),
                    cfg_bri.get("ha_min", 0), cfg_bri.get("ha_max", 255),
                    cfg_bri.get("tuya_min", 0), cfg_bri.get("tuya_max", 1000),
                )))
        else:
            cfg = config_by_role.get("brightness", {})
            dpcode_bri = dpcode_by_role.get("brightness")
            if dpcode_bri:
                result[dpcode_bri] = int(round(_scale(
                    float(brightness),
                    cfg.get("ha_min", 0), cfg.get("ha_max", 255),
                    cfg.get("tuya_min", 0), cfg.get("tuya_max", 1000),
                )))

        return result


def _c_to_f(celsius: float) -> float:
    return celsius * 9 / 5 + 32


def _f_to_c(fahrenheit: float) -> float:
    return (fahrenheit - 32) * 5 / 9


def _dp_scale(cfg: dict[str, Any]) -> float:
    """Scale factor between the Tuya DP integer and the real value.

    Tuya stores temperatures as scaled integers (e.g. scale=10 → DP 255 means
    25.5 °C). Tuya→HA divides by this factor, HA→Tuya multiplies by it. Absent
    or invalid config means scale=1 (whole-degree device, legacy behaviour).
    """
    try:
        scale = float(cfg.get("scale", 1))
    except (TypeError, ValueError):
        return 1.0
    return scale if scale > 0 else 1.0


class ClimateTempGroupConverter:
    """group:climate_temp — unit-aware temperature coupling.

    Works for both climate and water_heater (domain is derived from the bound
    entity, not hard-coded). Couples the Celsius/Fahrenheit DP pair so they are
    never written/reported independently.

    Roles (converter_config.role → DP):
      temp_celsius        °C setpoint DP    (e.g. temp_set,        rw)
      temp_fahrenheit     °F setpoint DP    (e.g. temp_set_f,      rw)
      current_celsius     °C current DP     (e.g. temp_current,    read-only)
      current_fahrenheit  °F current DP     (e.g. temp_current_f,  read-only)
      unit_report         unit DP           (e.g. temp_unit_convert, read-only)

    Active unit = HA system unit (context["temperature_unit"]). The device's
    own unit DP is REPORT-ONLY: it flows HA→Tuya (so Tuya learns the HA unit)
    and is never used Tuya→HA to control HA (the dispatcher filters read-only
    DPs out of the inbound path). Missing roles/DPs are ignored.
    """

    def tuya_to_ha(
        self,
        group_payload: dict[str, Any],
        routes: list[Any],
        context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not routes:
            return []

        role_to_dp = {r.converter_config.get("role"): r.dpcode for r in routes}
        role_to_scale = {
            r.converter_config.get("role"): _dp_scale(r.converter_config) for r in routes
        }
        entity_id = routes[0].entity_id
        domain = entity_id.split(".")[0] if "." in entity_id else "climate"
        use_f = context.get("temperature_unit") == "°F" if context else False

        # Only setpoint roles control HA; current/unit DPs are read-only and
        # never reach here (dispatcher drops read-only routes inbound). Each DP
        # is de-scaled from the Tuya integer into real degrees before use.
        dp_c = role_to_dp.get("temp_celsius")
        dp_f = role_to_dp.get("temp_fahrenheit")
        raw_c = group_payload.get(dp_c) if dp_c else None
        raw_f = group_payload.get(dp_f) if dp_f else None
        val_c = float(raw_c) / role_to_scale["temp_celsius"] if raw_c is not None else None
        val_f = float(raw_f) / role_to_scale["temp_fahrenheit"] if raw_f is not None else None

        temp_value = None
        if use_f:
            if val_f is not None:
                temp_value = val_f
            elif val_c is not None:
                temp_value = _c_to_f(val_c)
        else:
            if val_c is not None:
                temp_value = val_c
            elif val_f is not None:
                temp_value = _f_to_c(val_f)

        if temp_value is None:
            return []

        return [{
            "domain": domain,
            "service": "set_temperature",
            "service_data": {"entity_id": entity_id, "temperature": temp_value},
        }]

    def ha_to_tuya(
        self,
        ha_state: dict[str, Any],
        ha_attributes: dict[str, Any],
        routes: list[Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        role_to_dp = {r.converter_config.get("role"): r.dpcode for r in routes}
        role_to_scale = {
            r.converter_config.get("role"): _dp_scale(r.converter_config) for r in routes
        }
        use_f = context.get("temperature_unit") == "°F" if context else False
        result: dict[str, Any] = {}

        def _emit(attr: str, role_c: str, role_f: str) -> None:
            raw = ha_attributes.get(attr)
            if raw is None:
                return
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return
            # Real degrees in each unit, then scaled back to the Tuya integer.
            if use_f:
                c_deg, f_deg = _f_to_c(value), value
            else:
                c_deg, f_deg = value, _c_to_f(value)
            if dpcode := role_to_dp.get(role_c):
                result[dpcode] = int(round(c_deg * role_to_scale[role_c]))
            if dpcode := role_to_dp.get(role_f):
                result[dpcode] = int(round(f_deg * role_to_scale[role_f]))

        # Setpoint (HA "temperature") and current temperature, each reported in
        # both °C and °F DPs when those DPs exist.
        _emit("temperature", "temp_celsius", "temp_fahrenheit")
        _emit("current_temperature", "current_celsius", "current_fahrenheit")

        # Report-only: tell Tuya which unit the HA device uses.
        if dpcode := role_to_dp.get("unit_report"):
            result[dpcode] = "f" if use_f else "c"

        return result


class CoverControlGroupConverter:
    """group:cover_control — position / control coupling.

    Tuya→HA: position → set_cover_position, control → open/close/stop
    HA→Tuya: reports both position and inferred control state.
    """

    def tuya_to_ha(
        self,
        group_payload: dict[str, Any],
        routes: list[Any],
        context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not routes:
            return []

        entity_id = routes[0].entity_id
        calls: list[dict[str, Any]] = []

        if "position" in group_payload:
            pos = int(group_payload["position"])
            calls.append({
                "domain": "cover",
                "service": "set_cover_position",
                "service_data": {"entity_id": entity_id, "position": pos},
            })
        elif "control" in group_payload:
            control = str(group_payload["control"])
            service_map = {"open": "open_cover", "close": "close_cover", "stop": "stop_cover"}
            service = service_map.get(control)
            if service:
                calls.append({
                    "domain": "cover",
                    "service": service,
                    "service_data": {"entity_id": entity_id},
                })

        return calls

    def ha_to_tuya(
        self,
        ha_state: dict[str, Any],
        ha_attributes: dict[str, Any],
        routes: list[Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        dpcode_by_role = {r.converter_config.get("role"): r.dpcode for r in routes}
        result: dict[str, Any] = {}

        position = ha_attributes.get("current_position")
        if position is not None and (dpcode := dpcode_by_role.get("position")):
            result[dpcode] = int(position)

        state = ha_state.get("state", "")
        if dpcode := dpcode_by_role.get("control"):
            state_map = {"open": "open", "opening": "open", "closed": "close", "closing": "close"}
            if state in state_map:
                result[dpcode] = state_map[state]
            # Use last_cover_control from context if available
            elif context and "last_cover_control" in context:
                result[dpcode] = context["last_cover_control"]

        return result


GROUP_CONVERTERS: dict[str, GroupConverter] = {
    "group:light_color": LightColorGroupConverter(),
    "group:climate_temp": ClimateTempGroupConverter(),
    "group:cover_control": CoverControlGroupConverter(),
}


def get_group_converter(name: str) -> GroupConverter:
    """Return the group converter for *name*. Raises KeyError if unknown."""
    return GROUP_CONVERTERS[name]


def is_group_converter(name: str) -> bool:
    """Return True if name refers to a group converter."""
    return name.startswith("group:")
