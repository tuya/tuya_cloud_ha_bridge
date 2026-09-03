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

from ...const import LOGGER


# ---------------------------------------------------------------------------
# Rule↔code contract guards
# ---------------------------------------------------------------------------
#
# A group: converter's ``role`` vocabulary and the HA attributes each role reads
# are CODE constants — a rule can only pick from what is implemented here. When
# a rule names a role this code does not know, or points ``ha_attr`` at an
# attribute the bound entity never publishes, the converter used to just emit
# nothing: the rule looked correctly configured while the DP was silently dead.
# That is exactly how the light group kept reading the long-removed mired
# ``color_temp`` attribute unnoticed. These guards make such a mismatch loud.
#
# Both run on hot paths (every inbound DP / every state report), so each distinct
# problem is logged once per process.

# The role vocabularies below ARE the rule↔code contract for each group. Keep
# them in sync with references/converter-config-declarations.md when adding one.
_LIGHT_ROLES = frozenset({"brightness", "color_temp", "color_data", "work_mode"})
_CLIMATE_ROLES = frozenset({
    "temp_setpoint", "temp_celsius", "temp_fahrenheit",
    "current_temp", "current_celsius", "current_fahrenheit", "unit_report",
})
_COVER_ROLES = frozenset({"position", "control"})

_WARNED: set[tuple] = set()


def _warn_once(key: tuple, message: str, *args: Any) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    LOGGER.warning(message, *args)


def _check_roles(group: str, routes: list[Any], known_roles: frozenset[str]) -> None:
    """Warn when a route declares a ``role`` this converter does not implement."""
    for route in routes:
        role = (route.converter_config or {}).get("role")
        if role in known_roles:
            continue
        _warn_once(
            ("role", group, role),
            "%s: rule declares unknown role %r on dp %s (entity %s) — this "
            "converter implements %s; the DP will not be converted. Fix the rule "
            "or add the role to the converter.",
            group, role, getattr(route, "dpcode", "?"),
            getattr(route, "entity_id", "?"), sorted(known_roles),
        )


def _check_ha_attr(
    group: str, role: str, attr: str, ha_attributes: dict[str, Any]
) -> None:
    """Warn when ``ha_attr`` names an attribute the entity does not publish.

    Only a MISSING KEY is reported. A present-but-None value is normal (HA nulls
    brightness/colour attributes while a light is off) and must stay quiet.
    """
    if attr in ha_attributes:
        return
    _warn_once(
        ("attr", group, role, attr),
        "%s: role %r reads ha_attr %r but the entity does not publish that "
        "attribute (has: %s) — nothing will be reported for this DP. The HA "
        "attribute may have been renamed or removed.",
        group, role, attr, sorted(ha_attributes)[:12],
    )


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


# Fixed protocol constants (device-independent, so they live in code — never in
# a rule): Tuya encodes colour_data's s/v on 0-1000, HA's brightness on 0-255.
_TUYA_HSV_MAX = 1000
_HA_BRIGHTNESS_MAX = 255

# HA ColorMode values that mean "the lamp is currently showing a COLOUR" (as
# opposed to onoff / brightness / color_temp / white). Keep in sync with the
# `color` feature gate in feature_rules.domain_attr_features.light, which tests
# the same set against supported_color_modes.
_HA_COLOR_MODES = frozenset({"hs", "rgb", "rgbw", "rgbww", "xy"})


def _clamp(value: float, bound_a: float, bound_b: float) -> float:
    lo, hi = (bound_a, bound_b) if bound_a <= bound_b else (bound_b, bound_a)
    return max(lo, min(hi, value))


