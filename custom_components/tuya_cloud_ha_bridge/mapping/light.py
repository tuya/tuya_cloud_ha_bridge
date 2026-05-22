import json
from typing import Any, Dict


# ------------------------------
# Default HSV values (used when reporting light-off state)
# ------------------------------
DEFAULT_HSV = {"h": 0, "s": 0, "v": 0}


# ------------------------------
# Utility function: numeric range validation
# ------------------------------
def validate_value(value: Any, min_val: int, max_val: int, param_name: str) -> None:
    """Validate that a value is within the specified range; raise an exception if out of range."""
    if not (min_val <= value <= max_val):
        raise ValueError(f"Parameter {param_name} value {value} is out of range [{min_val}, {max_val}]")


def _validate_tuya_hsv(colour_data: Any) -> dict[str, int]:
    """Validate and return Tuya HSV color data."""
    if not isinstance(colour_data, dict) or not all(
        key in colour_data for key in ("h", "s", "v")
    ):
        raise ValueError("colour_data format error, must contain h/s/v keys")

    hsv_data = {
        "h": int(colour_data["h"]),
        "s": int(colour_data["s"]),
        "v": int(colour_data["v"]),
    }
    validate_value(hsv_data["h"], 0, 360, "colour_data.h")
    validate_value(hsv_data["s"], 0, 1000, "colour_data.s")
    validate_value(hsv_data["v"], 0, 1000, "colour_data.v")
    return hsv_data


def _validate_ha_hs_color(hs_color: Any) -> tuple[float, float]:
    """Validate and return HA hs_color."""
    if not isinstance(hs_color, (list, tuple)) or len(hs_color) != 2:
        raise ValueError("hs_color format error, must contain two values: h and s")

    hue = float(hs_color[0])
    saturation = float(hs_color[1])
    if not 0 <= hue <= 360:
        raise ValueError(f"Parameter hs_color[0] value {hue} is out of range [0, 360]")
    if not 0 <= saturation <= 100:
        raise ValueError(f"Parameter hs_color[1] value {saturation} is out of range [0, 100]")

    return hue, saturation


def _tuya_hsv_value_to_ha_brightness(value: int) -> int:
    """Convert Tuya HSV value (v, 0-1000) to HA brightness (0-255)."""
    validate_value(value, 0, 1000, "colour_data.v")
    return round(value * 255 / 1000)


def _ha_brightness_to_tuya_hsv_value(brightness: int) -> int:
    """Convert HA brightness (0-255) to Tuya HSV value (v, 0-1000)."""
    validate_value(brightness, 0, 255, "brightness")
    return round(brightness * 1000 / 255)


def _tuya_hsv_to_ha_hs_brightness(
    colour_data: dict[str, int],
) -> tuple[list[float], int]:
    """Convert Tuya HSV to HA hs_color and brightness."""
    return (
        [float(colour_data["h"]), round(colour_data["s"] / 10, 1)],
        _tuya_hsv_value_to_ha_brightness(colour_data["v"]),
    )


def _ha_hs_brightness_to_tuya_hsv(
    hs_color: Any,
    brightness: int | None,
) -> dict[str, int]:
    """Convert HA hs_color and brightness to Tuya HSV."""
    hue, saturation = _validate_ha_hs_color(hs_color)
    value = 1000 if brightness is None else _ha_brightness_to_tuya_hsv_value(brightness)
    return {
        "h": round(hue) % 360 if hue != 360 else 360,
        "s": round(saturation * 10),
        "v": value,
    }


