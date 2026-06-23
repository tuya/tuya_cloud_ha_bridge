"""Wire-format bundle parser: converts cloud GET /rules/bundle JSON to internal models.

Wire shape (cloud API):
  {
    "rule_version": int,
    "pidspecs": [ { "product_id": ..., "dp_definitions": [...], "candidate_targets": {...}, ... } ],
    "feature_rules": { "attribute_presence": {}, "device_class_features": {}, ... },
    "component_rules": [ {"keywords": [...], "component": "..."} ]
  }

dp_definitions items use:
  - "rw":  "rw" | "ro" | "wo"   (or "direction" key for Skill output)
  - "type": "bool" | "value" | "enum" | "string"  (or capitalised Skill variants)
  - "sub_type": "json_struct" | null | absent

Skill output uses capitalised type names (Boolean, Integer, Enum, String) and direction
aliases (report_only, bidirectional, …) which are normalised before model construction.
"""

from __future__ import annotations

from typing import Any

from ..const import LOGGER
from .models import (
    CandidateTarget,
    ComponentRule,
    DisambiguateHints,
    DomainFeatureReq,
    DPDefinition,
    FeatureRules,
    MatchHints,
    PidSpec,
)

# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

_RW_TO_DIRECTION: dict[str, str] = {
    "rw": "bidirectional",
    "ro": "read_only",
    "wo": "write_only",
}

_VALID_DP_TYPES: frozenset[str] = frozenset({"bool", "value", "enum", "string"})

# Normalise Skill-output type names to canonical lowercase wire names.
# Also handles any stray aliases that may arrive from skill generation.
_SKILL_TYPE_MAP: dict[str, str] = {
    # Capitalised skill variants
    "Boolean": "bool",
    "boolean": "bool",
    "Integer": "value",
    "integer": "value",
    "Enum": "enum",
    "String": "string",
    # Direction aliases used in Skill output
    "report_only": "read_only",
    "bidirectional": "bidirectional",
    "read_only": "read_only",
    "write_only": "write_only",
    # Already-canonical types (identity, kept for completeness)
    "bool": "bool",
    "value": "value",
    "enum": "enum",
    "string": "string",
}

# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _normalise_skill_type(raw: str) -> str:
    """Return the canonical lowercase dp_type for *raw*, applying Skill aliases."""
    return _SKILL_TYPE_MAP.get(raw, raw.lower())


def _parse_direction(raw_rw: str, dpcode: str) -> str:
    """Convert a wire ``rw`` value or Skill direction alias to a direction string.

    Raises ``ValueError`` for unrecognised values.
    """
    # First try direct rw mapping
    if raw_rw in _RW_TO_DIRECTION:
        return _RW_TO_DIRECTION[raw_rw]
    # Then try skill direction aliases (already canonical or aliased)
    normalised = _SKILL_TYPE_MAP.get(raw_rw, raw_rw.lower())
    if normalised in {"bidirectional", "read_only", "write_only"}:
        return normalised
    raise ValueError(
        f"dp '{dpcode}': unrecognised rw/direction value '{raw_rw}'"
    )


def _parse_dp_type(raw_type: str, dpcode: str) -> str:
    """Normalise a raw type string to a canonical dp_type.

    Raises ``ValueError`` if the result is not in ``_VALID_DP_TYPES``.
    """
    normalised = _normalise_skill_type(raw_type)
    if normalised not in _VALID_DP_TYPES:
        raise ValueError(
            f"dp '{dpcode}': unrecognised dp_type '{raw_type}' (normalised: '{normalised}')"
        )
    return normalised


# ---------------------------------------------------------------------------
# CandidateTarget
# ---------------------------------------------------------------------------


def _parse_candidate_target(raw: dict[str, Any]) -> CandidateTarget:
    """Parse a single candidate-target dict from wire format.

    Handles the ``"param"`` → ``"ha_attr"`` alias inside ``converter_config``.
    """
    converter_config: dict[str, Any] = dict(raw.get("converter_config") or {})
    # Normalise "param" alias to "ha_attr"
    if "param" in converter_config and "ha_attr" not in converter_config:
        converter_config["ha_attr"] = converter_config.pop("param")

    return CandidateTarget(
        domain=raw["domain"],
        converter=raw["converter"],
        features=list(raw.get("features") or []),
        converter_config=converter_config,
        priority=int(raw.get("priority", 100)),
        tuya_enum_values=raw.get("tuya_enum_values"),
    )


# ---------------------------------------------------------------------------
# DPDefinition
# ---------------------------------------------------------------------------


