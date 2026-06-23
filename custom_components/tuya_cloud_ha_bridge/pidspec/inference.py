"""PID inference engine — matches a DeviceProfile against all known PidSpecs."""

from __future__ import annotations

from typing import Any

from ..const import LOGGER
from .models import (
    DeviceProfile,
    DPDefinition,
    EntityProfile,
    PidInferResult,
    PidSpec,
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

CONFIDENCE_THRESHOLD = 10
MARGIN_THRESHOLD = 8

# Extra-domain penalty tuning. The base per-extra-domain penalty of -2 is
# applied in _score_pid_spec. When a device exposes EXTRA_DOMAIN_TRIGGER or
# more domains not covered by the spec (required + optional), each extra
# domain beyond the first further subtracts EXTRA_DOMAIN_PENALTY_PER_EXTRA.
# Rationale: a single stray domain can be a sensor/diagnostic the spec
# author didn't anticipate, but ≥2 extras (e.g. switch + select on a
# "curtain" device) is a strong signal the spec is a poor fit.
EXTRA_DOMAIN_TRIGGER = 2
EXTRA_DOMAIN_PENALTY_PER_EXTRA = 4


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def infer_pid(device_profile: DeviceProfile, all_specs: list[PidSpec]) -> PidInferResult:
    """Infer the best-matching PidSpec for a given DeviceProfile.

    Steps:
    1. Hard-filter specs that are structurally incompatible.
    2. Score each remaining candidate.
    3. Apply confidence / margin checks and return a PidInferResult.
    """
    total_specs = len(all_specs)
    device_name = device_profile.name or device_profile.device_id

    # 1. Hard filter
    candidates: list[PidSpec] = []
    filter_stats = {"no_domain": 0, "no_feature": 0, "no_dp_assign": 0}
    # Diagnostics: which specs passed / were rejected and why. Reasons align
    # with the cloud Skill's DeviceReport.infer_diagnostics.hard_filter_rejected
    # contract ({product_id, reason}) so decision routing can act on them.
    hard_filter_passed: list[str] = []
    hard_filter_rejected: list[dict[str, Any]] = []
    for spec in all_specs:
        # a) Required domains must all be present
        if not spec.required_domains.issubset(device_profile.domain_set):
            filter_stats["no_domain"] += 1
            hard_filter_rejected.append(
                {"product_id": spec.product_id, "reason": "domain_missing"}
            )
            continue

        # b) For each DomainFeatureReq the device must have at least one
        #    entity in that domain exposing at least one of the listed features.
        if not all(
            _device_has_any_feature(device_profile, req.domain, req.features)
            for req in spec.required_any_features
        ):
            filter_stats["no_feature"] += 1
            hard_filter_rejected.append(
                {"product_id": spec.product_id, "reason": "feature_mismatch"}
            )
            continue

        # c) Every required DP must be assignable
        if not _can_assign_all_required_dps(device_profile, spec):
            filter_stats["no_dp_assign"] += 1
            hard_filter_rejected.append(
                {"product_id": spec.product_id, "reason": "dp_unassignable"}
            )
            continue

        candidates.append(spec)
        hard_filter_passed.append(spec.product_id)

    LOGGER.debug(
        "infer_pid: device=%s domains=%s total_specs=%d candidates=%d "
        "(filtered: domain=%d feature=%d dp_assign=%d)",
        device_name, set(device_profile.domain_set), total_specs, len(candidates),
        filter_stats["no_domain"], filter_stats["no_feature"], filter_stats["no_dp_assign"],
    )

    # 2. No candidates
    if not candidates:
        LOGGER.info(
            "infer_pid: device=%s → no_candidate (filter: domain=%d feature=%d dp_assign=%d)",
            device_name,
            filter_stats["no_domain"], filter_stats["no_feature"], filter_stats["no_dp_assign"],
        )
        return PidInferResult(
            status="needs_cloud_upgrade",
            reason="no_candidate_passed_filter",
            low_confidence_signal="filter_empty",
            hard_filter_passed=hard_filter_passed,
            hard_filter_rejected=hard_filter_rejected,
        )

    # 3. Score (with per-spec breakdown for diagnostics)
    scored: list[tuple[PidSpec, float]] = []
    scoring_breakdown: dict[str, dict[str, Any]] = {}
    for spec in candidates:
        spec_score, breakdown = _score_pid_spec_detailed(device_profile, spec)
        scored.append((spec, spec_score))
        scoring_breakdown[spec.product_id] = breakdown
    scored.sort(key=lambda t: t[1], reverse=True)

    top_spec, top_score = scored[0]

    # Per-candidate human-readable scoring notes (top 5) + margin.
    match_details: dict[str, dict[str, Any]] = {
        spec.product_id: {"note": _scoring_note(scoring_breakdown[spec.product_id])}
        for spec, _ in scored[:5]
    }
    margin: float | None = (
        round(top_score - scored[1][1], 2) if len(scored) > 1 else None
    )

    LOGGER.debug(
        "infer_pid: device=%s top=%s score=%.1f (candidates=%d)",
        device_name, top_spec.product_id, top_score, len(scored),
    )

    # 4a. Score too low
    if top_score < CONFIDENCE_THRESHOLD:
        LOGGER.info(
            "infer_pid: device=%s → score_low top=%s",
            device_name, top_spec.product_id,
        )
        return PidInferResult(
            status="needs_cloud_upgrade",
            reason="score_below_threshold",
            candidates=scored,
            low_confidence_signal="score_low",
            margin=margin,
            threshold=float(CONFIDENCE_THRESHOLD),
            hard_filter_passed=hard_filter_passed,
            hard_filter_rejected=hard_filter_rejected,
            scoring_breakdown=scoring_breakdown,
            match_details=match_details,
        )

    # 4b. Margin between top-2 too narrow
    if len(scored) > 1:
        _, second_score = scored[1]
        if (top_score - second_score) < MARGIN_THRESHOLD:
            LOGGER.info(
                "infer_pid: device=%s → margin_tight top=%s second=%s",
                device_name, top_spec.product_id, scored[1][0].product_id,
            )
            return PidInferResult(
                status="needs_cloud_upgrade",
                reason="ambiguous_top_candidates",
                candidates=scored,
                low_confidence_signal="margin_tight",
                margin=margin,
                threshold=float(MARGIN_THRESHOLD),
                hard_filter_passed=hard_filter_passed,
                hard_filter_rejected=hard_filter_rejected,
                scoring_breakdown=scoring_breakdown,
                match_details=match_details,
            )

    # 4b'. Domain coverage sanity check. If the spec (required + optional
    # domains) covers less than spec.min_domain_coverage of the device's
    # domain_set, the spec is a poor description of the device — demote
    # regardless of score. This prevents keyword bonuses from single-handedly
    # promoting a spec whose domain coverage is clearly a mismatch (e.g. a
    # cover-only spec on a cover+switch+select curtain motor).
    # Domains listed in spec.ignore_domains are excluded from the denominator —
    # they are known to exist on the device but are transparently ignored.
    spec_handled = top_spec.required_domains | top_spec.optional_domains
    effective_device_domains = device_profile.domain_set - top_spec.ignore_domains
    domain_coverage = _compute_domain_coverage(effective_device_domains, spec_handled)
    if domain_coverage < top_spec.min_domain_coverage:
        LOGGER.info(
            "infer_pid: device=%s → insufficient_domain_coverage "
            "top=%s coverage=%.2f threshold=%.2f device_domains=%s spec_domains=%s",
            device_name, top_spec.product_id,
            domain_coverage, top_spec.min_domain_coverage,
            sorted(device_profile.domain_set),
            sorted(spec_handled),
        )
        # Record a deduction on the top spec so the Skill's score_low-style
        # routing (which keys on "domain_coverage_low") can also fire.
        scoring_breakdown.setdefault(top_spec.product_id, {}).setdefault(
            "deductions", []
        ).append(
            {
                "reason": "domain_coverage_low",
                "coverage": round(domain_coverage, 2),
                "threshold": round(top_spec.min_domain_coverage, 2),
                "extra_domains": sorted(effective_device_domains - spec_handled),
            }
        )
        return PidInferResult(
            status="needs_cloud_upgrade",
            reason="insufficient_domain_coverage",
            candidates=scored,
            low_confidence_signal=(
                f"insufficient_domain_coverage:"
                f"coverage={domain_coverage:.2f} threshold="
                f"{top_spec.min_domain_coverage:.2f}"
            ),
            margin=margin,
            threshold=round(top_spec.min_domain_coverage, 2),
            hard_filter_passed=hard_filter_passed,
            hard_filter_rejected=hard_filter_rejected,
            scoring_breakdown=scoring_breakdown,
            match_details=match_details,
        )

    # 4c. Confident match
    LOGGER.info(
        "infer_pid: device=%s → matched %s",
        device_name, top_spec.product_id,
    )
    return PidInferResult(
        status="matched",
        product_id=top_spec.product_id,
        candidates=scored,
        margin=margin,
        scoring_breakdown=scoring_breakdown,
        match_details=match_details,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _device_has_any_feature(
    device_profile: DeviceProfile,
    domain: str,
    features: list[str],
) -> bool:
    """Return True if at least one entity in *domain* exposes any of *features*."""
    feature_set = set(features)
    for entity in device_profile.entity_profiles:
        if entity.domain == domain and (entity.supported_features & feature_set):
            return True
    return False


def _has_any_carrier(device_profile: DeviceProfile, dp_def: DPDefinition) -> bool:
    """Return True if at least one entity can carry this DP."""
    return bool(_list_carrier_entities(device_profile, dp_def))


def _compute_domain_coverage(
    effective_device_domains: frozenset[str], spec_handled_domains: frozenset[str]
) -> float:
    """Return the fraction of *effective_device_domains* that is covered by
    *spec_handled_domains* (required + optional).

    ``effective_device_domains`` is the device's domain_set minus any domains
    the spec declares as ignore_domains (transparent to matching).

    Returns 1.0 when the device has no domains (vacuously satisfied).
    Used by the 4b' domain coverage sanity check.
    """
    if not effective_device_domains:
        return 1.0
    covered = len(effective_device_domains & spec_handled_domains)
    return covered / len(effective_device_domains)


def _list_carrier_entities(
    device_profile: DeviceProfile, dp_def: DPDefinition
) -> list[EntityProfile]:
    """Return every EntityProfile that can act as a carrier for *dp_def*."""
    result: list[EntityProfile] = []
    for target in dp_def.candidate_targets:
        required_features = set(target.features)
        for entity in device_profile.entity_profiles:
            # Domain must match
            if entity.domain != target.domain:
                continue

            # Direction compatibility
            direction = dp_def.direction
            if direction in ("read_only", "bidirectional") and not entity.readable:
                continue
            if direction in ("write_only", "bidirectional") and not entity.writable:
                continue

            # Required features must all be present
            if required_features and not required_features.issubset(
                entity.supported_features
            ):
                continue

            # component_hint check — skip entity_fallback sources
            if dp_def.component_hint is not None:
                if entity.component_source == "entity_fallback":
                    continue
                if entity.component != dp_def.component_hint:
                    continue

            if entity not in result:
                result.append(entity)

    return result


def _can_assign_all_required_dps(
    device_profile: DeviceProfile, spec: PidSpec
) -> bool:
    """Return True if every required DP can be uniquely assigned to an entity.

    Uses a backtracking search (most-constrained first) to verify that a
    valid assignment exists where entities are shared only when the
    component_hint is identical.
    """
    # Build list of (dpcode, [carrier_entities]) for required DPs only
    dp_candidates: list[tuple[str, list[EntityProfile], str | None]] = []
    for dpcode in spec.required_dps:
        dp_def = _find_dp_definition(spec, dpcode)
        if dp_def is None:
            # DP definition missing → cannot satisfy
            return False
        carriers = _list_carrier_entities(device_profile, dp_def)
        if not carriers:
            return False
        dp_candidates.append((dpcode, carriers, dp_def.component_hint))

    if not dp_candidates:
        return True

    # Sort most-constrained (fewest candidates) first
    dp_candidates.sort(key=lambda t: len(t[1]))

    # Backtrack: assignment maps entity_id → component_hint that claimed it.
    # Entities can be shared only if the component_hint is the same.
    assignment: dict[str, str | None] = {}  # entity_id → component_hint

    def backtrack(idx: int) -> bool:
        if idx == len(dp_candidates):
            return True
        _dpcode, carriers, hint = dp_candidates[idx]
        for entity in carriers:
            eid = entity.entity_id
            if eid in assignment:
                # Entity already claimed — allow only if hints match
                if assignment[eid] == hint:
                    if backtrack(idx + 1):
                        return True
            else:
                assignment[eid] = hint
                if backtrack(idx + 1):
                    return True
                del assignment[eid]
        return False

    return backtrack(0)


def _score_pid_spec(device_profile: DeviceProfile, spec: PidSpec) -> float:
    """Compute a numeric match score for *spec* against *device_profile*.

    Thin wrapper over :func:`_score_pid_spec_detailed` for callers that only
    need the scalar score.
    """
    return _score_pid_spec_detailed(device_profile, spec)[0]


def _score_pid_spec_detailed(
    device_profile: DeviceProfile, spec: PidSpec
) -> tuple[float, dict[str, Any]]:
    """Compute the match score plus a structured breakdown for diagnostics.

    Returns ``(score, breakdown)`` where ``breakdown`` is::

        {
          "total": <float>,
          "items": [{"factor": <str>, "score": <float>}, ...],
          "deductions": [{"reason": <str>, ...}, ...],
        }

    The breakdown feeds ``PidInferResult.scoring_breakdown`` and is consumed by
    the cloud Skill's ``determine_action_hint``. Deduction reasons emitted here:
    ``missing_optional_dp`` (with ``dps``) and ``extra_domain`` (with
    ``domains``). The scalar score is identical to the previous implementation.
    """
    score: float = 0.0
    items: list[dict[str, Any]] = []
    deductions: list[dict[str, Any]] = []

    # Domains that this spec explicitly ignores — excluded from scoring.
    effective_device_domains = device_profile.domain_set - spec.ignore_domains

    # 1. Optional DP coverage: up to 20 points
    if spec.optional_dps:
        present: list[str] = []
        missing: list[str] = []
        for dpcode in spec.optional_dps:
            dp_def = _find_dp_definition(spec, dpcode)
            if dp_def is not None and _has_any_carrier(device_profile, dp_def):
                present.append(dpcode)
            else:
                missing.append(dpcode)
        opt_dp_score = (len(present) / len(spec.optional_dps)) * 20
        score += opt_dp_score
        items.append({"factor": "optional_dp_coverage", "score": round(opt_dp_score, 2)})
        if missing:
            # Drives the Skill's score_low → derive-new/needs_human_review path.
            deductions.append({"reason": "missing_optional_dp", "dps": missing})

    # 2. Optional domain coverage: up to 10 points
    if spec.optional_domains:
        hit = sum(
            1
            for d in spec.optional_domains
            if d in effective_device_domains
        )
        opt_domain_score = (hit / len(spec.optional_domains)) * 10
        score += opt_domain_score
        items.append(
            {"factor": "optional_domain_coverage", "score": round(opt_domain_score, 2)}
        )

    # 3. Extra-domain penalty: -2 per domain not in required or optional
    known_domains = spec.required_domains | spec.optional_domains
    extra_domains = sorted(effective_device_domains - known_domains)
    base_extra_penalty = -2.0 * len(extra_domains)
    score += base_extra_penalty

    # 4. Keyword bonus: capped at 8
    name_lower = (device_profile.name + " " + device_profile.model).lower()
    keyword_bonus: float = 0.0
    if spec.category_code and spec.category_code.lower() in name_lower:
        keyword_bonus += 4
    if spec.product_id and spec.product_id.lower() in name_lower:
        keyword_bonus += 2
    capped_keyword = min(keyword_bonus, 8)
    score += capped_keyword
    if capped_keyword:
        items.append({"factor": "keyword_bonus", "score": round(capped_keyword, 2)})

    # 5. Disambiguate bonus
    disambiguate_score = _score_disambiguate(device_profile, spec)
    score += disambiguate_score
    if disambiguate_score:
        items.append({"factor": "disambiguate", "score": round(disambiguate_score, 2)})

    # 6. Extra-domain escalating penalty. The base -2 per extra domain is
    # applied in step 3 above. When the device exposes EXTRA_DOMAIN_TRIGGER
    # or more domains not covered by the spec (required + optional), each
    # domain beyond the first further subtracts EXTRA_DOMAIN_PENALTY_PER_EXTRA.
    # This stops disambiguate's keyword bonus (+10) from single-handedly promoting
    # a spec whose domain coverage is clearly a mismatch.
    extras = len(extra_domains)
    escalating_penalty = 0.0
    if extras >= EXTRA_DOMAIN_TRIGGER:
        escalating_penalty = -(extras - 1) * EXTRA_DOMAIN_PENALTY_PER_EXTRA
        score += escalating_penalty
    if extra_domains:
        deductions.append(
            {
                "reason": "extra_domain",
                "domains": extra_domains,
                "score": round(base_extra_penalty + escalating_penalty, 2),
            }
        )

    breakdown: dict[str, Any] = {
        "total": round(score, 2),
        "items": items,
        "deductions": deductions,
    }
    return score, breakdown


def _scoring_note(breakdown: dict[str, Any]) -> str:
    """Build a short human-readable note from a scoring breakdown.

    Example: ``"total=18.0; disambiguate +4.0; optional_dp_coverage +12.0;
    extra_domain -4.0 [select,switch]"``. Used for
    ``PidInferResult.match_details[pid].note``.
    """
    parts: list[str] = [f"total={breakdown.get('total', 0)}"]
    for item in breakdown.get("items", []):
        parts.append(f"{item['factor']} {item['score']:+g}")
    for ded in breakdown.get("deductions", []):
        reason = ded.get("reason", "")
        if reason == "missing_optional_dp":
            parts.append(f"missing_optional_dp [{','.join(ded.get('dps', []))}]")
        elif reason == "extra_domain":
            parts.append(
                f"extra_domain {ded.get('score', 0):+g} [{','.join(ded.get('domains', []))}]"
            )
        elif reason == "domain_coverage_low":
            parts.append(f"domain_coverage_low {ded.get('coverage')}")
        else:
            parts.append(reason)
    return "; ".join(parts)


def _score_disambiguate(device_profile: DeviceProfile, spec: PidSpec) -> float:
    """Return the disambiguation bonus/penalty for *spec*."""
    if spec.disambiguate is None:
        return 0.0

    hints = spec.disambiguate
    result: float = hints.weight

    # Name-keyword check — first hit only
    name_lower = (device_profile.name + " " + device_profile.model).lower()
    for kw in hints.name_keywords:
        if kw.lower() in name_lower:
            result += 10
            break

    # Collect all attributes from all entity profiles
    all_attrs: set[str] = set()
    for entity in device_profile.entity_profiles:
        all_attrs.update(entity.attributes.keys())

    # Positive attrs: +2 each
    for attr in hints.positive_attrs:
        if attr in all_attrs:
            result += 2

    # Negative attrs: -4 each
    for attr in hints.negative_attrs:
        if attr in all_attrs:
            result -= 4

    return result


def _find_dp_definition(spec: PidSpec, dpcode: str) -> DPDefinition | None:
    """Look up a DPDefinition by dpcode within a PidSpec."""
    for dp_def in spec.dp_definitions:
        if dp_def.dpcode == dpcode:
            return dp_def
    return None
