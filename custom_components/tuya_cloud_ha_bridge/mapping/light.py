import json
from typing import Any, Dict


# ------------------------------
# 默认HSV值（灯关闭时上报用）
# ------------------------------
DEFAULT_HSV = {"h": 0, "s": 0, "v": 0}


# ------------------------------
# 工具函数：数值范围校验
# ------------------------------
def validate_value(value: Any, min_val: int, max_val: int, param_name: str) -> None:
    """校验数值是否在指定范围内，超出则抛异常。"""
    if not (min_val <= value <= max_val):
        raise ValueError(f"参数{param_name}值{value}超出范围[{min_val}, {max_val}]")


def _validate_tuya_hsv(colour_data: Any) -> dict[str, int]:
    """校验并返回Tuya HSV彩光数据。"""
    if not isinstance(colour_data, dict) or not all(
        key in colour_data for key in ("h", "s", "v")
    ):
        raise ValueError("colour_data格式错误，需包含h/s/v键")

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
    """校验并返回HA hs_color。"""
    if not isinstance(hs_color, (list, tuple)) or len(hs_color) != 2:
        raise ValueError("hs_color格式错误，需包含h/s两个值")

    hue = float(hs_color[0])
    saturation = float(hs_color[1])
    if not 0 <= hue <= 360:
        raise ValueError(f"参数hs_color[0]值{hue}超出范围[0, 360]")
    if not 0 <= saturation <= 100:
        raise ValueError(f"参数hs_color[1]值{saturation}超出范围[0, 100]")

    return hue, saturation


def _tuya_hsv_value_to_ha_brightness(value: int) -> int:
    """将Tuya HSV明度(v)转换为HA亮度(0-255)。"""
    validate_value(value, 0, 1000, "colour_data.v")
    return round(value * 255 / 1000)


def _ha_brightness_to_tuya_hsv_value(brightness: int) -> int:
    """将HA亮度(0-255)转换为Tuya HSV明度(0-1000)。"""
    validate_value(brightness, 0, 255, "brightness")
    return round(brightness * 1000 / 255)


def _tuya_hsv_to_ha_hs_brightness(
    colour_data: dict[str, int],
) -> tuple[list[float], int]:
    """将Tuya HSV转换为HA hs_color和brightness。"""
    return (
        [float(colour_data["h"]), round(colour_data["s"] / 10, 1)],
        _tuya_hsv_value_to_ha_brightness(colour_data["v"]),
    )


def _ha_hs_brightness_to_tuya_hsv(
    hs_color: Any,
    brightness: int | None,
) -> dict[str, int]:
    """将HA hs_color和brightness转换为Tuya HSV。"""
    hue, saturation = _validate_ha_hs_color(hs_color)
    value = 1000 if brightness is None else _ha_brightness_to_tuya_hsv_value(brightness)
    return {
        "h": round(hue) % 360 if hue != 360 else 360,
        "s": round(saturation * 10),
        "v": value,
    }


# ------------------------------
# 核心函数1：涂鸦指令 → HA Light指令
# 只处理 switch_led + colour_data(HSV)
# ------------------------------
def tuya_to_ha(tuya_dps: Dict[str, Any], ha_entity_id: str) -> Dict[str, Any]:
    """将涂鸦DPS指令转换为HA Light服务调用参数。

    仅处理 switch_led（开关）和 colour_data（HSV彩光）。
    """
    ha_params: Dict[str, Any] = {
        "domain": "light",
        "service": "",
        "service_data": {"entity_id": ha_entity_id},
    }

    # 1. 开关指令：没有switch_led但有colour_data时，视为开灯
    if "switch_led" not in tuya_dps and "colour_data" in tuya_dps:
        switch_led = True
    else:
        switch_led = tuya_dps.get("switch_led", True)

    if not switch_led:
        ha_params["service"] = "turn_off"
        return ha_params

    ha_params["service"] = "turn_on"

    # 2. 彩光 colour_data（Tuya HSV → HA hs_color + brightness）
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
# 核心函数2：HA Light状态 → 涂鸦DPS指令
# 只上报 switch_led + colour_data(HSV)
# 灯关闭时 colour_data 给默认值
# ------------------------------
def ha_to_tuya(ha_state: Dict[str, Any], ha_attributes: Dict[str, Any]) -> Dict[str, Any]:
    """从HA灯光状态/属性转换为涂鸦DPS指令。

    仅上报 switch_led 和 colour_data（HSV）。
    灯关闭时 colour_data 上报默认值。
    """
    tuya_dps: Dict[str, Any] = {}

    # 1. 开关
    is_on = ha_state.get("state") == "on"
    tuya_dps["switch_led"] = is_on

    # 2. 彩光 colour_data
    if not is_on:
        # 灯关闭时上报默认HSV
        tuya_dps["colour_data"] = json.dumps(DEFAULT_HSV, separators=(",", ":"))
        return tuya_dps

    # 灯开启时，从HA属性构建HSV
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
        # 只有亮度没有颜色时，用h=0,s=0表示白光，v取亮度
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
# 调用示例
# ------------------------------
if __name__ == "__main__":
    # 示例 1：涂鸦开灯+彩光 → HA
    tuya_dps_example = {
        "switch_led": True,
        "colour_data": {"h": 120, "s": 1000, "v": 500},
    }
    ha_params = tuya_to_ha(tuya_dps_example, "light.tuya_living_room")
    print("=== 示例 1: 涂鸦→HA（开灯+绿色）===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # 示例 2：涂鸦关灯 → HA
    tuya_off_example = {"switch_led": False}
    ha_params = tuya_to_ha(tuya_off_example, "light.tuya_bedroom")
    print("\n=== 示例 2: 涂鸦→HA（关灯）===")
    print(json.dumps(ha_params, indent=2, ensure_ascii=False))

    # 示例 3：HA开灯+彩色 → 涂鸦
    ha_state_on = {"state": "on"}
    ha_attrs_colour = {"brightness": 150, "hs_color": [120, 100]}
    tuya_dps = ha_to_tuya(ha_state_on, ha_attrs_colour)
    print("\n=== 示例 3: HA→涂鸦（开灯+绿色）===")
    print(json.dumps(tuya_dps, indent=2, ensure_ascii=False))

    # 示例 4：HA关灯 → 涂鸦（colour_data给默认值）
    ha_state_off = {"state": "off"}
    ha_attrs_off = {}
    tuya_dps = ha_to_tuya(ha_state_off, ha_attrs_off)
    print("\n=== 示例 4: HA→涂鸦（关灯，HSV默认值）===")
    print(json.dumps(tuya_dps, indent=2, ensure_ascii=False))
