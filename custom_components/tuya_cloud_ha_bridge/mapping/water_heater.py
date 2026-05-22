import json
from typing import Any, Dict

# ------------------------------
# Core config: Tuya water heater to HA Water Heater mapping
# PID: nljqwchfgdmdtce1
# ------------------------------
# Full DP configuration for Tuya water heater category (rs)
# Enum DP (mode) is consistent with HA, no intermediate conversion needed
# temp_unit_convert is report-only, not controllable
TUYA_WATER_HEATER_CORE_DPS = {
    "switch": {"type": "Boolean"},              # DP1   main switch (rw)
    "mode": {"type": "Enum"},                   # DP2   operation mode (rw) range:["electric","performance","high_demand","heat_pump","eco","gas"]
    "temp_set": {"type": "Integer"},            # DP9   target temperature ℃ (rw) min:30 max:75
    "temp_current": {"type": "Integer"},        # DP10  current temperature ℃ (ro) min:0 max:100
    "work_state": {"type": "Enum"},             # DP13  work state (ro) range:["standby","heating","warm"]
    "temp_unit_convert": {"type": "Enum"},      # DP17  temperature unit switch (report-only) range:["c","f"]
    "temp_set_f": {"type": "Integer"},          # DP25  target temperature ℉ (rw) min:80 max:167
    "temp_current_f": {"type": "Integer"},      # DP26  current temperature ℉ (ro) min:-40 max:200
}

# Default values for all DPs
TUYA_DP_DEFAULTS: Dict[str, Any] = {
    "switch": True,
    "mode": "eco",
    "temp_set": 45,
    "temp_current": 30,
    "work_state": "standby",
    "temp_unit_convert": "c",
    "temp_set_f": 113,          # 45℃ ≈ 113℉
    "temp_current_f": 86,       # 30℃ ≈ 86℉
}