def _parse_match_hints(raw: dict[str, Any]) -> MatchHints:
    return MatchHints(
        preferred_keywords=list(raw.get("preferred_keywords") or []),
        excluded_keywords=list(raw.get("excluded_keywords") or []),
    )


def _parse_dp_definition(
    dpcode: str,
    raw_dp: dict[str, Any],
    candidate_targets_raw: list[dict[str, Any]],
) -> DPDefinition:
    """Parse a single dp_definition entry.

    Direction preference:
      1. ``"direction"`` key (Skill output)
      2. ``"rw"`` key (cloud wire format)
    """
    # --- direction ---
    if "direction" in raw_dp:
        direction = _parse_direction(raw_dp["direction"], dpcode)
    elif "rw" in raw_dp:
        direction = _parse_direction(raw_dp["rw"], dpcode)
    else:
        raise ValueError(f"dp '{dpcode}': missing both 'direction' and 'rw' keys")

    # --- dp_type ---
    dp_type = _parse_dp_type(raw_dp["type"], dpcode)

    # --- sub_type ---
    sub_type: str | None = raw_dp.get("sub_type") or None

    # --- candidate_targets ---
    targets: list[CandidateTarget] = []
    for ct_raw in candidate_targets_raw:
        try:
            targets.append(_parse_candidate_target(ct_raw))
        except (KeyError, TypeError, ValueError) as exc:
            LOGGER.warning(
                "bundle: skipping malformed candidate_target for dp '%s': %s",
                dpcode,
                exc,
            )

    # --- optional fields ---
    component_hint: str | None = raw_dp.get("component_hint")

    match_hints: MatchHints | None = None
    if "match_hints" in raw_dp and raw_dp["match_hints"]:
        try:
            match_hints = _parse_match_hints(raw_dp["match_hints"])
        except (KeyError, TypeError) as exc:
            LOGGER.warning(
                "bundle: ignoring malformed match_hints for dp '%s': %s", dpcode, exc
            )

    return DPDefinition(
        dpcode=dpcode,
        direction=direction,
        dp_type=dp_type,
        sub_type=sub_type,
        candidate_targets=targets,
        component_hint=component_hint,
        match_hints=match_hints,
    )


# ---------------------------------------------------------------------------
# DisambiguateHints
# ---------------------------------------------------------------------------


def _parse_disambiguate(raw: dict[str, Any]) -> DisambiguateHints | None:
    """Parse a ``disambiguate`` block; returns ``None`` if the block is empty/absent."""
    if not raw:
        return None
    return DisambiguateHints(
        name_keywords=list(raw.get("name_keywords") or []),
        positive_attrs=set(raw.get("positive_attrs") or []),
        negative_attrs=set(raw.get("negative_attrs") or []),
        weight=float(raw.get("weight", 0.0)),
    )


# ---------------------------------------------------------------------------
# PidSpec
# ---------------------------------------------------------------------------


def _parse_pidspec(raw: dict[str, Any]) -> PidSpec:
    """Parse a single pidspec dict from wire format.

    Wire shape:
      dp_definitions  — list of {dpcode, type, rw, ...}
      candidate_targets — dict keyed by dpcode, value is list of target dicts
    """
    product_id: str = raw["product_id"]
    category_code: str = raw["category_code"]

    candidate_targets_by_dp: dict[str, list[dict[str, Any]]] = (
        raw.get("candidate_targets") or {}
    )

    dp_definitions: list[DPDefinition] = []
    for dp_raw in raw.get("dp_definitions") or []:
        dpcode = dp_raw.get("dpcode")
        if not dpcode:
            LOGGER.warning(
                "bundle: pidspec '%s': dp_definition missing dpcode, skipping", product_id
            )
            continue
        try:
            ct_raw_list = candidate_targets_by_dp.get(dpcode) or []
            dp_def = _parse_dp_definition(dpcode, dp_raw, ct_raw_list)
            dp_definitions.append(dp_def)
        except (KeyError, TypeError, ValueError) as exc:
            LOGGER.warning(
                "bundle: pidspec '%s': skipping dp '%s': %s", product_id, dpcode, exc
            )

    required_any_features: list[DomainFeatureReq] = [
        DomainFeatureReq(
            domain=f["domain"],
            features=list(f.get("features") or []),
        )
        for f in (raw.get("required_any_features") or [])
    ]

    disambiguate: DisambiguateHints | None = None
    if raw.get("disambiguate"):
        try:
            disambiguate = _parse_disambiguate(raw["disambiguate"])
        except (KeyError, TypeError) as exc:
            LOGGER.warning(
                "bundle: pidspec '%s': ignoring malformed disambiguate: %s",
                product_id,
                exc,
            )

    return PidSpec(
        product_id=product_id,
        category_code=category_code,
        required_domains=frozenset(raw.get("required_domains") or []),
        optional_domains=frozenset(raw.get("optional_domains") or []),
        required_any_features=required_any_features,
        required_dps=list(raw.get("required_dps") or []),
        optional_dps=list(raw.get("optional_dps") or []),
        dp_definitions=dp_definitions,
        disambiguate=disambiguate,
        ignore_domains=frozenset(raw.get("ignore_domains") or []),
        min_domain_coverage=float(raw.get("min_domain_coverage", 0.5)),
    )


