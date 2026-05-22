import json
from typing import Any, Dict

# ------------------------------
# Core config: Tuya AC ↔ HA Climate mapping
# PID: dmbxv4ph2ydxmugq
# ------------------------------
# Tuya AC category (kt) full DP configuration
# Enum DPs (mode/fan_speed_enum/status) are consistent with HA; no intermediate conversion
# During bind, supported enum ranges are reported to HA via range_attr
TUYA_CLIMATE_CORE_DPS = {
    "switch": {"type": "Boolean"},                # DP1  switch (rw)
    "temp_set": {"type": "Integer"},              # DP2  target temp ℃ (rw) min:0 max:50
    "temp_current": {"type": "Integer"},          # DP3  current temp ℃ (ro) min:-20 max:100
    "mode": {"type": "Enum"},                     # DP4  operating mode (rw) consistent with HA hvac_mode
    "fan_speed_enum": {"type": "Enum"},           # DP5  fan speed level (rw) consistent with HA fan_mode
    "status": {"type": "Enum"},                   # DP6  operating status (ro) range:["off","cooling","defrosting","drying","fan","heating","idle","preheating"]
    "wind_shake": {"type": "Enum"},               # DP15 swing mode (rw) range:["horizontal","vertical","both","on","off"]
    "temp_unit_convert": {"type": "Enum"},        # DP19 temp unit switch (report only) range:["c","f"]
    "temp_current_f": {"type": "Integer"},        # DP23 current temp ℉ (ro) min:-40 max:200
    "temp_set_f": {"type": "Integer"},            # DP24 target temp ℉ (rw) min:0 max:100
    "run_mode": {"type": "Enum"},                 # DP101 preset mode (rw) consistent with HA preset_mode
}

# Full DP default values (fallback when ha_to_tuya is missing attributes)
TUYA_DP_DEFAULTS: Dict[str, Any] = {
    "switch": True,
    "temp_set": 26,
    "temp_current": 20,
    "mode": "auto",
    "fan_speed_enum": "auto",
    "status": "off",
    "wind_shake": "off",
    "temp_unit_convert": "c",       # default Celsius
    "temp_set_f": 79,               # 26℃ ≈ 79℉
    "temp_current_f": 68,           # 20℃ ≈ 68℉
    "run_mode": "none",
}

# Property metadata: reported as defaultProperties during bind
# Enum DPs dynamically retrieve supported ranges from HA entities via range_attr; uploaded directly without value_map conversion
TUYA_PROPERTY_METADATA: Dict[str, Dict[str, Any]] = {
    "mode": {
        "range_attr": "hvac_modes",
    },
    "fan_speed_enum": {
        "range_attr": "fan_modes",
    },
    "wind_shake": {
        "range_attr": "swing_modes",
    },
    "run_mode": {
        "range_attr": "preset_modes",
    },
    "temp_set": {
        "unit": "c",
        "dp_properties_attr_map": {"min": "min_temp", "max": "max_temp"},
    },
    "temp_set_f": {
        "unit": "f",
        "dp_properties_attr_map": {"min": "min_temp", "max": "max_temp"},
    },
}


# ------------------------------
# Utility functions
# ------------------------------
def validate_tuya_param(param_name: str, value: Any) -> None:
    """Validate the type of a Tuya AC parameter."""
    config = TUYA_CLIMATE_CORE_DPS.get(param_name)
    if not config:
        return

    if config["type"] == "Boolean":
        if not isinstance(value, bool):
            raise ValueError(f"Parameter {param_name} value {value} has wrong type, must be Boolean")
    elif config["type"] == "Integer":
        if not isinstance(value, int):
            raise ValueError(f"Parameter {param_name} value {value} has wrong type, must be Integer")


def _celsius_to_fahrenheit(celsius: int) -> int:
    """Convert Celsius to Fahrenheit."""
    return round(celsius * 9 / 5 + 32)


def _fahrenheit_to_celsius(fahrenheit: int) -> int:
    """Convert Fahrenheit to Celsius."""
    return round((fahrenheit - 32) * 5 / 9)


def _is_fahrenheit(context: Dict[str, Any] | None) -> bool:
    """Check whether the HA system temperature unit is Fahrenheit."""
    if context and context.get("temperature_unit") == "°F":
        return True
    return False


