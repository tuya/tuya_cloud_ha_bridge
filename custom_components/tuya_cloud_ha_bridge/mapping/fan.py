import json
from typing import Any, Dict

# ------------------------------
# Core config: Tuya Fan to HA Fan mapping
# PID: 3crz4k4rdmbmnmmc
# ------------------------------
# Tuya fan category (fs) full DP configuration
# Enum DPs (mode/fan_direction) are consistent with HA, no intermediate conversion needed
TUYA_FAN_CORE_DPS = {
    "switch": {"type": "Boolean"},              # DP1  Main switch (rw)
    "mode": {"type": "Enum"},                   # DP2  Preset mode (rw) range:["nature","sleep","normal","baby"]
    "fan_speed": {"type": "Integer"},           # DP3  Fan speed (rw) min:1 max:100 step:1
    "switch_horizontal": {"type": "Boolean"},   # DP5  Oscillation switch (rw) maps to HA oscillate
    "fan_direction": {"type": "Enum"},          # DP8  Fan direction (rw) forward/reverse consistent with HA direction
}

# Default values for all DPs
TUYA_DP_DEFAULTS: Dict[str, Any] = {
    "switch": True,
    "mode": "nature",
    "fan_speed": 50,
    "switch_horizontal": False,
    "fan_direction": "forward",
}

# Property metadata: reports defaultProperties on bind
TUYA_PROPERTY_METADATA: Dict[str, Dict[str, Any]] = {
    "mode": {
        "range_attr": "preset_modes",
    },
}


# ------------------------------
# Utility function: parameter validation
# ------------------------------
def validate_tuya_param(param_name: str, value: Any) -> None:
    """Validate the type of a Tuya fan parameter."""
    config = TUYA_FAN_CORE_DPS.get(param_name)
    if not config:
        return

    if config["type"] == "Boolean":
        if not isinstance(value, bool):
            raise ValueError(f"Parameter '{param_name}' value '{value}' has wrong type, must be Boolean")
    elif config["type"] == "Integer":
        if not isinstance(value, int):
            raise ValueError(f"Parameter '{param_name}' value '{value}' has wrong type, must be Integer")


