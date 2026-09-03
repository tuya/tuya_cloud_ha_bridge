"""Feature discovery for entity profiles using FeatureRules."""

from __future__ import annotations

from .models import EntityProfile, FeatureRules


def _condition_holds(condition: dict, value: object) -> bool:
    """Evaluate a domain_attr_features condition against the attribute value.

    Supported condition types:

    - ``bitfield_contains`` {"bit": N} — the int attribute has that bit set.
    - ``list_contains_any`` {"values": [...]} — the list attribute contains at
      least one of the listed values.

    ``list_contains_any`` exists because *key presence is not always evidence of
    capability*: HA's light platform publishes ``hs_color``/``rgb_color``/
    ``xy_color`` as None even on a colour-temperature-only lamp (verified across
    every light in the profile store), so a rule keyed on the attribute NAME
    grants a `color` feature the lamp does not have. The capability list
    (``supported_color_modes``) must be read by VALUE instead.

    An unknown condition type fails closed — the feature is not granted — so a
    rule naming a condition this engine does not implement cannot silently widen
    matching.
    """
    cond_type = condition.get("type")
    if cond_type == "bitfield_contains":
        bit = condition.get("bit")
        if bit is None:
            return True
        try:
            return bool(int(value or 0) & int(bit))
        except (TypeError, ValueError):
            return False
    if cond_type == "list_contains_any":
        wanted = condition.get("values") or []
        if not isinstance(value, (list, tuple, set, frozenset)):
            return False
        return any(v in value for v in wanted)
    return False


def discover_features(entity: EntityProfile, rules: FeatureRules) -> set[str]:
    """Derive the feature set for an entity by applying rules in priority order.

    Steps 1-4 are always applied (additive).
    Steps 5-6 are fallback-only: skipped when steps 1-4 produced at least one feature.
    """
    features: set[str] = set()

    # Step 1 — domain_features: unconditional features granted by domain
    domain_feats = rules.domain_features.get(entity.domain)
    if domain_feats:
        features.update(domain_feats)

    # Step 2 — attribute_presence: any matching attribute key grants its feature
    for attr_key, feature in rules.attribute_presence.items():
        if attr_key in entity.attributes:
            features.add(feature)

    # Step 3 — device_class_features: entity.device_class maps to a feature
    if entity.device_class and entity.device_class in rules.device_class_features:
        features.add(rules.device_class_features[entity.device_class])

    # Step 4 — domain_attr_features: domain-scoped attribute presence
    domain_attr_map = rules.domain_attr_features.get(entity.domain)
    if domain_attr_map:
        if isinstance(domain_attr_map, dict):
            # Dict form: {attr: feature}
            for attr_key, feature in domain_attr_map.items():
                if attr_key in entity.attributes:
                    features.add(feature)
        elif isinstance(domain_attr_map, list):
            # List form: [{"attr": ..., "feature": ..., "condition": ...}]
            for rule in domain_attr_map:
                attr_key = rule.get("attr")
                feature = rule.get("feature")
                condition = rule.get("condition")
                if not attr_key or not feature:
                    continue
                if attr_key not in entity.attributes:
                    continue
                if condition is not None and not _condition_holds(
                    condition, entity.attributes.get(attr_key)
                ):
                    continue
                features.add(feature)

    # Steps 5-6 are fallback-only — skip when 1-4 produced features
    if features:
        return features

    # Step 5 — unit_features: match entity.unit
    if entity.unit:
        for rule in rules.unit_features:
            if rule.get("unit") == entity.unit:
                feat = rule.get("feature")
                if feat:
                    features.add(feat)

    # Step 6 — state_type_features: match entity.state_type in types list
    if entity.state_type:
        for rule in rules.state_type_features:
            if entity.state_type in rule.get("types", []):
                feat = rule.get("feature")
                if feat:
                    features.add(feat)

    return features
