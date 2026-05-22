import json
from typing import Any, Dict

# ------------------------------
# Core config: Tuya humidifier to HA Humidifier mapping
# ------------------------------
# 1. Tuya humidifier core command config (validation rules)
TUYA_HUMIDIFIER_CORE_DPS = {
    "switch": {"type": "Boolean"},  # Main switch
    "humidity_set": {"type": "Integer"},  # Humidity setting (range fetched dynamically)
    "humidity_current": {"type": "Integer"},  # Current humidity (report only, range fetched dynamically)
    "mode": {"type": "Enum"},  # Operating mode (range fetched dynamically)
}

# Property metadata used to report `defaultProperties` during bind, including range info.
# `range_attr` is the HA entity attribute name. The enum range is read dynamically
# from the entity state during bind instead of being hard-coded.
TUYA_PROPERTY_METADATA: Dict[str, Dict[str, Any]] = {
    "mode": {
        "range_attr": "available_modes",
    },
}


# ------------------------------
# Helper function: parameter validation
# ------------------------------
def validate_tuya_param(param_name: str, value: Any) -> None:
    """Validate Tuya humidifier parameter types.

    Range values are fetched dynamically at runtime, so no static range
    validation is performed here.
    """
    config = TUYA_HUMIDIFIER_CORE_DPS.get(param_name)
    if not config:
        return  # Skip validation for parameters without an explicit config.

    # 1. Boolean validation
    if config["type"] == "Boolean":
        if not isinstance(value, bool):
            raise ValueError(
                f"Invalid type for parameter '{param_name}': {value!r}. Expected a Python boolean (True/False)."
            )
    # 2. Integer validation only. Range validation is handled dynamically.
    elif config["type"] == "Integer":
        if not isinstance(value, int):
            raise ValueError(
                f"Invalid type for parameter '{param_name}': {value!r}. Expected Integer."
            )