def _as_float(value: Any) -> float | None:
    """Return *value* as a float, or None when it is absent/not numeric.

    HA sets ``brightness`` / ``color_temp_kelvin`` / ``hs_color`` to ``None``
    while the light is OFF — the attribute key still exists, so ``.get(k, dflt)``
    yields None rather than the default. Every read goes through here so an
    off light degrades to "report nothing for this DP" instead of raising.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dp_linear_scale(cfg: dict[str, Any]) -> float:
    """Linear multiplier between the HA value and the Tuya DP integer.

    Same convention as ``std:numeric_scale``: ``scale`` is 10^(tuya_scale), so
    HA→Tuya multiplies and Tuya→HA divides. Absent/invalid → 1 (pure
    pass-through, the default for this group).
    """
    try:
        scale = float(cfg.get("scale", 1))
    except (TypeError, ValueError):
        return 1.0
    return scale if scale > 0 else 1.0


def _entity_attributes(context: dict[str, Any] | None, entity_id: str) -> dict[str, Any]:
    """Live attributes of a bound entity, injected into ``context`` at dispatch.

    Inbound converters only receive (value, config, entity_id, context) — they
    cannot read HA. But a mode switch carries no value, so filling one in needs
    the entity's CURRENT attributes. ``pidspec_build_service_calls`` holds both
    ``hass`` and the route table, so it seeds ``context["entity_states"]`` there
    (same pattern as the existing ``last_cover_control``: a command that does not
    carry the value it needs).
    """
    states = (context or {}).get("entity_states") or {}
    entry = states.get(entity_id) or {}
    attrs = entry.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _hs_from_last_colour_data(raw: Any) -> list[float] | None:
    """Decode a previously reported colour_data JSON back into HA hs_color."""
    if raw is None:
        return None
    try:
        hsv = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(hsv, dict):
        return None
    h = _as_float(hsv.get("h"))
    s = _as_float(hsv.get("s"))
    if h is None or s is None:
        return None
    return [round(h, 1), round(_scale(s, 0, _TUYA_HSV_MAX, 0, 100), 1)]


def _mired_midpoint(attrs: dict[str, Any]) -> float | None:
    """Middle of the lamp's OWN colour-temperature span, in kelvin.

    Taken as the midpoint in **mired** (1e6/K), not kelvin: perceived colour
    temperature is linear in mired, so a kelvin midpoint lands noticeably cool.
    For a 2000-6535 K lamp that is 3063 K, not 4268 K. Range comes from the
    entity at runtime, so each lamp gets its own — no literal in the rules.
    """
    lo = _as_float(attrs.get("min_color_temp_kelvin"))
    hi = _as_float(attrs.get("max_color_temp_kelvin"))
    if not lo or not hi or lo <= 0 or hi <= 0:
        return None
    return 1e6 / ((1e6 / lo + 1e6 / hi) / 2)


def _to_tuya_scalar(value: float, cfg: dict[str, Any]) -> int:
    """Scale a HA value into the Tuya DP integer and clamp it to the DP contract.

    ``tuya_contract`` is a PID-level fact (the DP's thing-model bounds), not a
    device-level range — clamping to it does not freeze one device's span onto
    the others, it just keeps us from publishing a value the DP cannot hold.
    Needed because HA's own span is not always a subset of the DP's: standard
    ``bright_value`` starts at 10 while HA brightness starts at 0, so an off
    lamp (0) and the dimmest steps (1-9) would fall outside the contract.
    """
    result = value * _dp_linear_scale(cfg)
    contract = cfg.get("tuya_contract") or {}
    lo, hi = contract.get("min"), contract.get("max")
    if lo is not None:
        result = max(float(lo), result)
    if hi is not None:
        result = min(float(hi), result)
    return int(round(result))


class LightColorGroupConverter:
    """group:light_color — colour_data / bright_value / temp_value coupling.

    **Scalar DPs pass through.** ``bright_value`` carries HA's ``brightness``
    verbatim and ``temp_value`` carries HA's ``color_temp_kelvin`` verbatim
    (``scale`` may apply a 10^n multiplier, nothing else). No range mapping
    happens here, by design:

    - a PID covers a whole device *class*, so its DP contract is the widest
      envelope of that class — never one lamp's range;
    - the authoritative range is whatever HA reports for THAT lamp, declared to
      the cloud at bind time from the ``min_attr``/``max_attr``/``fallback``
      keys (see ``pidspec_bridge._resolve_dp_properties``);
    - so a rule must not carry ``tuya_min``/``tuya_max``/``ha_min``/``ha_max``
      for these DPs — that would freeze one device's range onto every device
      sharing the PID.

    ``colour_data`` is the exception: it is a JSON struct whose inner HSV
    encoding is a fixed Tuya protocol convention (h 0-360, s/v 0-1000), so the
    ratio to HA's (0-360, 0-100) + brightness 0-255 lives here as a code
    constant, not in the rules.

    Tuya→HA:
    - colour_data present → hs_color + brightness
    - otherwise bright_value → brightness and/or temp_value → colour temperature

    HA→Tuya:
    - bright_value / temp_value always report whichever attribute is set
    - colour_data additionally reports HSV while the lamp is in a colour mode
      (hs / rgb / rgbw / rgbww / xy) — NOT only literal "hs"
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
        _check_roles("group:light_color", routes, _LIGHT_ROLES)
        config_by_role = {r.converter_config.get("role"): r.converter_config for r in routes}
        # Payload keys are the BOUND dpcodes, which are a rule choice.
        # Always resolve through role → dpcode; never match a dpcode literal.
        dpcode_by_role = {r.converter_config.get("role"): r.dpcode for r in routes}
        service_data: dict[str, Any] = {"entity_id": entity_id}

        dp_color = dpcode_by_role.get("color_data")
        if dp_color and dp_color in group_payload:
            raw = group_payload[dp_color]
            try:
                hsv = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                hsv = None
            if isinstance(hsv, dict):
                h = _as_float(hsv.get("h")) or 0.0
                s = _as_float(hsv.get("s")) or 0.0
                v = _as_float(hsv.get("v")) or 0.0
                service_data["hs_color"] = [
                    round(h, 1),
                    round(_scale(s, 0, _TUYA_HSV_MAX, 0, 100), 1),
                ]
                service_data["brightness"] = int(round(
                    _clamp(_scale(v, 0, _TUYA_HSV_MAX, 0, _HA_BRIGHTNESS_MAX),
                           0, _HA_BRIGHTNESS_MAX)
                ))
        else:
            dp_bri = dpcode_by_role.get("brightness")
            bright_raw = _as_float(group_payload.get(dp_bri)) if dp_bri else None
            if bright_raw is not None:
                cfg = config_by_role.get("brightness", {})
                service_data["brightness"] = int(round(bright_raw / _dp_linear_scale(cfg)))

            dp_ct = dpcode_by_role.get("color_temp")
            temp_raw = _as_float(group_payload.get(dp_ct)) if dp_ct else None
            if temp_raw is not None:
                cfg = config_by_role.get("color_temp", {})
                attr = cfg.get("ha_attr", "color_temp_kelvin")
                service_data[attr] = int(round(temp_raw / _dp_linear_scale(cfg)))

        # ── work_mode 切档 ─────────────────────────────────────────────────
        # 面板切标签页时只发 work_mode，不带任何颜色/色温值，而 HA 没有"只切模式"
        # 的服务 —— light.turn_on 必须带 hs_color 或 color_temp_kelvin 之一，
        # color_mode 才会真的变。所以这里要**补一个值**。
        # 只在同一批 payload 没带对应的值 DP 时才补（面板"切档+给值"一起发时，
        # 用户给的值优先）。
        dp_wm = dpcode_by_role.get("work_mode")
        if dp_wm and dp_wm in group_payload:
            mode = str(group_payload[dp_wm] or "").lower()
            attrs = _entity_attributes(context, entity_id)
            last = (context or {}).get("last_reported") or {}
            if mode == "colour" and "hs_color" not in service_data:
                # 白光档下 HA 一直在派生 hs_color（2000K → [30.6, 94.5]）。用它
                # 切档，灯进入彩光模式而**观感颜色不跳变**，用户再继续拖色轮。
                hs = attrs.get("hs_color")
                if not (isinstance(hs, (list, tuple)) and len(hs) >= 2):
                    hs = _hs_from_last_colour_data(last.get(dpcode_by_role.get("color_data")))
                if hs:
                    service_data["hs_color"] = [round(float(hs[0]), 1), round(float(hs[1]), 1)]
            elif mode == "white" and "color_temp_kelvin" not in service_data:
                cfg_ct = config_by_role.get("color_temp", {})
                attr_ct = cfg_ct.get("ha_attr", "color_temp_kelvin")
                # 彩光档下 HA 的 color_temp_kelvin 是 None，所以要回溯：
                # 当前值 → 上次上报的 → 实体自报范围的 mired 中点。
                kelvin = _as_float(attrs.get(attr_ct))
                if kelvin is None:
                    kelvin = _as_float(last.get(dpcode_by_role.get("color_temp")))
                if kelvin is None:
                    kelvin = _mired_midpoint(attrs)
                if kelvin is not None:
                    service_data[attr_ct] = int(round(kelvin))

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
        _check_roles("group:light_color", routes, _LIGHT_ROLES)
        config_by_role = {r.converter_config.get("role"): r.converter_config for r in routes}
        dpcode_by_role = {r.converter_config.get("role"): r.dpcode for r in routes}
        result: dict[str, Any] = {}

        brightness = _as_float(ha_attributes.get("brightness"))

        dpcode_bri = dpcode_by_role.get("brightness")
        if dpcode_bri:
            cfg = config_by_role.get("brightness", {})
            _check_ha_attr("group:light_color", "brightness", "brightness", ha_attributes)
            if brightness is not None:
                result[dpcode_bri] = _to_tuya_scalar(brightness, cfg)

        dpcode_ct = dpcode_by_role.get("color_temp")
        if dpcode_ct:
            cfg_ct = config_by_role.get("color_temp", {})
            attr_ct = cfg_ct.get("ha_attr", "color_temp_kelvin")
            _check_ha_attr("group:light_color", "color_temp", attr_ct, ha_attributes)
            color_temp = _as_float(ha_attributes.get(attr_ct))
            if color_temp is not None:
                result[dpcode_ct] = _to_tuya_scalar(color_temp, cfg_ct)

        # work_mode 是白光/彩光的**唯一消歧信号**：色温派生出的 hs 和用户手选的
        # 颜色在 colour_data 上完全一样（2900K → h=28,s=597，和色轮上选这个橙
        # 一模一样），云端只能靠 work_mode 区分。由 color_mode 派生，规则不写映射
        # 表 —— HA 的 ColorMode 取值集合是协议常量，属于代码。
        dpcode_wm = dpcode_by_role.get("work_mode")
        if dpcode_wm:
            result[dpcode_wm] = (
                "colour" if ha_attributes.get("color_mode") in _HA_COLOR_MODES
                else "white"
            )

        # colour_data keeps the fixed Tuya HSV encoding (see class docstring).
        # Gate on the light being in ANY colour mode, not literally "hs": an RGB
        # lamp showing a colour reports color_mode "rgb" (or rgbw/rgbww/xy) while
        # HA still publishes the derived hs_color. Matching only "hs" left both
        # colour_data AND colour_temp_kelvin at their type defaults for every RGB
        # lamp — the cloud saw {"h":0,"s":0,"v":0} + 0 K on a lit colour bulb.
        # Gating on hs_color alone would be wrong the other way: in color_temp
        # mode HA also derives an hs_color, and reporting it would tell the panel
        # the lamp is in colour mode when it is showing white.
        dpcode_color = dpcode_by_role.get("color_data")
        hs = ha_attributes.get("hs_color")
        if (
            dpcode_color
            and ha_attributes.get("color_mode") in _HA_COLOR_MODES
            and isinstance(hs, (list, tuple))
            and len(hs) >= 2
        ):
            v_ha = brightness if brightness is not None else float(_HA_BRIGHTNESS_MAX)
            result[dpcode_color] = json.dumps(
                {
                    "h": int(round(_as_float(hs[0]) or 0.0)),
                    "s": int(round(_scale(_as_float(hs[1]) or 0.0, 0, 100, 0, _TUYA_HSV_MAX))),
                    "v": int(round(_clamp(
                        _scale(v_ha, 0, _HA_BRIGHTNESS_MAX, 0, _TUYA_HSV_MAX),
                        0, _TUYA_HSV_MAX,
                    ))),
                },
                separators=(",", ":"),
            )

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

        _check_roles("group:climate_temp", routes, _CLIMATE_ROLES)
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
        # Native value+unit role: the setpoint DP carries the value in the
        # HA-native unit already — de-scale and use directly, NO conversion.
        dp_native = role_to_dp.get("temp_setpoint")
        raw_native = group_payload.get(dp_native) if dp_native else None
        if raw_native is not None:
            temp_value = float(raw_native) / role_to_scale["temp_setpoint"]
            return [{
                "domain": domain,
                "service": "set_temperature",
                "service_data": {"entity_id": entity_id, "temperature": temp_value},
            }]

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
        _check_roles("group:climate_temp", routes, _CLIMATE_ROLES)
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

        # Native (unit-agnostic) roles for the single-value + unit_report scheme:
        # report the HA value AS-IS in its own unit — NO °F↔°C conversion —
        # paired with unit_report so Tuya reads it via the reported unit. Keeps
        # value, unit and declared range all in the device's native unit.
        def _emit_native(attr: str, role: str) -> None:
            raw = ha_attributes.get(attr)
            if raw is None:
                return
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return
            if dpcode := role_to_dp.get(role):
                result[dpcode] = int(round(value * role_to_scale[role]))

        _emit_native("temperature", "temp_setpoint")
        _emit_native("current_temperature", "current_temp")

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
        _check_roles("group:cover_control", routes, _COVER_ROLES)
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