# ---------------------------------------------------------------------------
# FeatureRules
# ---------------------------------------------------------------------------


def _parse_feature_rules(raw: dict[str, Any]) -> FeatureRules:
    """Parse feature_rules block (cloud shape — no unit/state_type fields)."""
    return FeatureRules(
        attribute_presence=dict(raw.get("attribute_presence") or {}),
        device_class_features=dict(raw.get("device_class_features") or {}),
        domain_features={
            k: list(v)
            for k, v in (raw.get("domain_features") or {}).items()
        },
        domain_attr_features={
            k: dict(v)
            for k, v in (raw.get("domain_attr_features") or {}).items()
        },
        unit_features=list(raw.get("unit_features") or []),
        state_type_features=list(raw.get("state_type_features") or []),
    )


# ---------------------------------------------------------------------------
# ComponentRule
# ---------------------------------------------------------------------------


def _parse_component_rules(raw: list[dict[str, Any]]) -> list[ComponentRule]:
    """Parse the component_rules list."""
    rules: list[ComponentRule] = []
    for entry in raw:
        try:
            rules.append(
                ComponentRule(
                    keywords=list(entry["keywords"]),
                    component=entry["component"],
                )
            )
        except (KeyError, TypeError) as exc:
            LOGGER.warning("bundle: skipping malformed component_rule: %s", exc)
    return rules


# ---------------------------------------------------------------------------
# Anti-downgrade error
# ---------------------------------------------------------------------------


class BundleDowngradeError(ValueError):
    """Raised when the incoming bundle has a lower rule_version than the current one.

    This prevents accidental rollback to an older ruleset.
    """


# ---------------------------------------------------------------------------
# Top-level parse / serialise
# ---------------------------------------------------------------------------


def parse_bundle(
    raw: dict[str, Any],
    current_rule_version: int = 0,
) -> tuple[list[PidSpec], FeatureRules, list[ComponentRule], int]:
    """Parse a full wire-format bundle dict.

    Args:
        raw: Parsed JSON dict from the cloud GET /rules/bundle response.
        current_rule_version: The rule version currently in use; used for the
            anti-downgrade check.

    Returns:
        A 4-tuple of ``(pidspecs, feature_rules, component_rules, rule_version)``.

    Raises:
        ValueError: If required top-level fields are missing.
        BundleDowngradeError: If the bundle's rule_version is lower than
            ``current_rule_version``.
    """
    # --- required fields ---
    missing = [
        k
        for k in ("rule_version", "pidspecs", "feature_rules", "component_rules")
        if k not in raw
    ]
    if missing:
        raise ValueError(f"bundle: missing required top-level fields: {missing}")

    rule_version = int(raw["rule_version"])

    # --- anti-downgrade check ---
    if rule_version < current_rule_version:
        raise BundleDowngradeError(
            f"bundle: incoming rule_version {rule_version} is lower than "
            f"current {current_rule_version}; refusing to downgrade"
        )

    # --- pidspecs ---
    pidspecs: list[PidSpec] = []
    for i, pid_raw in enumerate(raw.get("pidspecs") or []):
        try:
            pidspecs.append(_parse_pidspec(pid_raw))
        except (KeyError, TypeError, ValueError) as exc:
            product_id = pid_raw.get("product_id", f"<index {i}>") if isinstance(pid_raw, dict) else f"<index {i}>"
            LOGGER.warning(
                "bundle: skipping malformed pidspec '%s': %s", product_id, exc
            )

    # --- feature_rules ---
    try:
        feature_rules = _parse_feature_rules(raw["feature_rules"])
    except (KeyError, TypeError) as exc:
        LOGGER.warning(
            "bundle: feature_rules malformed, using empty defaults: %s", exc
        )
        feature_rules = FeatureRules(
            attribute_presence={},
            device_class_features={},
            domain_features={},
            domain_attr_features={},
        )

    # --- component_rules ---
    component_rules = _parse_component_rules(raw.get("component_rules") or [])

    LOGGER.debug(
        "bundle: parsed rule_version=%d  pidspecs=%d  component_rules=%d",
        rule_version,
        len(pidspecs),
        len(component_rules),
    )

    return pidspecs, feature_rules, component_rules, rule_version


