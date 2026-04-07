import json
from typing import Dict, Any, Optional, List, Union

# ------------------------------
# 核心配置：涂鸦开关与HA Switch的映射关系
# ------------------------------
# 涂鸦开关核心指令（适配涂鸦开关类设备标准DPS）
TUYA_SWITCH_CORE_DPS = {
    "switch": {"type": "Boolean", "min": None, "max": None}  # 核心开关（移除countdown配置）
}


# ------------------------------
# 工具函数：数值范围校验
# ------------------------------
def validate_value(value: Any, min_val: int, max_val: int, param_name: str) -> None:
    """校验数值是否在指定范围内，超出则抛异常（None表示不校验）"""
    if min_val is None or max_val is None:
        return
    if not (min_val <= value <= max_val):
        raise ValueError(f"参数{param_name}值{value}超出范围[{min_val}, {max_val}]")


# ------------------------------
# 核心函数1：涂鸦开关指令 → HA Switch指令
# ------------------------------
def tuya_to_ha(tuya_dps: Dict[str, Any], ha_entity_id: str) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    """
    解析涂鸦开关DPS指令，转换为HA Switch服务调用参数
    :param tuya_dps: 涂鸦DPS字典（如{"switch":true...}或{"switch_1":true, "switch_2":false...}）
    :param ha_entity_id: HA开关实体ID（如switch.tuya_wall_switch_123456）
    :return: HA服务调用参数（单键返回字典，多键返回列表）
    """
    # 第一步：筛选单键/多键开关字段
    root_switch_exists = "switch_1" in tuya_dps
    sub_switch_keys = [k for k in tuya_dps.keys() if k.startswith("switch_") and k != "switch_1"]
    #sub_switch_keys = []

    # 校验：既无单键也无多键，抛异常
    if not root_switch_exists and not sub_switch_keys:
        raise ValueError("涂鸦开关指令缺少开关参数：需包含switch（单键）或switch_*（多键）")

    # 第二步：处理单键开关（移除countdown相关逻辑）
    ha_params_list = []
    if root_switch_exists:
        ha_params = {
            "domain": "switch",
            "service": "",
            "service_data": {"entity_id": ha_entity_id}
        }
        # 校验根级switch类型
        switch_val = tuya_dps["switch_1"]
        if not isinstance(switch_val, bool):
            raise ValueError(f"参数switch值{switch_val}类型错误，必须为Boolean（true/false）")
        # 映射开关状态
        ha_params["service"] = "turn_on" if switch_val else "turn_off"
        ha_params_list.append(ha_params)

    # 第三步：处理多键开关（移除countdown相关逻辑）
    for key in sub_switch_keys:
        sub_switch_val = tuya_dps[key]
        if not isinstance(sub_switch_val, bool):
            raise ValueError(f"参数{key}值{sub_switch_val}类型错误，必须为Boolean")
        # 生成子开关EntityID（如switch.tuya_wall_switch_123456_1）
        sub_num = key.split("_")[1]
        sub_entity_id = f"{ha_entity_id}_{sub_num}"
        # 构建子开关HA参数
        sub_ha_params = {
            "domain": "switch",
            "service": "turn_on" if sub_switch_val else "turn_off",
            "service_data": {"entity_id": sub_entity_id}
        }
        ha_params_list.append(sub_ha_params)

    # 单键返回单个字典，多键返回列表
    return ha_params_list[0] if len(ha_params_list) == 1 else ha_params_list


# ------------------------------
# 核心函数2：HA Switch指令 → 涂鸦开关DPS指令（反向同步用）
# ------------------------------
def ha_to_tuya(ha_state: Dict[str, Any], ha_attributes: Dict[str, Any]) -> Dict[str, Any]:
    """
    从HA开关状态/属性转换为涂鸦DPS指令
    :param ha_state: HA状态（如{"state":"on"}）
    :param ha_attributes: HA属性
    :return: 涂鸦DPS字典
    """
    tuya_dps = {}

    # 1. 核心开关状态映射
    ha_state_val = ha_state.get("state")
    if ha_state_val not in ["on", "off"]:
        raise ValueError(f"HA开关状态{ha_state_val}非法，仅支持on/off")

    # tuya_dps["switch"] = True if ha_state_val == "on" else False
    tuya_dps["switch_1"] = True if ha_state_val == "on" else False
    # 扩展：多键开关反向映射（适配HA多键开关实体）
    if "sub_switches" in ha_attributes:
        for sub_switch in ha_attributes["sub_switches"]:
            sub_entity_id = sub_switch["entity_id"]
            sub_state = sub_switch["state"]
            # 提取子开关编号（如switch.tuya_wall_switch_123456_1 → 1）
            sub_num = sub_entity_id.split("_")[-1]
            tuya_dps[f"switch_{sub_num}"] = True if sub_state == "on" else False

    return tuya_dps


# ------------------------------
# 实战调用示例
# ------------------------------
if __name__ == "__main__":
    # 示例1：涂鸦单键开关指令 → HA指令（移除countdown参数）
    tuya_dps_single = {
        "switch": True
    }
    ha_params_single = tuya_to_ha(tuya_dps_single, "switch.tuya_wall_switch_123456")
    print("=== 涂鸦单键开关→HA转换结果 ===")
    print(json.dumps(ha_params_single, indent=2, ensure_ascii=False))

    # 示例2：涂鸦双键开关指令 → HA指令（移除countdown参数）
    tuya_dps_double = {
        "switch_1": True,
        "switch_2": False
    }
    ha_params_double = tuya_to_ha(tuya_dps_double, "switch.tuya_wall_switch_123456")
    print("\n=== 涂鸦双键开关→HA转换结果 ===")
    print(json.dumps(ha_params_double, indent=2, ensure_ascii=False))

    # 示例3：HA开关指令 → 涂鸦指令（移除countdown参数）
    ha_state_example = {"state": "on"}
    ha_attributes_example = {}  # 清空countdown相关属性
    tuya_dps = ha_to_tuya(ha_state_example, ha_attributes_example)
    print("\n=== HA→涂鸦开关转换结果 ===")
    print(json.dumps(tuya_dps, indent=2, ensure_ascii=False))


# ------------------------------
# HA插件中异步调用HA Switch服务
# ------------------------------
async def call_ha_switch_service(hass, ha_params: Union[Dict[str, Any], List[Dict[str, Any]]]):
    """
    执行HA Switch服务调用（需在HA异步上下文执行）
    :param hass: HA的hass对象（插件上下文）
    :param ha_params: tuya_to_ha返回的参数（单键/多键）
    """
    # 兼容单键/多键场景
    params_list = [ha_params] if isinstance(ha_params, dict) else ha_params
    for params in params_list:
        await hass.services.async_call(
            domain=params["domain"],
            service=params["service"],
            service_data=params["service_data"],
            blocking=True
        )


# ------------------------------
# MQTT指令解析封装（适配TuyaLink MQTT完整格式）
# ------------------------------
def parse_tuya_mqtt_payload(mqtt_payload: str) -> Dict[str, Any]:
    """
    解析TuyaLink MQTT收到的完整payload，提取DPS字典
    :param mqtt_payload: MQTT收到的JSON字符串（含msgId/time/dps）
    :return: 涂鸦DPS字典
    """
    payload = json.loads(mqtt_payload)
    dps = payload.get("dps", {})
    if not dps:
        raise ValueError("TuyaLink MQTT payload缺少dps字段")
    return dps