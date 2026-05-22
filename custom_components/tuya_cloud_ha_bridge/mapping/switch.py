import json
from typing import Dict, Any, Optional, List, Union

# ------------------------------
# Core config: Mapping between Tuya switch and HA Switch
# ------------------------------
# Tuya switch core commands (compatible with Tuya switch device standard DPS)
TUYA_SWITCH_CORE_DPS = {
    "switch": {"type": "Boolean", "min": None, "max": None}  # Core switch (countdown config removed)
}


# ------------------------------
# Utility function: Numeric range validation
# ------------------------------
def validate_value(value: Any, min_val: int, max_val: int, param_name: str) -> None:
    """Validate whether a value is within the specified range; raise an exception if out of range (None means no validation)"""
    if min_val is None or max_val is None:
        return
    if not (min_val <= value <= max_val):
        raise ValueError(f"Parameter {param_name} value {value} is out of range [{min_val}, {max_val}]")


# ------------------------------
# Core function 1: Tuya switch command → HA Switch command
# ------------------------------
def tuya_to_ha(tuya_dps: Dict[str, Any], ha_entity_id: str, context: Dict[str, Any] | None = None) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Parse Tuya switch DPS commands and convert to HA Switch service call parameters.
    :param tuya_dps: Tuya DPS dictionary (e.g. {"switch":true...} or {"switch_1":true, "switch_2":false...})
    :param ha_entity_id: HA switch entity ID (e.g. switch.tuya_wall_switch_123456)
    :return: HA service call parameters (single-key returns dict, multi-key returns list)
    """
    # Step 1: Filter single-key/multi-key switch fields
    root_switch_exists = "switch_1" in tuya_dps
    sub_switch_keys = [k for k in tuya_dps.keys() if k.startswith("switch_") and k != "switch_1"]
    #sub_switch_keys = []

    # Validation: raise exception if neither single-key nor multi-key is present
    if not root_switch_exists and not sub_switch_keys:
        raise ValueError("Tuya switch command missing switch parameter: must contain switch (single-key) or switch_* (multi-key)")

    # Step 2: Handle single-key switch (countdown-related logic removed)
    ha_params_list = []
    if root_switch_exists:
        ha_params = {
            "domain": "switch",
            "service": "",
            "service_data": {"entity_id": ha_entity_id}
        }
        # Validate root-level switch type
        switch_val = tuya_dps["switch_1"]
        if not isinstance(switch_val, bool):
            raise ValueError(f"Parameter switch value {switch_val} has wrong type, must be Boolean (true/false)")
        # Map switch state
        ha_params["service"] = "turn_on" if switch_val else "turn_off"
        ha_params_list.append(ha_params)

    # Step 3: Handle multi-key switches (countdown-related logic removed)
    for key in sub_switch_keys:
        sub_switch_val = tuya_dps[key]
        if not isinstance(sub_switch_val, bool):
            raise ValueError(f"Parameter {key} value {sub_switch_val} has wrong type, must be Boolean")
        # Generate sub-switch EntityID (e.g. switch.tuya_wall_switch_123456_1)
        sub_num = key.split("_")[1]
        sub_entity_id = f"{ha_entity_id}_{sub_num}"
        # Build sub-switch HA parameters
        sub_ha_params = {
            "domain": "switch",
            "service": "turn_on" if sub_switch_val else "turn_off",
            "service_data": {"entity_id": sub_entity_id}
        }
        ha_params_list.append(sub_ha_params)

    # Single-key returns a single dict, multi-key returns a list
    return ha_params_list[0] if len(ha_params_list) == 1 else ha_params_list


# ------------------------------
# Core function 2: HA Switch command → Tuya switch DPS command (for reverse sync)
# ------------------------------
def ha_to_tuya(ha_state: Dict[str, Any], ha_attributes: Dict[str, Any], context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Convert HA switch state/attributes to Tuya DPS commands.
    :param ha_state: HA state (e.g. {"state":"on"})
    :param ha_attributes: HA attributes
    :return: Tuya DPS dictionary
    """
    tuya_dps = {}

    # 1. Core switch state mapping
    ha_state_val = ha_state.get("state")
    if ha_state_val not in ["on", "off"]:
        raise ValueError(f"HA switch state {ha_state_val} is invalid, only on/off are supported")

    # tuya_dps["switch"] = True if ha_state_val == "on" else False
    tuya_dps["switch_1"] = True if ha_state_val == "on" else False
    # Extension: Multi-key switch reverse mapping (compatible with HA multi-key switch entities)
    if "sub_switches" in ha_attributes:
        for sub_switch in ha_attributes["sub_switches"]:
            sub_entity_id = sub_switch["entity_id"]
            sub_state = sub_switch["state"]
            # Extract sub-switch number (e.g. switch.tuya_wall_switch_123456_1 → 1)
            sub_num = sub_entity_id.split("_")[-1]
            tuya_dps[f"switch_{sub_num}"] = True if sub_state == "on" else False

    return tuya_dps


# ------------------------------
# Usage examples
# ------------------------------
if __name__ == "__main__":
    # Example 1: Tuya single-key switch command → HA command (countdown parameter removed)
    tuya_dps_single = {
        "switch": True
    }
    ha_params_single = tuya_to_ha(tuya_dps_single, "switch.tuya_wall_switch_123456")
    print("=== Tuya single-key switch → HA conversion result ===")
    print(json.dumps(ha_params_single, indent=2, ensure_ascii=False))

    # Example 2: Tuya dual-key switch command → HA command (countdown parameter removed)
    tuya_dps_double = {
        "switch_1": True,
        "switch_2": False
    }
    ha_params_double = tuya_to_ha(tuya_dps_double, "switch.tuya_wall_switch_123456")
    print("\n=== Tuya dual-key switch → HA conversion result ===")
    print(json.dumps(ha_params_double, indent=2, ensure_ascii=False))

    # Example 3: HA switch command → Tuya command (countdown parameter removed)
    ha_state_example = {"state": "on"}
    ha_attributes_example = {}  # Cleared countdown-related attributes
    tuya_dps = ha_to_tuya(ha_state_example, ha_attributes_example)
    print("\n=== HA → Tuya switch conversion result ===")
    print(json.dumps(tuya_dps, indent=2, ensure_ascii=False))


# ------------------------------
# Async HA Switch service call within HA integration
# ------------------------------
async def call_ha_switch_service(hass, ha_params: Union[Dict[str, Any], List[Dict[str, Any]]]):
    """
    Execute HA Switch service call (must be run in HA async context).
    :param hass: HA hass object (integration context)
    :param ha_params: Parameters returned by tuya_to_ha (single-key/multi-key)
    """
    # Compatible with both single-key and multi-key scenarios
    params_list = [ha_params] if isinstance(ha_params, dict) else ha_params
    for params in params_list:
        await hass.services.async_call(
            domain=params["domain"],
            service=params["service"],
            service_data=params["service_data"],
            blocking=True
        )


# ------------------------------
# MQTT command parsing wrapper (compatible with TuyaLink MQTT full format)
# ------------------------------
def parse_tuya_mqtt_payload(mqtt_payload: str) -> Dict[str, Any]:
    """
    Parse the full payload received from TuyaLink MQTT and extract the DPS dictionary.
    :param mqtt_payload: JSON string received from MQTT (contains msgId/time/dps)
    :return: Tuya DPS dictionary
    """
    payload = json.loads(mqtt_payload)
    dps = payload.get("dps", {})
    if not dps:
        raise ValueError("TuyaLink MQTT payload is missing the dps field")
    return dps