# ------------------------------
# Core function 1: Tuya humidifier command -> HA Humidifier service payload
# ------------------------------
def tuya_to_ha(tuya_dps: Dict[str, Any], ha_entity_id: str, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Parse Tuya humidifier DPS commands and convert them to HA Humidifier service call parameters.

    :param tuya_dps: Tuya DPS dictionary, for example {"switch": True, "humidity_set": 60}.
    :param ha_entity_id: HA humidifier entity ID, for example `humidifier.tuya_humidifier_123456`.
    :return: HA Humidifier service call payload (`domain` / `service` / `service_data`).
    """
    ha_params = {
        "domain": "humidifier",
        "service": "",
        "service_data": {"entity_id": ha_entity_id}
    }

    # 1. Main power command. Default to `true` when the field is missing.
    switch_val = tuya_dps.get("switch", True)
    validate_tuya_param("switch", switch_val)

    if switch_val:
        # Power on: apply humidity first when provided, otherwise just turn on.
        if "humidity_set" in tuya_dps:
            humidity = tuya_dps["humidity_set"]
            validate_tuya_param("humidity_set", humidity)
            ha_params["service"] = "set_humidity"
            ha_params["service_data"]["humidity"] = humidity
        else:
            ha_params["service"] = "turn_on"
    else:
        # Power off command
        ha_params["service"] = "turn_off"

    # 2. Operating mode (`mode` -> `set_mode`). Tuya and HA use the same mode values.
    if "mode" in tuya_dps:
        tuya_mode = tuya_dps["mode"]
        validate_tuya_param("mode", tuya_mode)
        ha_params["mode_service"] = {
            "domain": "humidifier",
            "service": "set_mode",
            "service_data": {
                "entity_id": ha_entity_id,
                "mode": tuya_mode,
            },
        }

    # `humidity_current` is report-only and is not converted to an HA command.
    return ha_params


# ------------------------------
# Core function 2: HA Humidifier state -> Tuya humidifier DPS payload
# ------------------------------
def ha_to_tuya(ha_state: Dict[str, Any], ha_attributes: Dict[str, Any], context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Convert HA Humidifier state and attributes into Tuya humidifier DPS commands.

    :param ha_state: HA state, for example {"state": "on"}.
    :param ha_attributes: HA attributes, for example {"humidity": 60, "current_humidity": 45}.
    :return: Tuya DPS dictionary.
    """
    tuya_dps = {}

    # 1. Power state mapping: HA `on` maps to `True`, everything else maps to `False`.
    ha_state_val = ha_state.get("state", "off")
    tuya_dps["switch"] = True if ha_state_val == "on" else False

    # 2. Reverse mapping for target humidity
    if "humidity" in ha_attributes:
        ha_humidity = ha_attributes["humidity"]
        tuya_humidity = int(ha_humidity)
        validate_tuya_param("humidity_set", tuya_humidity)
        tuya_dps["humidity_set"] = tuya_humidity

    # 3. Reverse mapping for operating mode (HA `mode` -> Tuya `mode`)
    if "mode" in ha_attributes:
        tuya_dps["mode"] = ha_attributes["mode"]

    # 4. Sync current humidity as a report-only parameter
    if "current_humidity" in ha_attributes:
        tuya_dps["humidity_current"] = int(ha_attributes["current_humidity"])

    return tuya_dps


# ------------------------------
# Usage examples
# ------------------------------
if __name__ == "__main__":
    # Example 1: Tuya humidifier power-on command (60% humidity) -> HA payload
    tuya_dps_on = {
        "switch": True,
        "humidity_set": 60,
        "mode": "comfort",
    }
    ha_params_on = tuya_to_ha(tuya_dps_on, "humidifier.tuya_humidifier_123456")
    print("=== Tuya humidifier power on (60% humidity, comfort mode) -> HA result ===")
    print(json.dumps(ha_params_on, indent=2, ensure_ascii=False))

    # Example 2: Tuya humidifier power-off command -> HA payload
    tuya_dps_off = {
        "switch": False
    }
    ha_params_off = tuya_to_ha(tuya_dps_off, "humidifier.tuya_humidifier_123456")
    print("\n=== Tuya humidifier power off -> HA result ===")
    print(json.dumps(ha_params_off, indent=2, ensure_ascii=False))

    # Example 3: HA sets humidifier to 50% humidity -> Tuya payload
    ha_state_set = {"state": "on"}
    ha_attributes_set = {
        "humidity": 50,
        "current_humidity": 42,
        "mode": "eco",
    }
    tuya_dps_set = ha_to_tuya(ha_state_set, ha_attributes_set)
    print("\n=== HA sets humidifier to 50% humidity -> Tuya result ===")
    print(json.dumps(tuya_dps_set, indent=2, ensure_ascii=False))


# ------------------------------
# Async helper for calling HA Humidifier services inside the integration
# ------------------------------
async def call_ha_humidifier_service(hass, ha_params: Dict[str, Any]):
    """
    Execute an HA Humidifier service call in the HA async context.

    :param hass: HA `hass` object from the integration context.
    :param ha_params: Payload returned by `tuya_to_ha`.
    """
    await hass.services.async_call(
        domain=ha_params["domain"],
        service=ha_params["service"],
        service_data=ha_params["service_data"],
        blocking=True
    )


# ------------------------------
# MQTT payload parser for the full TuyaLink MQTT message format
# ------------------------------
def parse_tuya_mqtt_payload(mqtt_payload: str) -> Dict[str, Any]:
    """
    Parse a full TuyaLink MQTT payload and extract the DPS dictionary.

    :param mqtt_payload: MQTT JSON string containing fields such as `msgId`, `time`, and `dps`.
    :return: Tuya DPS dictionary.
    """
    payload = json.loads(mqtt_payload)
    dps = payload.get("dps", {})
    if not dps:
        raise ValueError("TuyaLink MQTT payload is missing the 'dps' field.")
    return dps
