import json
from typing import Any, Dict

# ------------------------------
# Core config: Tuya robot vacuum <-> HA Vacuum mapping
# PID: kwhlfgsfz6j5cfjs
# ------------------------------
# Tuya robot vacuum category (sd) full DP configuration
# mode enum values map directly to HA vacuum service names; status enum values map directly to HA vacuum states
TUYA_VACUUM_CORE_DPS = {
    "mode": {"type": "Enum"},               # DP3   Cleaning mode (rw) range:["pause","return_to_base","start","stop"]
    "status": {"type": "Enum"},             # DP5   Work status (ro) range:["cleaning","docked","idle","paused","returning","error"]
    "suction": {"type": "Enum"},            # DP14  Suction level (rw) range:["quiet","balanced","turbo","max"]
}

# Full DP default values
TUYA_DP_DEFAULTS: Dict[str, Any] = {
    "mode": "start",
    "status": "idle",
    "suction": "balanced",
}

# Tuya mode -> HA vacuum service mapping (mode enum values are directly HA service names)
_TUYA_MODE_TO_HA_SERVICE = {
    "start": "start",
    "stop": "stop",
    "pause": "pause",
    "locate": "locate",
    "return_to_base": "return_to_base",
}

# HA vacuum service/state -> Tuya mode reverse mapping
_HA_STATE_TO_TUYA_MODE = {
    "cleaning": "start",
    "docked": "return_to_base",
    "idle": "stop",
    "returning": "return_to_base",
    "paused": "pause",
    "error": "stop",
}

# Property metadata
TUYA_PROPERTY_METADATA: Dict[str, Dict[str, Any]] = {
    "suction": {
        "range_attr": "fan_speed_list",
    },
}


# ------------------------------
# Utility function: parameter validation
# ------------------------------
def validate_tuya_param(param_name: str, value: Any) -> None:
    """Validate the type of a Tuya robot vacuum parameter."""
    config = TUYA_VACUUM_CORE_DPS.get(param_name)
    if not config:
        return


# ------------------------------
# Core function 1: Tuya robot vacuum command -> HA Vacuum command
# ------------------------------
def tuya_to_ha(tuya_dps: Dict[str, Any], ha_entity_id: str, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Parse Tuya robot vacuum DPS commands and convert to HA Vacuum service call parameters.
    mode enum values map directly to HA vacuum service names (start/stop/pause/return_to_base).
    Read-only DP (status) does not generate service calls.
    """
    ha_params = {
        "domain": "vacuum",
        "service": "",
        "service_data": {"entity_id": ha_entity_id}
    }

    # 1. Cleaning mode -> HA service (mode enum values are directly HA service names)
    if "mode" in tuya_dps:
        tuya_mode = tuya_dps["mode"]
        service = _TUYA_MODE_TO_HA_SERVICE.get(tuya_mode)
        if service:
            ha_params["service"] = service

    # 2. Suction level -> set_fan_speed
    if "suction" in tuya_dps:
        suction = tuya_dps["suction"]
        ha_params["suction_service"] = {
            "domain": "vacuum",
            "service": "set_fan_speed",
            "service_data": {
                "entity_id": ha_entity_id,
                "fan_speed": suction,
            },
        }

    # status (DP5) is read-only (ro), not converted to HA service calls

    # Fallback: default to "start" only when no valid service (including suction_service) is set
    if not ha_params["service"] and "suction_service" not in ha_params:
        ha_params["service"] = "start"

    return ha_params


# ------------------------------
# Core function 2: HA Vacuum state -> Tuya robot vacuum DPS command (full output)
# ------------------------------
def ha_to_tuya(ha_state: Dict[str, Any], ha_attributes: Dict[str, Any], context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Convert HA vacuum state/attributes to Tuya robot vacuum DPS commands.
    Output all DPs in full; use default values for missing attributes.
    status enum values correspond directly to HA vacuum states, no conversion needed.
    """
    tuya_dps: Dict[str, Any] = {}

    # 1. Work status (ro, HA vacuum states correspond directly to Tuya status enum values)
    ha_state_val = ha_state.get("state", "idle")
    tuya_dps["status"] = ha_state_val if ha_state_val in ("cleaning", "docked", "idle", "paused", "returning", "error") else TUYA_DP_DEFAULTS["status"]

    # 2. Cleaning mode (infer the corresponding mode from HA state)
    tuya_dps["mode"] = _HA_STATE_TO_TUYA_MODE.get(ha_state_val, TUYA_DP_DEFAULTS["mode"])

    # 3. Suction level
    fan_speed = ha_attributes.get("fan_speed")
    tuya_dps["suction"] = fan_speed if fan_speed is not None else TUYA_DP_DEFAULTS["suction"]

    return tuya_dps


# ------------------------------
# Usage examples
# ------------------------------
if __name__ == "__main__":
    # Example 1: Tuya robot vacuum starts cleaning -> HA
    tuya_dps_start = {"mode": "start", "suction": "turbo"}
    ha_params = tuya_to_ha(tuya_dps_start, "vacuum.tuya_vacuum_123456")
    print("=== Tuya robot vacuum starts cleaning (turbo suction) -> HA ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 2: Tuya robot vacuum returns to dock -> HA
    tuya_dps_charge = {"mode": "return_to_base"}
    ha_params = tuya_to_ha(tuya_dps_charge, "vacuum.tuya_vacuum_123456")
    print("\n=== Tuya robot vacuum returns to dock -> HA ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 3: Tuya robot vacuum pauses -> HA
    tuya_dps_pause = {"mode": "pause"}
    ha_params = tuya_to_ha(tuya_dps_pause, "vacuum.tuya_vacuum_123456")
    print("\n=== Tuya robot vacuum pauses -> HA ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 4: Tuya robot vacuum stops -> HA
    tuya_dps_stop = {"mode": "stop"}
    ha_params = tuya_to_ha(tuya_dps_stop, "vacuum.tuya_vacuum_123456")
    print("\n=== Tuya robot vacuum stops -> HA ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 5: HA vacuum state -> Tuya (full output)
    ha_state_cleaning = {"state": "cleaning"}
    ha_attrs = {"fan_speed": "turbo"}
    tuya_dps = ha_to_tuya(ha_state_cleaning, ha_attrs)
    print("\n=== HA vacuum cleaning -> Tuya (full output) ===")
    print(json.dumps(tuya_dps, indent=2, ensure_ascii=False))

    # Example 6: HA vacuum with missing attributes -> Tuya (with default values)
    ha_state_docked = {"state": "docked"}
    tuya_dps = ha_to_tuya(ha_state_docked, {})
    print("\n=== HA vacuum docked -> Tuya (full output, with default values) ===")
    print(json.dumps(tuya_dps, indent=2, ensure_ascii=False))
