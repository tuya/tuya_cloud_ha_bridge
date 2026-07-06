"""Dataclass models for the PidSpec inference engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Rule containers
# ---------------------------------------------------------------------------

@dataclass
class FeatureRules:
    attribute_presence: dict[str, str] = field(default_factory=dict)
    device_class_features: dict[str, str] = field(default_factory=dict)
    domain_features: dict[str, list[str]] = field(default_factory=dict)
    domain_attr_features: dict[str, dict[str, str]] = field(default_factory=dict)
    unit_features: list[dict[str, str]] = field(default_factory=list)
    state_type_features: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ComponentRule:
    keywords: list[str]
    component: str


@dataclass
class LocalBaselineComponentRules:
    platform_signals: dict[str, dict[str, str]]
    channel_patterns: list[dict[str, str]]
    semantic_keywords: list[ComponentRule]


# ---------------------------------------------------------------------------
# Entity / device profiles
# ---------------------------------------------------------------------------

@dataclass
class EntityProfile:
    entity_id: str
    domain: str
    integration: str
    friendly_name: str
    original_name: str
    unique_id: str
    device_class: str | None
    state_class: str | None
    entity_category: str | None
    unit: str | None
    state_type: str
    readable: bool
    writable: bool
    notifiable: bool
    attributes: dict[str, Any]
    ha_supported_features_int: int | None
    supported_features: set[str]
    component: str
    component_source: str


@dataclass
class DeviceProfile:
    device_id: str
    name: str
    model: str
    manufacturer: str
    entity_profiles: list[EntityProfile]
    domain_set: frozenset[str]


# ---------------------------------------------------------------------------
# Matching / disambiguation hints
# ---------------------------------------------------------------------------

@dataclass
class DomainFeatureReq:
    domain: str
    features: list[str]


@dataclass
class DisambiguateHints:
    name_keywords: list[str]
    positive_attrs: set[str]
    negative_attrs: set[str]
    weight: float = 0.0


@dataclass
class MatchHints:
    preferred_keywords: list[str]
    excluded_keywords: list[str]


# ---------------------------------------------------------------------------
# DP / PID specification
# ---------------------------------------------------------------------------

@dataclass
class CandidateTarget:
    domain: str
    converter: str
    features: list[str]
    converter_config: dict[str, Any]
    priority: int
    tuya_enum_values: list[str] | None = None


@dataclass
class DPDefinition:
    dpcode: str
    direction: str
    dp_type: str
    sub_type: str | None
    candidate_targets: list[CandidateTarget]
    component_hint: str | None = None
    match_hints: MatchHints | None = None


@dataclass
class PidSpec:
    product_id: str
    category_code: str
    required_domains: frozenset[str]
    optional_domains: frozenset[str]
    required_any_features: list[DomainFeatureReq]
    required_dps: list[str]
    optional_dps: list[str]
    dp_definitions: list[DPDefinition]
    disambiguate: DisambiguateHints | None = None
    # Domains that may exist on matched devices but should be transparently
    # ignored during scoring and domain-coverage checks.  Use this for domains
    # that are known to appear on devices of this category (e.g. a "switch"
    # entity for an indicator light on a curtain motor) but have no Tuya DP
    # mapping and should not penalise the match.
    ignore_domains: frozenset[str] = frozenset()

    # Minimum fraction of the device's domain_set that must be covered by the
    # spec's handled domains (required + optional). Below this ratio the spec
    # is a poor description of the device regardless of score — demoted to
    # low_confidence (insufficient_domain_coverage).
    # Default 0.5: a single extra domain (e.g. sensor) on a 2-domain device
    # sits exactly at the boundary and passes; 2+ extra domains (e.g. switch
    # + select on a curtain motor) push coverage below 50% and are caught.
    # Spec authors can adjust per spec, or add extra domains to optional_domains
    # with DP definitions to increase handled coverage.
    min_domain_coverage: float = 0.5


# ---------------------------------------------------------------------------
# Route / binding records
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class DPRoute:
    dpcode: str
    entity_id: str
    domain: str
    direction: str
    dp_type: str
    sub_type: str | None
    converter: str
    # converter_config drives BOTH runtime conversion (ha_attr/service/value_map/
    # …) AND bind-time defaultProperties declaration (range_attr/fallback/
    # min_attr/max_attr/step/unit/tuya_contract). Keeping the thing-model
    # declaration here means it always follows the carrier that won routing.
    converter_config: dict[str, Any]
    component: str
    category_code: str


@dataclass(slots=True)
class DeviceRouteTable:
    ha_device_id: str
    tuya_device_id: str
    product_id: str
    category_code: str
    primary_entity_id: str
    entity_ids: set[str]
    routes: list[DPRoute]
    dpcode_to_route: dict[str, DPRoute]
    entity_to_routes: dict[str, list[DPRoute]]
    last_reported_snapshot: dict[str, Any]
    # DPcodes REQUIRED by the matched spec. The full-report builder (b2) defers
    # the whole report only when a REQUIRED DP's entity is unreportable; an
    # unknown OPTIONAL DP entity is emitted as a type default instead of
    # freezing the whole device. Defaults to empty → callers that don't populate
    # it get the conservative legacy "defer on any unknown" behaviour.
    required_dpcodes: frozenset[str] = frozenset()


@dataclass
class BoundDeviceRoute:
    ha_device_id: str
    tuya_device_id: str
    product_id: str
    category_code: str
    confidence: float
    routes: list[DPRoute]
    bind_time: int
    route_version: str
    rule_version: int


# ---------------------------------------------------------------------------
# Inference results
# ---------------------------------------------------------------------------

@dataclass
class PidInferResult:
    status: str
    product_id: str | None = None
    reason: str | None = None
    candidates: list[tuple[PidSpec, float]] = field(default_factory=list)
    low_confidence_signal: str | None = None

    # ----- diagnostics (populated for low-confidence cases) -----
    # These mirror the cloud Skill's DeviceReport contract so the upload can be
    # consumed by decision.determine_action_hint without fabrication:
    #   - margin:    top1 − top2 score gap (margin_tight routing).
    #   - threshold: the gate that was applied (CONFIDENCE_THRESHOLD / MARGIN_THRESHOLD).
    #   - hard_filter_passed:   product_ids that survived the hard filter.
    #   - hard_filter_rejected: [{"product_id", "reason"}] with reason in
    #       {domain_missing, feature_mismatch, dp_unassignable}.
    #   - scoring_breakdown: {pid: {"total", "items":[{factor,score}],
    #       "deductions":[{reason, dps?/domains?/...}]}}; deduction reasons the
    #       engine can emit: missing_optional_dp, extra_domain, domain_coverage_low.
    #   - match_details: {pid: {"note": str, ...}} short human-readable scoring note.
    margin: float | None = None
    threshold: float | None = None
    hard_filter_passed: list[str] = field(default_factory=list)
    hard_filter_rejected: list[dict[str, Any]] = field(default_factory=list)
    scoring_breakdown: dict[str, dict[str, Any]] = field(default_factory=dict)
    match_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    # For carrier/route assignment failures (demoted-after-match): structured
    # detail mapped to the Skill's AssignmentFailure. None for pure inference
    # low-confidence cases.
    assignment_failure: dict[str, Any] | None = None


@dataclass
class DPResolveResult:
    matched: bool
    entity_id: str | None
    domain: str | None
    converter: str | None
    converter_config: dict[str, Any]
    component: str | None
    dp_type: str | None
    sub_type: str | None
