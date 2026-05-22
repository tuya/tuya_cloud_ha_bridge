import json
from typing import Any, Dict

# ------------------------------
# Core config: Tuya Cover ↔ HA Cover mapping
# PID: qbhdvznlsdpfdwa0
# ------------------------------
# Tuya cover category (cl) full DP configuration
TUYA_COVER_CORE_DPS = {
    "control": {"type": "Enum"},             # DP1   Control command (rw) range:["open_cover","stop_cover","close_cover"]
    "percent_control": {"type": "Integer"},  # DP2   Open percentage control (rw) min:0 max:100
    "work_state": {"type": "Enum"},          # DP7   Work state (ro) range:["opening","closing","closed","open"]
}

# Full DP default values
TUYA_DP_DEFAULTS: Dict[str, Any] = {
    "control": "stop_cover",
    "percent_control": 0,
    "work_state": "closed",
}

# Tuya cover control enum → HA Cover service mapping
_TUYA_CONTROL_TO_HA_SERVICE = {
    "open_cover": "open_cover",
    "close_cover": "close_cover",
    "stop_cover": "stop_cover",
}
_VALID_CONTROL_VALUES = frozenset(_TUYA_CONTROL_TO_HA_SERVICE)


# Property metadata: report defaultProperties on bind
TUYA_PROPERTY_METADATA: Dict[str, Dict[str, Any]] = {
    "control": {
        "bind_value": "",
        "dp_properties": {
            "range": ["open_cover", "stop_cover", "close_cover"],
            "type": "enum",
        },
    },
}


# ------------------------------
# Utility functions
# ------------------------------
def validate_tuya_param(param_name: str, value: Any) -> None:
    """Validate Tuya cover parameter types."""
    config = TUYA_COVER_CORE_DPS.get(param_name)
    if not config:
        return

    if config["type"] == "Integer":
        if not isinstance(value, int):
            raise ValueError(f"Parameter '{param_name}' value {value} has wrong type, must be Integer")
        if param_name == "percent_control":
            if not (0 <= value <= 100):
                raise ValueError(f"Parameter '{param_name}' value {value} out of range [0, 100]")
# ------------------------------
# Core function 1: Tuya cover command → HA Cover command
# ------------------------------
def tuya_to_ha(tuya_dps: Dict[str, Any], ha_entity_id: str, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Parse Tuya cover DPS commands and convert to HA Cover service call parameters.
    percent_control takes priority (precise position control), followed by control (open/close/stop).
    Read-only DP (work_state) does not generate a service call.
    """
    ha_params = {
        "domain": "cover",
        "service": "",
        "service_data": {"entity_id": ha_entity_id}
    }

    # 1. Percentage control takes priority (precise control)
    if "percent_control" in tuya_dps:
        percent = tuya_dps["percent_control"]
        validate_tuya_param("percent_control", percent)
        ha_params["service"] = "set_cover_position"
        # Tuya: 0=fully closed, 100=fully open; HA cover position: 0=fully closed, 100=fully open (consistent)
        ha_params["service_data"]["position"] = percent
        return ha_params

    # 2. Control command (open_cover/stop_cover/close_cover → HA service)
    if "control" in tuya_dps:
        control = tuya_dps["control"]
        service = _TUYA_CONTROL_TO_HA_SERVICE.get(control)
        if service:
            ha_params["service"] = service
            return ha_params

    # work_state (DP7) is read-only (ro), not converted to HA service call

    raise ValueError("Tuya cover command missing control parameter: must contain 'control' or 'percent_control'")


# ------------------------------
# Core function 2: HA Cover state → Tuya cover DPS command (full output)
# ------------------------------
def ha_to_tuya(ha_state: Dict[str, Any], ha_attributes: Dict[str, Any], context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Convert HA cover state/attributes to Tuya cover DPS commands.
    Outputs all DPs in full; missing attributes use default values.
    """
    tuya_dps: Dict[str, Any] = {}

    # 1. control is a command DP; state reporting prefers the most recently issued command
    ha_state_val = ha_state.get("state", "closed")
    last_control = context.get("last_cover_control") if isinstance(context, dict) else None
    tuya_dps["control"] = (
        last_control
        if isinstance(last_control, str) and last_control in _VALID_CONTROL_VALUES
        else TUYA_DP_DEFAULTS["control"]
    )


    # 2. Current position (read from HA, use default if missing, synced to percent_control)
    current_position = ha_attributes.get("current_position")
    position = int(current_position) if current_position is not None else TUYA_DP_DEFAULTS["percent_control"]
    tuya_dps["percent_control"] = position

    # 3. Work state (ro, directly corresponds to HA state)
    # HA cover state: open/opening/closed/closing matches Tuya work_state range
    tuya_dps["work_state"] = ha_state_val if ha_state_val in ("opening", "closing", "closed", "open") else TUYA_DP_DEFAULTS["work_state"]

    return tuya_dps


# ------------------------------
# Usage examples
# ------------------------------
if __name__ == "__main__":
    # Example 1: Tuya cover open → HA
    tuya_dps_open = {"control": "open_cover"}
    ha_params = tuya_to_ha(tuya_dps_open, "cover.tuya_curtain_123456")
    print("=== Tuya cover open → HA ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 2: Tuya cover percentage control → HA
    tuya_dps_percent = {"percent_control": 50}
    ha_params = tuya_to_ha(tuya_dps_percent, "cover.tuya_curtain_123456")
    print("\n=== Tuya cover 50% position → HA ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 3: Tuya cover stop → HA
    tuya_dps_stop = {"control": "stop_cover"}
    ha_params = tuya_to_ha(tuya_dps_stop, "cover.tuya_curtain_123456")
    print("\n=== Tuya cover stop → HA ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 4: HA cover fully open → Tuya (control should be stop_cover)
    ha_state_open = {"state": "open"}
    ha_attrs = {"current_position": 100}
    tuya_dps = ha_to_tuya(ha_state_open, ha_attrs)
    print("\n=== HA cover fully open → Tuya (control=stop_cover) ===")
    print(json.dumps(tuya_dps, indent=2, ensure_ascii=False))

    # Example 5: HA cover closing → Tuya (control stays stop_cover, work_state=closing)
    ha_state_closing = {"state": "closing"}
    ha_attrs = {"current_position": 40}
    tuya_dps = ha_to_tuya(ha_state_closing, ha_attrs)
    print("\n=== HA cover closing → Tuya (control=stop_cover, work_state=closing) ===")
    print(json.dumps(tuya_dps, indent=2, ensure_ascii=False))

    # Example 6: HA cover closed → Tuya (with default values)
    ha_state_closed = {"state": "closed"}
    tuya_dps = ha_to_tuya(ha_state_closed, {})
    print("\n=== HA cover closed → Tuya (full output, with defaults) ===")
    print(json.dumps(tuya_dps, indent=2, ensure_ascii=False))
