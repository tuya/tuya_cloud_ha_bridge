import json
from typing import Any, Dict

# ------------------------------
# Core config: Tuya air purifier to HA Fan mapping
# Category code: wf_kj
# PID: wesu2cmngvsc6baa
# HA Domain: fan (HA has no air_purifier domain, so air purifiers use the fan domain)
# ------------------------------
# Tuya air purifier full DP configuration
# Enum DP (mode) is consistent with HA, no intermediate conversion needed
TUYA_PURIFIER_CORE_DPS = {
    "switch": {"type": "Boolean"},              # DP1    Power switch (rw)
    "mode": {"type": "Enum"},                   # DP3    Working mode (rw) range:["manual","auto","silent","sleep","turbo"]
    "fan_speed": {"type": "Integer"},           # DP103  Fan speed (rw) min:1 max:100
}

# Full DP default values
TUYA_DP_DEFAULTS: Dict[str, Any] = {
    "switch": True,
    "mode": "auto",
    "fan_speed": 50,
}

# Property metadata: report defaultProperties when binding
# Enum DPs dynamically get supported range from HA entity via range_attr, uploaded directly without value_map conversion
TUYA_PROPERTY_METADATA: Dict[str, Dict[str, Any]] = {
    "mode": {
        "range_attr": "preset_modes",
    },
}


# ------------------------------
# Utility function: parameter validation
# ------------------------------
def validate_tuya_param(param_name: str, value: Any) -> None:
    """Validate Tuya air purifier parameter types."""
    config = TUYA_PURIFIER_CORE_DPS.get(param_name)
    if not config:
        return

    if config["type"] == "Boolean":
        if not isinstance(value, bool):
            raise ValueError(f"Parameter {param_name} value {value} has wrong type, must be Boolean")
    elif config["type"] == "Integer":
        if not isinstance(value, int):
            raise ValueError(f"Parameter {param_name} value {value} has wrong type, must be Integer")


# ------------------------------
# Core function 1: Tuya air purifier commands → HA Fan commands
# ------------------------------
def tuya_to_ha(tuya_dps: Dict[str, Any], ha_entity_id: str, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Parse Tuya air purifier DPS commands and convert to HA Fan service call parameters.
    Handles three DPs: switch, mode, and fan_speed.
    Enum DP (mode) is consistent with HA, passed through directly without conversion.
    """
    ha_params = {
        "domain": "fan",
        "service": "",
        "service_data": {"entity_id": ha_entity_id}
    }

    # 1. Power switch command
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
        ha_params["service"] = "set_percentage"
        ha_params["service_data"]["percentage"] = max(0, min(100, speed))

    # 3. Working mode → set_preset_mode (enum passed through directly, consistent with HA preset_mode)
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

    # If no power switch command and no fan speed, default to turning on
    if not ha_params["service"]:
        ha_params["service"] = "turn_on"

    return ha_params


# ------------------------------
# Core function 2: HA Fan state → Tuya air purifier DPS commands (full output)
# ------------------------------
def ha_to_tuya(ha_state: Dict[str, Any], ha_attributes: Dict[str, Any], context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Convert HA fan state/attributes to Tuya air purifier DPS commands.
    Outputs all DPs in full; missing attributes use default values.
    Enum DPs are passed through directly without conversion.
    """
    tuya_dps: Dict[str, Any] = {}

    # 1. Power switch state
    is_on = ha_state.get("state") == "on"
    tuya_dps["switch"] = is_on

    # 2. Working mode (enum passed through directly, consistent with HA preset_mode)
    preset_mode = ha_attributes.get("preset_mode")
    tuya_dps["mode"] = preset_mode if preset_mode is not None else TUYA_DP_DEFAULTS["mode"]

    # 3. Fan speed (HA percentage 0-100 → Tuya fan_speed 1-100)
    percentage = ha_attributes.get("percentage")
    if percentage is not None:
        tuya_dps["fan_speed"] = max(1, min(100, int(percentage)))
    else:
        tuya_dps["fan_speed"] = TUYA_DP_DEFAULTS["fan_speed"]

    return tuya_dps


# ------------------------------
# Usage examples
# ------------------------------
if __name__ == "__main__":
    # Example 1: Tuya purifier power on + auto mode → HA
    tuya_dps_on = {
        "switch": True,
        "mode": "auto",
        "fan_speed": 60,
    }
    ha_params = tuya_to_ha(tuya_dps_on, "fan.tuya_air_purifier_123456")
    print("=== Tuya purifier power on (auto mode, 60% fan speed) → HA ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 2: Tuya purifier power off → HA
    tuya_dps_off = {"switch": False}
    ha_params = tuya_to_ha(tuya_dps_off, "fan.tuya_air_purifier_123456")
    print("\n=== Tuya purifier power off → HA ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 3: Tuya purifier sleep mode → HA
    tuya_dps_sleep = {"mode": "sleep"}
    ha_params = tuya_to_ha(tuya_dps_sleep, "fan.tuya_air_purifier_123456")
    print("\n=== Tuya purifier sleep mode → HA ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 4: HA purifier state → Tuya (full output)
    ha_state_on = {"state": "on"}
    ha_attrs = {"preset_mode": "sleep", "percentage": 30}
    tuya_dps = ha_to_tuya(ha_state_on, ha_attrs)
    print("\n=== HA purifier → Tuya (full output) ===")
    print(json.dumps(tuya_dps, indent=2, ensure_ascii=False))

    # Example 5: HA purifier with missing attributes → Tuya (with default values)
    ha_state_off = {"state": "off"}
    tuya_dps = ha_to_tuya(ha_state_off, {})
    print("\n=== HA purifier power off → Tuya (full output, with default values) ===")
    print(json.dumps(tuya_dps, indent=2, ensure_ascii=False))
