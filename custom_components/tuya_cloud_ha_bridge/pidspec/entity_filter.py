"""Entity inclusion filter for DeviceProfile construction & report serialization.

Implements the global exclusion rules:

1. EXCLUDED_DOMAINS      — always excluded
2. EXCLUDED_CATEGORIES   — diagnostic / config 默认排除；diagnostic 中
                          device_class 命中 DIAGNOSTIC_DEVICE_CLASS_WHITELIST
                          的实体（battery / temperature / humidity / tamper 等
                          承载 Tuya DP 的电池/环境/告警类）保留
3. CONDITIONAL_DOMAINS   — always excluded (no Tuya DP mapping)

Public API:
- should_include_entity(entity) -> bool
"""

from __future__ import annotations

from .models import EntityProfile

# Domains with no Tuya control / reporting meaning — always excluded.
EXCLUDED_DOMAINS: frozenset[str] = frozenset(
    {
        "update",                   # 固件更新实体
        "persistent_notification",  # HA 系统通知
        "tts",                      # 文本转语音
        "scene",                    # HA 场景（非设备实体）
        "device_tracker",           # 设备追踪（隐私敏感）
    }
)

# Entity categories with no control meaning — always excluded.
EXCLUDED_CATEGORIES: frozenset[str] = frozenset(
    {
        "diagnostic",               # 诊断实体（信号强度、内存、温度等）
        "config",                   # 配置实体（灵敏度、LED 指示灯开关等）
    }
)

# device_class 白名单：即使 entity_category=diagnostic，也保留这些实体，
# 因为它们承载真正的 Tuya DP（battery_percentage / temp_current / humidity_value /
# tamper_alarm / power / energy / air_quality 等），过滤掉会让 optional_dp_coverage
# 拿不到加分、optional_domain_coverage(sensor) 也跟着丢分。
# 与 rules.json 的 feature_rules.device_class_features 对齐。
DIAGNOSTIC_DEVICE_CLASS_WHITELIST: frozenset[str] = frozenset(
    {
        "battery",
        "temperature",
        "humidity",
        "tamper",
        "power",
        "energy",
        "voltage",
        "current",
        "pm25",
        "pm10",
        "co2",
        "illuminance",
        "moisture",
    }
)

# Domains with no Tuya DP mapping — always excluded from the profile entirely.
CONDITIONAL_DOMAINS: frozenset[str] = frozenset(
    {
        "event",                    # 一次性事件触发（无持久状态）
        "notify",                   # 通知（send-only）
    }
)

# Domains INCLUDED in the profile (so a matched spec's DPs CAN route to them)
# but EXCLUDED from inference — they never enter domain_set nor the feature /
# carrier checks, so they cannot influence which PID matches or the score.
# Purpose: momentary actions like a projector's play/pause `button` entities.
# Routing (dp_routing) still binds them; only inference.py ignores them.
# NOTE: config/diagnostic buttons (restart, identify) are still dropped earlier
# by the entity_category filter, so only primary action buttons survive.
INFERENCE_EXCLUDED_DOMAINS: frozenset[str] = frozenset({"button"})


def should_include_entity(
    entity: EntityProfile,
) -> bool:
    """Return True if *entity* should be included in DeviceProfile / report.

    Args:
        entity: The EntityProfile (or any object with `.domain` and
            `.entity_category`) to evaluate.
    """
    # 1. Global domain exclusion
    if entity.domain in EXCLUDED_DOMAINS:
        return False

    # 2. Entity category exclusion — diagnostic 默认排除，但 device_class
    # 命中 DIAGNOSTIC_DEVICE_CLASS_WHITELIST 的实体例外（它们承载 Tuya DP）。
    if entity.entity_category in EXCLUDED_CATEGORIES:
        if (
            entity.entity_category == "diagnostic"
            and getattr(entity, "device_class", None) in DIAGNOSTIC_DEVICE_CLASS_WHITELIST
        ):
            pass
        else:
            return False

    # 3. Conditional domain — always excluded (no Tuya DP mapping)
    if entity.domain in CONDITIONAL_DOMAINS:
        return False

    return True
