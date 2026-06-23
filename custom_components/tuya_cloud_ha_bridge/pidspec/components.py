"""Component discovery for entity profiles using baseline and cloud rules."""

from __future__ import annotations

from .models import ComponentRule, EntityProfile, LocalBaselineComponentRules


def discover_component(
    entity: EntityProfile,
    local_rules: LocalBaselineComponentRules,
    cloud_rules: list[ComponentRule] | None = None,
) -> tuple[str, str]:
    """Determine the component identifier for an entity.

    Priority chain (first match wins):
    1. platform_signals  — integration-specific attribute signal
    2. channel_patterns  — token match in entity_id / friendly_name / original_name
    3. semantic_keywords — keyword match in search text (local + cloud rules merged)
    4. entity_fallback   — f"entity:{entity.entity_id}"

    Returns:
        (component, component_source) where component_source is one of:
        "platform_signal" | "channel_number" | "semantic_hint" | "entity_fallback"
    """

    # Step 1 — platform_signals
    signal_cfg = local_rules.platform_signals.get(entity.integration)
    if signal_cfg:
        attr_key = signal_cfg.get("attr")
        template = signal_cfg.get("template", "")
        if attr_key and attr_key in entity.attributes:
            value = entity.attributes[attr_key]
            component = template.replace("{value}", str(value))
            return component, "platform_signal"

    # Step 2 — channel_patterns
    search_text = " ".join(
        [entity.entity_id, entity.friendly_name, entity.original_name]
    ).lower()
    for pattern in local_rules.channel_patterns:
        token = pattern.get("token", "")
        if token and token.lower() in search_text:
            return pattern["component"], "channel_number"

    # Step 3 — semantic_keywords (local rules merged with cloud rules)
    all_semantic: list[ComponentRule] = list(local_rules.semantic_keywords)
    if cloud_rules:
        all_semantic.extend(cloud_rules)

    for rule in all_semantic:
        for keyword in rule.keywords:
            if keyword.lower() in search_text:
                return rule.component, "semantic_hint"

    # Step 4 — entity_fallback
    return f"entity:{entity.entity_id}", "entity_fallback"