# ------------------------------
# Core function 1: Tuya Fan command → HA Fan command
# ------------------------------
def tuya_to_ha(tuya_dps: Dict[str, Any], ha_entity_id: str, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Parse Tuya fan DPS commands and convert to HA Fan service call parameters.
    Processes all rw DPs.
    """
    ha_params = {
        "domain": "fan",
        "service": "",
        "service_data": {"entity_id": ha_entity_id}
    }

    # 1. Switch command
    switch_val = tuya_dps.get("switch")
    if switch_val is not None:
        validate_tuya_param("switch", switch_val)
        if not switch_val:
            ha_params["service"] = "turn_off"
            return ha_params
        ha_params["service"] = "turn_on"

    # 2. Fan speed → set_percentage (fan_speed 1-100 maps directly to percentage 1-100)
    if "fan_speed" in tuya_dps:
        speed = tuya_dps["fan_speed"]
        validate_tuya_param("fan_speed", speed)
        # Tuya min:1, HA percentage 0-100
        ha_params["service"] = "set_percentage"
        ha_params["service_data"]["percentage"] = max(0, min(100, speed))

    # 3. Fan direction → set_direction (enum passed directly, consistent with HA direction)
    if "fan_direction" in tuya_dps:
        direction = tuya_dps["fan_direction"]
        if direction in ("forward", "reverse"):
            ha_params["direction_service"] = {
                "domain": "fan",
                "service": "set_direction",
                "service_data": {
                    "entity_id": ha_entity_id,
                    "direction": direction,
                },
            }

    # 4. Operating mode → set_preset_mode (enum passed directly, consistent with HA preset_mode)
    if "mode" in tuya_dps:
        mode = tuya_dps["mode"]
        ha_params["mode_service"] = {
            "domain": "fan",
            "service": "set_preset_mode",
            "service_data": {
                "entity_id": ha_entity_id,
                "preset_mode": mode,
            },
        }

    # 5. Oscillation switch → oscillate
    if "switch_horizontal" in tuya_dps:
        oscillating = tuya_dps["switch_horizontal"]
        validate_tuya_param("switch_horizontal", oscillating)
        ha_params["oscillate_service"] = {
            "domain": "fan",
            "service": "oscillate",
            "service_data": {
                "entity_id": ha_entity_id,
                "oscillating": oscillating,
            },
        }

    # If no switch or fan speed command, default to turning on
    if not ha_params["service"]:
        ha_params["service"] = "turn_on"

    return ha_params


# ------------------------------
# Core function 2: HA Fan state → Tuya Fan DPS command (full output)
# ------------------------------
def ha_to_tuya(ha_state: Dict[str, Any], ha_attributes: Dict[str, Any], context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Convert HA fan state/attributes to Tuya fan DPS commands.
    Outputs all DPs; missing attributes use default values.
    """
    tuya_dps: Dict[str, Any] = {}

    # 1. Switch state
    is_on = ha_state.get("state") == "on"
    tuya_dps["switch"] = is_on

    # 2. Fan speed (HA percentage 0-100 → Tuya fan_speed 1-100)
    percentage = ha_attributes.get("percentage")
    if percentage is not None:
        tuya_dps["fan_speed"] = max(1, min(100, int(percentage)))
    else:
        tuya_dps["fan_speed"] = TUYA_DP_DEFAULTS["fan_speed"]

    # 3. Fan direction (enum passed directly, consistent with HA direction)
    direction = ha_attributes.get("direction")
    tuya_dps["fan_direction"] = direction if direction in ("forward", "reverse") else TUYA_DP_DEFAULTS["fan_direction"]

    # 4. Operating mode (enum passed directly, consistent with HA preset_mode)
    preset_mode = ha_attributes.get("preset_mode")
    tuya_dps["mode"] = preset_mode if preset_mode is not None else TUYA_DP_DEFAULTS["mode"]

    # 5. Oscillation switch (HA oscillating → Tuya switch_horizontal)
    oscillating = ha_attributes.get("oscillating")
    tuya_dps["switch_horizontal"] = oscillating if oscillating is not None else TUYA_DP_DEFAULTS["switch_horizontal"]

    return tuya_dps


# ------------------------------
# Usage examples
# ------------------------------
if __name__ == "__main__":
    # Example 1: Tuya fan turn on + 50% speed → HA
    tuya_dps_on = {
        "switch": True,
        "fan_speed": 50,
        "mode": "nature",
    }
    ha_params = tuya_to_ha(tuya_dps_on, "fan.tuya_fan_123456")
    print("=== Tuya fan turn on (50% speed, nature mode) → HA ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 2: Tuya fan turn off → HA
    tuya_dps_off = {"switch": False}
    ha_params = tuya_to_ha(tuya_dps_off, "fan.tuya_fan_123456")
    print("\n=== Tuya fan turn off → HA ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 3: Tuya fan reverse direction → HA
    tuya_dps_dir = {"fan_direction": "reverse"}
    ha_params = tuya_to_ha(tuya_dps_dir, "fan.tuya_fan_123456")
    print("\n=== Tuya fan reverse direction → HA ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 4: HA fan state → Tuya (full output)
    ha_state_on = {"state": "on"}
    ha_attrs = {"percentage": 75, "direction": "forward", "preset_mode": "sleep"}
    tuya_dps = ha_to_tuya(ha_state_on, ha_attrs)
    print("\n=== HA fan → Tuya (full) ===")
    print(json.dumps(tuya_dps, indent=2, ensure_ascii=False))

    # Example 5: HA fan missing attributes → Tuya (with defaults)
    ha_state_off = {"state": "off"}
    tuya_dps = ha_to_tuya(ha_state_off, {})
    print("\n=== HA fan off → Tuya (full, with defaults) ===")
    print(json.dumps(tuya_dps, indent=2, ensure_ascii=False))