# ------------------------------
# Core function 1: Tuya AC command → HA Climate command
# ------------------------------
def tuya_to_ha(tuya_dps: Dict[str, Any], ha_entity_id: str, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Parse Tuya AC DPS commands and convert to HA Climate service call parameters.
    Enum DPs (mode/fan_speed_enum/run_mode) are consistent with HA; passed through directly without conversion.
    Temperature is determined by the HA temperature unit:
    - When HA uses ℃: prefer temp_set (pass ℃ directly); convert from temp_set_f to ℃ only if temp_set is absent
    - When HA uses ℉: prefer temp_set_f (pass ℉ directly); convert from temp_set to ℉ only if temp_set_f is absent
    Read-only DPs (temp_current/temp_current_f/status) do not generate service calls.
    """
    ha_params = {
        "domain": "climate",
        "service": "",
        "service_data": {"entity_id": ha_entity_id}
    }
    use_f = _is_fahrenheit(context)

    # 1. Switch command
    switch_val = tuya_dps.get("switch")
    if switch_val is not None:
        validate_tuya_param("switch", switch_val)
        if not switch_val:
            ha_params["service"] = "set_hvac_mode"
            ha_params["service_data"]["hvac_mode"] = "off"
            return ha_params

    # 2. Operating mode → set_hvac_mode (enum passed through directly, consistent with HA hvac_mode)
    if "mode" in tuya_dps:
        ha_mode = tuya_dps["mode"]
        ha_params["service"] = "set_hvac_mode"
        ha_params["service_data"]["hvac_mode"] = ha_mode

    # 3. Preset mode → set_preset_mode (enum passed through directly, consistent with HA preset_mode)
    if "run_mode" in tuya_dps and tuya_dps["run_mode"] is not None:
        preset_mode = tuya_dps["run_mode"]
        ha_params["preset_service"] = {
            "domain": "climate",
            "service": "set_preset_mode",
            "service_data": {
                "entity_id": ha_entity_id,
                "preset_mode": preset_mode,
            },
        }

    # 4. Target temperature → set_temperature
    #    Select the preferred DP based on HA temp unit to ensure the sent value matches HA's unit
    temp_value = None
    if use_f:
        # HA uses ℉: prefer temp_set_f passed directly; convert from temp_set to ℉ only if temp_set_f is absent
        if "temp_set_f" in tuya_dps:
            temp_f = tuya_dps["temp_set_f"]
            validate_tuya_param("temp_set_f", temp_f)
            temp_value = temp_f
        elif "temp_set" in tuya_dps:
            temp_c = tuya_dps["temp_set"]
            validate_tuya_param("temp_set", temp_c)
            temp_value = _celsius_to_fahrenheit(temp_c)
    else:
        # HA uses ℃: prefer temp_set passed directly; convert from temp_set_f to ℃ only if temp_set is absent
        if "temp_set" in tuya_dps:
            temp = tuya_dps["temp_set"]
            validate_tuya_param("temp_set", temp)
            temp_value = temp
        elif "temp_set_f" in tuya_dps:
            temp_f = tuya_dps["temp_set_f"]
            validate_tuya_param("temp_set_f", temp_f)
            temp_value = _fahrenheit_to_celsius(temp_f)

    if temp_value is not None:
        ha_params["mode_service"] = {
            "domain": "climate",
            "service": "set_temperature",
            "service_data": {
                "entity_id": ha_entity_id,
                "temperature": temp_value,
            },
        }
        if not ha_params["service"]:
            ha_params["service"] = "set_temperature"
            ha_params["service_data"]["temperature"] = temp_value
            ha_params.pop("mode_service")

    # 5. Fan speed → set_fan_mode (enum passed through directly, consistent with HA fan_mode)
    if "fan_speed_enum" in tuya_dps:
        fan_speed = tuya_dps["fan_speed_enum"]
        ha_params["fan_service"] = {
            "domain": "climate",
            "service": "set_fan_mode",
            "service_data": {
                "entity_id": ha_entity_id,
                "fan_mode": fan_speed,
            },
        }

    # 6. Swing mode → set_swing_mode (enum passed through directly, consistent with HA swing_mode)
    if "wind_shake" in tuya_dps:
        swing = tuya_dps["wind_shake"]
        ha_params["swing_service"] = {
            "domain": "climate",
            "service": "set_swing_mode",
            "service_data": {
                "entity_id": ha_entity_id,
                "swing_mode": swing,
            },
        }

    # Read-only DPs are not converted to HA service calls:
    # - temp_current / temp_current_f: current temperature, state sync only
    # - status: operating status, state sync only
    # - temp_unit_convert: temp unit switch, report only, no HA command generated

    # Fallback: when only switch=true with no other commands, default to turn on
    if not ha_params["service"]:
        ha_params["service"] = "turn_on"

    return ha_params


# ------------------------------
# Core function 2: HA Climate state → Tuya AC DPS command (full output)
# ------------------------------
def ha_to_tuya(ha_state: Dict[str, Any], ha_attributes: Dict[str, Any], context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Convert HA AC state/attributes to Tuya AC DPS commands.
    Output all DPs in full; use default values for missing attributes.
    Enum DPs are passed through directly without conversion.
    Temperature base unit is determined by the HA temp unit; both ℃ and ℉ groups are output simultaneously.
    """
    tuya_dps: Dict[str, Any] = {}
    use_f = _is_fahrenheit(context)

    # 1. Switch + mode (HA climate's state is hvac_mode; enum passed through directly)
    ha_hvac_mode = ha_state.get("state", "off")
    if ha_hvac_mode == "off":
        tuya_dps["switch"] = False
        tuya_dps["mode"] = TUYA_DP_DEFAULTS["mode"]
    else:
        tuya_dps["switch"] = True
        tuya_dps["mode"] = ha_hvac_mode

    # 2. Target temperature (base determined by HA temp unit; output both ℃ and ℉)
    temp = ha_attributes.get("temperature")
    if use_f:
        # HA value is in ℉
        temp_f = int(temp) if temp is not None else TUYA_DP_DEFAULTS["temp_set_f"]
        tuya_dps["temp_set_f"] = temp_f
        tuya_dps["temp_set"] = _fahrenheit_to_celsius(temp_f)
    else:
        # HA value is in ℃
        temp_c = int(temp) if temp is not None else TUYA_DP_DEFAULTS["temp_set"]
        tuya_dps["temp_set"] = temp_c
        tuya_dps["temp_set_f"] = _celsius_to_fahrenheit(temp_c)

    # 3. Current temperature (ro, base determined by HA temp unit; output both ℃ and ℉)
    current_temp = ha_attributes.get("current_temperature")
    if use_f:
        current_f = int(current_temp) if current_temp is not None else TUYA_DP_DEFAULTS["temp_current_f"]
        tuya_dps["temp_current_f"] = current_f
        tuya_dps["temp_current"] = _fahrenheit_to_celsius(current_f)
    else:
        current_c = int(current_temp) if current_temp is not None else TUYA_DP_DEFAULTS["temp_current"]
        tuya_dps["temp_current"] = current_c
        tuya_dps["temp_current_f"] = _celsius_to_fahrenheit(current_c)

    # 4. Preset mode (enum passed through directly, consistent with HA preset_mode)
    preset_mode = ha_attributes.get("preset_mode")
    tuya_dps["run_mode"] = preset_mode if preset_mode is not None else TUYA_DP_DEFAULTS["run_mode"]

    # 5. Fan speed (enum passed through directly; use default if missing)
    fan_mode = ha_attributes.get("fan_mode")
    tuya_dps["fan_speed_enum"] = fan_mode if fan_mode is not None else TUYA_DP_DEFAULTS["fan_speed_enum"]

    # 6. Swing mode (enum passed through directly, consistent with HA swing_mode; use default if missing)
    swing_mode = ha_attributes.get("swing_mode")
    tuya_dps["wind_shake"] = swing_mode if swing_mode is not None else TUYA_DP_DEFAULTS["wind_shake"]

    # 7. Operating status (ro)
    #    Prefer hvac_action when available (off/heating/cooling/drying/fan/idle/preheating/defrosting).
    #    Many climate integrations (IR / cloud-controlled ACs) do NOT report hvac_action,
    #    so fall back to inferring status from hvac_mode.
    hvac_action = ha_attributes.get("hvac_action")
    if hvac_action is not None:
        tuya_dps["status"] = hvac_action
    elif ha_hvac_mode == "off":
        tuya_dps["status"] = "off"
    else:
        _MODE_TO_STATUS = {
            "cool": "cooling",
            "heat": "heating",
            "dry": "drying",
            "fan_only": "fan",
            "auto": "idle",
            "heat_cool": "idle",
        }
        tuya_dps["status"] = _MODE_TO_STATUS.get(ha_hvac_mode, "idle")

    # 8. Temperature unit switch (report only, set based on HA temp unit)
    tuya_dps["temp_unit_convert"] = "f" if use_f else "c"

    return tuya_dps


# ------------------------------
# Usage examples
# ------------------------------
if __name__ == "__main__":
    # Example 1: Tuya AC turn on + cool at 26° → HA (enum passed through directly)
    tuya_dps_cool = {
        "switch": True,
        "temp_set": 26,
        "mode": "cool",       # consistent with HA hvac_mode
        "run_mode": "sleep",
        "fan_speed_enum": "auto",
    }
    ha_params = tuya_to_ha(tuya_dps_cool, "climate.tuya_ac_123456")
    print("=== Tuya AC turn on (cool 26°, auto fan) → HA ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 2: Tuya AC turn off → HA
    tuya_dps_off = {"switch": False}
    ha_params = tuya_to_ha(tuya_dps_off, "climate.tuya_ac_123456")
    print("\n=== Tuya AC turn off → HA ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 3: Tuya AC set temp in Fahrenheit → HA
    tuya_dps_f = {"switch": True, "temp_set_f": 79}
    ha_params = tuya_to_ha(tuya_dps_f, "climate.tuya_ac_123456")
    print("\n=== Tuya AC Fahrenheit 79°F → HA ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 4: HA AC heating state → Tuya (full output, including ℃ and ℉)
    ha_state_heat = {"state": "heat"}
    ha_attrs = {
        "temperature": 28,
        "current_temperature": 22,
        "preset_mode": "comfort",
        "fan_mode": "high",
        "hvac_action": "heating",
    }
    tuya_dps = ha_to_tuya(ha_state_heat, ha_attrs)
    print("\n=== HA AC heating → Tuya (full, with ℃ and ℉) ===")
    print(json.dumps(tuya_dps, indent=2, ensure_ascii=False))

    # Example 5: HA AC turn off → Tuya (full output, with defaults)
    ha_state_off = {"state": "off"}
    tuya_dps = ha_to_tuya(ha_state_off, {})
    print("\n=== HA AC turn off → Tuya (full, with defaults) ===")
    print(json.dumps(tuya_dps, indent=2, ensure_ascii=False))