# ---------------------------------------------------------------------------
# Serialisation (internal model → wire format)
# ---------------------------------------------------------------------------

_DIRECTION_TO_RW: dict[str, str] = {v: k for k, v in _RW_TO_DIRECTION.items()}


def _direction_to_rw(direction: str) -> str:
    """Convert an internal direction string back to the wire ``rw`` value."""
    return _DIRECTION_TO_RW.get(direction, direction)


def _serialise_candidate_target(ct: CandidateTarget) -> dict[str, Any]:
    out: dict[str, Any] = {
        "domain": ct.domain,
        "converter": ct.converter,
        "features": list(ct.features),
        "converter_config": dict(ct.converter_config),
        "priority": ct.priority,
    }
    if ct.tuya_enum_values is not None:
        out["tuya_enum_values"] = list(ct.tuya_enum_values)
    return out


def _serialise_dp_definition(
    dp: DPDefinition,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``(dp_def_dict, candidate_targets_list)`` for wire format."""
    dp_dict: dict[str, Any] = {
        "dpcode": dp.dpcode,
        "type": dp.dp_type,
        "rw": _direction_to_rw(dp.direction),
    }
    if dp.sub_type is not None:
        dp_dict["sub_type"] = dp.sub_type
    if dp.component_hint is not None:
        dp_dict["component_hint"] = dp.component_hint
    if dp.match_hints is not None:
        dp_dict["match_hints"] = {
            "preferred_keywords": list(dp.match_hints.preferred_keywords),
            "excluded_keywords": list(dp.match_hints.excluded_keywords),
        }

    ct_list = [_serialise_candidate_target(ct) for ct in dp.candidate_targets]
    return dp_dict, ct_list


def _serialise_disambiguate(d: DisambiguateHints) -> dict[str, Any]:
    return {
        "name_keywords": list(d.name_keywords),
        "positive_attrs": sorted(d.positive_attrs),
        "negative_attrs": sorted(d.negative_attrs),
        "weight": d.weight,
    }


def serialise_pidspec(spec: PidSpec) -> dict[str, Any]:
    """Serialise a ``PidSpec`` back to wire-format dict."""
    dp_definitions: list[dict[str, Any]] = []
    candidate_targets: dict[str, list[dict[str, Any]]] = {}

    for dp in spec.dp_definitions:
        dp_dict, ct_list = _serialise_dp_definition(dp)
        dp_definitions.append(dp_dict)
        candidate_targets[dp.dpcode] = ct_list

    out: dict[str, Any] = {
        "product_id": spec.product_id,
        "category_code": spec.category_code,
        "required_domains": sorted(spec.required_domains),
        "optional_domains": sorted(spec.optional_domains),
        "required_any_features": [
            {"domain": f.domain, "features": list(f.features)}
            for f in spec.required_any_features
        ],
        "required_dps": list(spec.required_dps),
        "optional_dps": list(spec.optional_dps),
        "dp_definitions": dp_definitions,
        "candidate_targets": candidate_targets,
    }

    if spec.disambiguate is not None:
        out["disambiguate"] = _serialise_disambiguate(spec.disambiguate)
    if spec.min_domain_coverage != 0.5:
        out["min_domain_coverage"] = spec.min_domain_coverage
    if spec.ignore_domains:
        out["ignore_domains"] = sorted(spec.ignore_domains)

    return out


def serialise_bundle(
    pidspecs: list[PidSpec],
    feature_rules: FeatureRules,
    component_rules: list[ComponentRule],
    rule_version: int,
) -> dict[str, Any]:
    """Serialise a full bundle back to wire-format dict."""
    return {
        "rule_version": rule_version,
        "pidspecs": [serialise_pidspec(s) for s in pidspecs],
        "feature_rules": {
            "attribute_presence": dict(feature_rules.attribute_presence),
            "device_class_features": dict(feature_rules.device_class_features),
            "domain_features": {
                k: list(v) for k, v in feature_rules.domain_features.items()
            },
            "domain_attr_features": {
                k: dict(v) for k, v in feature_rules.domain_attr_features.items()
            },
            "unit_features": list(feature_rules.unit_features),
            "state_type_features": list(feature_rules.state_type_features),
        },
        "component_rules": [
            {"keywords": list(r.keywords), "component": r.component}
            for r in component_rules
        ],
    }