# ------------------------------
# Core function 1: Tuya command → HA Light command
# Only handles switch_led + colour_data (HSV)
# ------------------------------
def tuya_to_ha(tuya_dps: Dict[str, Any], ha_entity_id: str, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Convert Tuya DPS commands to HA Light service call parameters.

    Only handles switch_led (on/off) and colour_data (HSV color).
    """
    ha_params: Dict[str, Any] = {
        "domain": "light",
        "service": "",
        "service_data": {"entity_id": ha_entity_id},
    }

    # 1. Switch command: if switch_led is absent but colour_data is present, treat as turn-on
    if "switch_led" not in tuya_dps and "colour_data" in tuya_dps:
        switch_led = True
    else:
        switch_led = tuya_dps.get("switch_led", True)

    if not switch_led:
        ha_params["service"] = "turn_off"
        return ha_params

    ha_params["service"] = "turn_on"

    # 2. Color data colour_data (Tuya HSV → HA hs_color + brightness)
    if "colour_data" in tuya_dps:
        raw_colour = tuya_dps["colour_data"]
        if isinstance(raw_colour, str):
            raw_colour = json.loads(raw_colour)
        colour_data = _validate_tuya_hsv(raw_colour)
        ha_hs_color, ha_brightness = _tuya_hsv_to_ha_hs_brightness(colour_data)
        ha_params["service_data"]["hs_color"] = ha_hs_color
        ha_params["service_data"]["brightness"] = ha_brightness

    return ha_params


# ------------------------------
# Core function 2: HA Light state → Tuya DPS command
# Only reports switch_led + colour_data (HSV)
# When the light is off, colour_data uses default values
# ------------------------------
def ha_to_tuya(ha_state: Dict[str, Any], ha_attributes: Dict[str, Any], context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Convert HA light state/attributes to Tuya DPS commands.

    Only reports switch_led and colour_data (HSV).
    When the light is off, colour_data reports default values.
    """
    tuya_dps: Dict[str, Any] = {}

    # 1. On/off switch
    is_on = ha_state.get("state") == "on"
    tuya_dps["switch_led"] = is_on

    # 2. Color data colour_data
    if not is_on:
        # Report default HSV when the light is off
        tuya_dps["colour_data"] = json.dumps(DEFAULT_HSV, separators=(",", ":"))
        return tuya_dps

    # When the light is on, build HSV from HA attributes
    if "hs_color" in ha_attributes:
        brightness = ha_attributes.get("brightness")
        if brightness is not None:
            validate_value(brightness, 0, 255, "brightness")
        hsv_dict = _ha_hs_brightness_to_tuya_hsv(
            ha_attributes["hs_color"],
            brightness,
        )
        tuya_dps["colour_data"] = json.dumps(hsv_dict, separators=(",", ":"))
    elif "brightness" in ha_attributes:
        # When only brightness is available without color, use h=0, s=0 for white light, v for brightness
        brightness = ha_attributes["brightness"]
        validate_value(brightness, 0, 255, "brightness")
        hsv_dict = {
            "h": 0,
            "s": 0,
            "v": _ha_brightness_to_tuya_hsv_value(brightness),
        }
        tuya_dps["colour_data"] = json.dumps(hsv_dict, separators=(",", ":"))

    return tuya_dps


# ------------------------------
# Usage examples
# ------------------------------
if __name__ == "__main__":
    # Example 1: Tuya turn-on + color → HA
    tuya_dps_example = {
        "switch_led": True,
        "colour_data": {"h": 120, "s": 1000, "v": 500},
    }
    ha_params = tuya_to_ha(tuya_dps_example, "light.tuya_living_room")
    print("=== Example 1: Tuya → HA (turn on + green) ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 2: Tuya turn-off → HA
    tuya_off_example = {"switch_led": False}
    ha_params = tuya_to_ha(tuya_off_example, "light.tuya_bedroom")
    print("\n=== Example 2: Tuya → HA (turn off) ===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # Example 3: HA turn-on + color → Tuya
    ha_state_on = {"state": "on"}
    ha_attrs_colour = {"brightness": 150, "hs_color": [120, 100]}
    tuya_dps = ha_to_tuya(ha_state_on, ha_attrs_colour)
    print("\n=== Example 3: HA → Tuya (turn on + green) ===")
    print(json.dumps(tuya_dps, indent=2, ensure_ascii=False))

    # Example 4: HA turn-off → Tuya (colour_data uses default values)
    ha_state_off = {"state": "off"}
    ha_attrs_off = {}
    tuya_dps = ha_to_tuya(ha_state_off, ha_attrs_off)
    print("\n=== Example 4: HA → Tuya (turn off, HSV defaults) ===")
    print(json.dumps(tuya_dps, indent=2, ensure_ascii=False))
