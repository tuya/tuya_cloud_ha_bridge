import json
from typing import Any, Dict

# ------------------------------
# 核心配置：涂鸦加湿器与HA Humidifier的映射关系
# ------------------------------
# 1. 涂鸦加湿器核心指令配置（校验规则）
TUYA_HUMIDIFIER_CORE_DPS = {
    "switch": {"type": "Boolean"},  # 总开关
    "humidity_set": {"type": "Integer"},  # 湿度设置（range 动态获取）
    "humidity_current": {"type": "Integer"},  # 当前湿度（仅上报，range 动态获取）
    "mode": {"type": "Enum"},  # 工作模式（range 动态获取）
}

# 属性元数据：用于 bind 时上报 defaultProperties（含 range）
# range_attr: HA 实体属性名，bind 时从实体状态动态读取枚举范围（不写死）
TUYA_PROPERTY_METADATA: Dict[str, Dict[str, Any]] = {
    "mode": {
        "range_attr": "available_modes",
    },
}


# ------------------------------
# 工具函数：参数校验
# ------------------------------
def validate_tuya_param(param_name: str, value: Any) -> None:
    """校验涂鸦加湿器参数的类型（range 由运行时动态获取，不做静态范围校验）"""
    config = TUYA_HUMIDIFIER_CORE_DPS.get(param_name)
    if not config:
        return  # 未配置的参数不校验

    # 1. 布尔型校验
    if config["type"] == "Boolean":
        if not isinstance(value, bool):
            raise ValueError(f"参数{param_name}值{value}类型错误，必须为Boolean（true/false）")
    # 2. 数值型仅校验类型，不校验范围（range 动态获取）
    elif config["type"] == "Integer":
        if not isinstance(value, int):
            raise ValueError(f"参数{param_name}值{value}类型错误，必须为Integer")



# ------------------------------
# 核心函数1：涂鸦加湿器指令 → HA Humidifier指令
# ------------------------------
def tuya_to_ha(tuya_dps: Dict[str, Any], ha_entity_id: str) -> Dict[str, Any]:
    """
    解析涂鸦加湿器DPS指令，转换为HA Humidifier服务调用参数
    :param tuya_dps: 涂鸦DPS字典（如{"switch":true, "humidity_set":60...}）
    :param ha_entity_id: HA加湿器实体ID（如humidifier.tuya_humidifier_123456）
    :return: HA Humidifier服务调用参数（domain/service/service_data）
    """
    ha_params = {
        "domain": "humidifier",
        "service": "",
        "service_data": {"entity_id": ha_entity_id}
    }

    # 1. 核心开关指令（如果没有提供，默认为 true）
    switch_val = tuya_dps.get("switch", True)
    validate_tuya_param("switch", switch_val)

    if switch_val:
        # 开机：优先处理湿度设置，无湿度则仅开机
        if "humidity_set" in tuya_dps:
            humidity = tuya_dps["humidity_set"]
            validate_tuya_param("humidity_set", humidity)
            ha_params["service"] = "set_humidity"
            ha_params["service_data"]["humidity"] = humidity
        else:
            ha_params["service"] = "turn_on"
    else:
        # 关机指令
        ha_params["service"] = "turn_off"

    # 2. 工作模式（mode → set_mode，涂鸦与HA模式一致，无需转换）
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

    # humidity_current仅为上报状态，不转换为HA指令（用于状态同步）
    return ha_params


# ------------------------------
# 核心函数2：HA Humidifier指令 → 涂鸦加湿器DPS指令（反向同步用）
# ------------------------------
def ha_to_tuya(ha_state: Dict[str, Any], ha_attributes: Dict[str, Any]) -> Dict[str, Any]:
    """
    从HA Humidifier状态/属性转换为涂鸦加湿器DPS指令
    :param ha_state: HA状态（如{"state":"on"}）
    :param ha_attributes: HA属性（如{"humidity":60, "current_humidity":45...}）
    :return: 涂鸦DPS字典
    """
    tuya_dps = {}

    # 1. 开关状态映射（HA state为on则开机，否则关机）
    ha_state_val = ha_state.get("state", "off")
    tuya_dps["switch"] = True if ha_state_val == "on" else False

    # 2. 目标湿度反向映射
    if "humidity" in ha_attributes:
        ha_humidity = ha_attributes["humidity"]
        tuya_humidity = int(ha_humidity)
        validate_tuya_param("humidity_set", tuya_humidity)
        tuya_dps["humidity_set"] = tuya_humidity

    # 3. 工作模式反向映射（HA mode → 涂鸦 mode）
    if "mode" in ha_attributes:
        tuya_dps["mode"] = ha_attributes["mode"]

    # 4. 同步当前湿度（仅上报参数）
    if "current_humidity" in ha_attributes:
        tuya_dps["humidity_current"] = int(ha_attributes["current_humidity"])

    return tuya_dps


# ------------------------------
# 实战调用示例
# ------------------------------
if __name__ == "__main__":
    # 示例1：涂鸦加湿器开机（60%湿度）→ HA指令
    tuya_dps_on = {
        "switch": True,
        "humidity_set": 60,
        "mode": "comfort",
    }
    ha_params_on = tuya_to_ha(tuya_dps_on, "humidifier.tuya_humidifier_123456")
    print("=== 涂鸦加湿器开机（60%湿度, comfort模式）→ HA转换结果 ===")
    print(json.dumps(ha_params_on, indent=2, ensure_ascii=False))

    # 示例2：涂鸦加湿器关机指令 → HA指令
    tuya_dps_off = {
        "switch": False
    }
    ha_params_off = tuya_to_ha(tuya_dps_off, "humidifier.tuya_humidifier_123456")
    print("\n=== 涂鸦加湿器关机→HA转换结果 ===")
    print(json.dumps(ha_params_off, indent=2, ensure_ascii=False))

    # 示例3：HA设置加湿器50%湿度 → 涂鸦指令
    ha_state_set = {"state": "on"}
    ha_attributes_set = {
        "humidity": 50,
        "current_humidity": 42,
        "mode": "eco",
    }
    tuya_dps_set = ha_to_tuya(ha_state_set, ha_attributes_set)
    print("\n=== HA设置加湿器50%湿度→涂鸦转换结果 ===")
    print(json.dumps(tuya_dps_set, indent=2, ensure_ascii=False))


# ------------------------------
# HA插件中异步调用HA Humidifier服务
# ------------------------------
async def call_ha_humidifier_service(hass, ha_params: Dict[str, Any]):
    """
    执行HA Humidifier服务调用（需在HA异步上下文执行）
    :param hass: HA的hass对象（插件上下文）
    :param ha_params: tuya_to_ha返回的参数
    """
    await hass.services.async_call(
        domain=ha_params["domain"],
        service=ha_params["service"],
        service_data=ha_params["service_data"],
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