# Property metadata: reported as defaultProperties during bind
# Enum DPs dynamically fetch supported ranges from HA entity via range_attr, uploaded directly without value_map conversion
TUYA_PROPERTY_METADATA: Dict[str, Dict[str, Any]] = {
    "mode": {
        "range_attr": "operation_list",
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
    """Validate parameter types for Tuya water heater"""
    config = TUYA_WATER_HEATER_CORE_DPS.get(param_name)
    if not config:
        return

    if config["type"] == "Boolean":
        if not isinstance(value, bool):
            raise ValueError(f"Parameter {param_name} value {value} has invalid type, must be Boolean")
    elif config["type"] == "Integer":
        if not isinstance(value, int):
            raise ValueError(f"Parameter {param_name} value {value} has invalid type, must be Integer")


def _celsius_to_fahrenheit(celsius: int) -> int:
    """Convert Celsius to Fahrenheit"""
    return round(celsius * 9 / 5 + 32)


def _fahrenheit_to_celsius(fahrenheit: int) -> int:
    """Convert Fahrenheit to Celsius"""
    return round((fahrenheit - 32) * 5 / 9)


def _is_fahrenheit(context: Dict[str, Any] | None) -> bool:
    """Check if HA system temperature unit is Fahrenheit"""
    if context and context.get("temperature_unit") == "°F":
        return True
    return False


# ------------------------------
# Core function 1: Tuya water heater commands → HA Water Heater commands
# ------------------------------
def tuya_to_ha(tuya_dps: Dict[str, Any], ha_entity_id: str, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Parse Tuya water heater DPS commands and convert to HA Water Heater service call parameters.
    Enum DP (mode) is consistent with HA, passed through directly without conversion.
    Temperature is determined by HA temperature unit:
    - When HA uses ℃: prefer temp_set (pass ℃ directly), convert temp_set_f to ℃ if only ℉ is available
    - When HA uses ℉: prefer temp_set_f (pass ℉ directly), convert temp_set to ℉ if only ℃ is available
    Read-only DPs (temp_current/temp_current_f/work_state) do not generate service calls.
    temp_unit_convert is report-only and does not generate HA commands.
    """
    ha_params = {
        "domain": "water_heater",
        "service": "",
        "service_data": {"entity_id": ha_entity_id}
    }
    use_f = _is_fahrenheit(context)

    # 1. Switch command
    switch_val = tuya_dps.get("switch", True)
    validate_tuya_param("switch", switch_val)

    if switch_val:
        # Turn on: set temperature (choose preferred DP based on HA temperature unit)
        temp_value = None
        if use_f:
            if "temp_set_f" in tuya_dps:
                temp_f = tuya_dps["temp_set_f"]
                validate_tuya_param("temp_set_f", temp_f)
                temp_value = temp_f
            elif "temp_set" in tuya_dps:
                temp_c = tuya_dps["temp_set"]
                validate_tuya_param("temp_set", temp_c)
                temp_value = _celsius_to_fahrenheit(temp_c)
        else:
            if "temp_set" in tuya_dps:
                temp = tuya_dps["temp_set"]
                validate_tuya_param("temp_set", temp)
                temp_value = temp
            elif "temp_set_f" in tuya_dps:
                temp_f = tuya_dps["temp_set_f"]
                validate_tuya_param("temp_set_f", temp_f)
                temp_value = _fahrenheit_to_celsius(temp_f)

        if temp_value is not None:
            ha_params["service"] = "set_temperature"
            ha_params["service_data"]["temperature"] = temp_value
        else:
            ha_params["service"] = "turn_on"
    else:
        ha_params["service"] = "turn_off"

    # 2. Operation mode → set_operation_mode (enum passed through directly, consistent with HA operation_mode)
    if "mode" in tuya_dps:
        mode = tuya_dps["mode"]
        ha_params["mode_service"] = {
            "domain": "water_heater",
            "service": "set_operation_mode",
            "service_data": {
                "entity_id": ha_entity_id,
                "operation_mode": mode,
            },
        }

    # Read-only DPs are not converted to HA service calls:
    # - temp_current / temp_current_f: current temperature, state sync only
    # - work_state: work state, state sync only
    # - temp_unit_convert: temperature unit switch, report-only, does not generate HA commands

    return ha_params


# ------------------------------
# Core function 2: HA Water Heater state → Tuya water heater DPS commands (full output)
# ------------------------------
def ha_to_tuya(ha_state: Dict[str, Any], ha_attributes: Dict[str, Any], context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Convert HA water heater state/attributes to Tuya water heater DPS commands.
    Output all DPs in full, using default values for missing attributes.
    Enum DPs are passed through directly without conversion.
    Temperature base unit is determined by HA temperature unit, outputting both ℃ and ℉ groups.
    """
    tuya_dps: Dict[str, Any] = {}
    use_f = _is_fahrenheit(context)

    # 1. Switch state (HA water_heater state can be: off, eco, electric, gas, heat_pump, etc.)
    ha_state_val = ha_state.get("state", "off")
    tuya_dps["switch"] = ha_state_val != "off"

    # 2. Operation mode (HA state is the operation mode directly, consistent with Tuya mode enum values, passed through directly)
    tuya_dps["mode"] = ha_state_val if ha_state_val != "off" else TUYA_DP_DEFAULTS["mode"]

    # 3. Target temperature (base unit determined by HA temp unit, output both ℃ and ℉)
    temp = ha_attributes.get("temperature")
    if use_f:
        temp_f = int(temp) if temp is not None else TUYA_DP_DEFAULTS["temp_set_f"]
        tuya_dps["temp_set_f"] = temp_f
        tuya_dps["temp_set"] = _fahrenheit_to_celsius(temp_f)
    else:
        temp_c = int(temp) if temp is not None else TUYA_DP_DEFAULTS["temp_set"]
        tuya_dps["temp_set"] = temp_c
        tuya_dps["temp_set_f"] = _celsius_to_fahrenheit(temp_c)

    # 4. Current temperature (read-only, base unit determined by HA temp unit, output both ℃ and ℉)
    current_temp = ha_attributes.get("current_temperature")
    if use_f:
        current_f = int(current_temp) if current_temp is not None else TUYA_DP_DEFAULTS["temp_current_f"]
        tuya_dps["temp_current_f"] = current_f
        tuya_dps["temp_current"] = _fahrenheit_to_celsius(current_f)
    else:
        current_c = int(current_temp) if current_temp is not None else TUYA_DP_DEFAULTS["temp_current"]
        tuya_dps["temp_current"] = current_c
        tuya_dps["temp_current_f"] = _celsius_to_fahrenheit(current_c)

    # 5. Work state (ro, infer from switch state and temperatures)
    #    HA water_heater does not expose a dedicated work_state attribute,
    #    so we infer it: off → standby, target > current → heating, else warm.
    if not tuya_dps["switch"]:
        tuya_dps["work_state"] = "standby"
    else:
        temp_set_c = tuya_dps.get("temp_set", TUYA_DP_DEFAULTS["temp_set"])
        temp_cur_c = tuya_dps.get("temp_current", TUYA_DP_DEFAULTS["temp_current"])
        tuya_dps["work_state"] = "heating" if temp_set_c > temp_cur_c else "warm"

    # 6. Temperature unit switch (report-only, set based on HA temperature unit)
    tuya_dps["temp_unit_convert"] = "f" if use_f else "c"

    return tuya_dps


# ------------------------------
# Usage examples
# ------------------------------
if __name__ == "__main__":
    # Example 1: Tuya water heater turn on + set 45°C → HA
    tuya_dps_on = {
        "switch": True,
        "temp_set": 45,
        "mode": "eco",
    }
    ha_params = tuya_to_ha(tuya_dps_on, "water_heater.tuya_water_heater_123456")
    print("=== Tuya water heater turn on (45°C, eco mode) → HA ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 2: Tuya water heater turn off → HA
    tuya_dps_off = {"switch": False}
    ha_params = tuya_to_ha(tuya_dps_off, "water_heater.tuya_water_heater_123456")
    print("\n=== Tuya water heater turn off → HA ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 3: Tuya water heater set temperature in Fahrenheit → HA
    tuya_dps_f = {"switch": True, "temp_set_f": 120}
    ha_params = tuya_to_ha(tuya_dps_f, "water_heater.tuya_water_heater_123456")
    print("\n=== Tuya water heater 120°F → HA ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 4: HA water heater state → Tuya (full output, including ℃ and ℉)
    ha_state_on = {"state": "electric"}
    ha_attrs = {"temperature": 50, "current_temperature": 42, "operation_mode": "eco"}
    tuya_dps = ha_to_tuya(ha_state_on, ha_attrs)
    print("\n=== HA water heater → Tuya (full output, including ℃ and ℉) ===")
    print(json.dumps(tuya_dps, indent=2, ensure_ascii=False))

    # Example 5: HA water heater with missing attributes → Tuya (with default values)
    ha_state_off = {"state": "off"}
    tuya_dps = ha_to_tuya(ha_state_off, {})
    print("\n=== HA water heater turn off → Tuya (full output, with default values) ===")
    print(json.dumps(tuya_dps, indent=2, ensure_ascii=False))
