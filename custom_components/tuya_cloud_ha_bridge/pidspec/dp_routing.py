"""DP-to-entity resolution (routing doc §11).

Public API:
- resolve_dp_routes: resolve all DPs in a PidSpec to entity routes
- build_device_route_table: assemble a DeviceRouteTable from resolved routes
"""

from __future__ import annotations

from dataclasses import replace

from .models import (
    CandidateTarget,
    DPDefinition,
    DPResolveResult,
    DPRoute,
    DeviceProfile,
    DeviceRouteTable,
    EntityProfile,
    PidSpec,
)
from .service_capability import prune_value_map


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_dp_routes(
    device_profile: DeviceProfile,
    spec: PidSpec,
) -> tuple[list[DPRoute], list[dict]]:
    """Resolve all DPs in *spec* to entity routes against *device_profile*.

    Returns:
        (routes, diagnostics) where diagnostics is a list of dicts with keys:
        dpcode, status ("ok" | "unmatched" | "missing_definition"), reason.
    """
    routes: list[DPRoute] = []
    diagnostics: list[dict] = []

    # Track which entity_id is claimed by which component_hint so that the
    # component mutex is respected: an entity may only be shared across DPs
    # when they carry the same component_hint (or both have no hint).
    assigned: dict[str, str | None] = {}  # entity_id → component_hint

    # Process required DPs first so they get priority in entity assignment.
    ordered_dps: list[tuple[str, bool]] = [
        (dpcode, True) for dpcode in spec.required_dps
    ] + [
        (dpcode, False) for dpcode in spec.optional_dps
    ]

    for dpcode, _required in ordered_dps:
        # Find the DPDefinition inside the spec.
        dp_def = _find_dp_definition(spec, dpcode)
        if dp_def is None:
            diagnostics.append(
                {
                    "dpcode": dpcode,
                    "status": "missing_definition",
                    "reason": "no_dp_definition_in_spec",
                }
            )
            continue

        resolve_result = _resolve_single_dp(device_profile, dp_def, assigned)

        if not resolve_result.matched:
            diagnostics.append(
                {
                    "dpcode": dpcode,
                    "status": "unmatched",
                    "reason": "no_suitable_entity_found",
                }
            )
            continue

        # Find the winning CandidateTarget so we can build the route.
        winning_target = _find_target_for_result(dp_def, resolve_result)
        route = _build_dp_route(dp_def, winning_target, resolve_result, spec)
        routes.append(route)

        # Commit the assignment.
        assigned[resolve_result.entity_id] = dp_def.component_hint

        diagnostics.append(
            {
                "dpcode": dpcode,
                "status": "ok",
                "reason": "matched",
                "entity_id": resolve_result.entity_id,
            }
        )

    return routes, diagnostics


def build_device_route_table(
    device_profile: DeviceProfile,
    spec: PidSpec,
    tuya_device_id: str,
    routes: list[DPRoute],
) -> DeviceRouteTable:
    """Assemble a DeviceRouteTable from a resolved list of DPRoutes.

    The primary_entity_id is chosen as the entity_id of the first route,
    falling back to the first entity in device_profile.entity_profiles.
    """
    # Bind-time capability trimming (method B): drop value_map commands the
    # bound entity does not support, so only executable commands are advertised
    # to the Tuya cloud. Uses the per-entity supported_features snapshot
    # captured in the profile at bind time — no live lookup needed.
    feats_by_entity = {
        ep.entity_id: ep.ha_supported_features_int
        for ep in device_profile.entity_profiles
    }
    routes = [_trim_route_value_map(route, feats_by_entity) for route in routes]

    dpcode_to_route: dict[str, DPRoute] = {}
    entity_to_routes: dict[str, list[DPRoute]] = {}
    entity_ids: set[str] = set()

    for route in routes:
        dpcode_to_route[route.dpcode] = route
        entity_to_routes.setdefault(route.entity_id, []).append(route)
        entity_ids.add(route.entity_id)

    if routes:
        primary_entity_id = routes[0].entity_id
    elif device_profile.entity_profiles:
        primary_entity_id = device_profile.entity_profiles[0].entity_id
    else:
        primary_entity_id = ""

    return DeviceRouteTable(
        ha_device_id=device_profile.device_id,
        tuya_device_id=tuya_device_id,
        product_id=spec.product_id,
        category_code=spec.category_code,
        primary_entity_id=primary_entity_id,
        entity_ids=entity_ids,
        routes=routes,
        dpcode_to_route=dpcode_to_route,
        entity_to_routes=entity_to_routes,
        last_reported_snapshot={},
        required_dpcodes=frozenset(spec.required_dps),
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _trim_route_value_map(
    route: DPRoute,
    feats_by_entity: dict[str, int | None],
) -> DPRoute:
    """Return *route* with unsupported ``value_map`` commands removed.

    Only routes whose ``converter_config`` carries a ``value_map`` are affected
    (``std:value_to_service``). The shared ``converter_config`` dict from the
    cached PidSpec is never mutated — a NEW dict is built when trimming applies,
    so other devices binding the same spec are unaffected. Routes with nothing
    to trim are returned unchanged.
    """
    value_map = route.converter_config.get("value_map")
    if not isinstance(value_map, dict) or not value_map:
        return route

    pruned = prune_value_map(value_map, feats_by_entity.get(route.entity_id))
    if pruned is value_map:
        return route  # guard hit or nothing dropped — keep shared dict as-is

    new_config = {**route.converter_config, "value_map": pruned}
    return replace(route, converter_config=new_config)


def _resolve_single_dp(
    device_profile: DeviceProfile,
    dp_def: DPDefinition,
    assigned: dict[str, str | None],
) -> DPResolveResult:
    """Attempt to match *dp_def* to an entity in *device_profile*.

    Candidate targets are evaluated in descending priority order.  For each
    target the entities are first filtered by domain / direction / features /
    component_hint, then ranked by match_hints scoring.  The first entity that
    does not violate the component mutex wins.

    Returns a DPResolveResult with matched=False when no entity qualifies.
    """
    # Sort candidate targets by priority descending.
    sorted_targets: list[CandidateTarget] = sorted(
        dp_def.candidate_targets, key=lambda t: t.priority, reverse=True
    )

    for target in sorted_targets:
        # Collect all entities that structurally fit this target.
        candidates = _filter_entities(device_profile, dp_def, target)
        if not candidates:
            continue

        # Apply match_hints scoring if present. For a featureless target
        # (features == []) the hints are the ONLY discriminator, so a preferred
        # keyword match is made mandatory; feature-gated targets keep soft rank.
        if dp_def.match_hints is not None:
            candidates = _rank_by_match_hints(
                candidates,
                dp_def.match_hints,
                require_preferred=not target.features,
            )

        # Pick the first entity that respects the component mutex.
        for entity in candidates:
            if _check_mutex(entity.entity_id, dp_def.component_hint, assigned):
                return DPResolveResult(
                    matched=True,
                    entity_id=entity.entity_id,
                    domain=entity.domain,
                    converter=target.converter,
                    converter_config=target.converter_config,
                    component=entity.component,
                    dp_type=dp_def.dp_type,
                    sub_type=dp_def.sub_type,
                )

    return DPResolveResult(
        matched=False,
        entity_id=None,
        domain=None,
        converter=None,
        converter_config={},
        component=None,
        dp_type=dp_def.dp_type,
        sub_type=dp_def.sub_type,
    )


def _build_dp_route(
    dp_def: DPDefinition,
    target: CandidateTarget | None,
    result: DPResolveResult,
    spec: PidSpec,
) -> DPRoute:
    """Construct a DPRoute from the resolved information."""
    return DPRoute(
        dpcode=dp_def.dpcode,
        entity_id=result.entity_id or "",
        domain=result.domain or "",
        direction=dp_def.direction,
        dp_type=dp_def.dp_type,
        sub_type=dp_def.sub_type,
        converter=result.converter or "",
        converter_config=result.converter_config,
        component=result.component or "",
        category_code=spec.category_code,
    )


def _filter_entities(
    device_profile: DeviceProfile,
    dp_def: DPDefinition,
    target: CandidateTarget,
) -> list[EntityProfile]:
    """Return entities from *device_profile* that satisfy the given target constraints."""
    required_features = set(target.features)
    result: list[EntityProfile] = []

    for entity in device_profile.entity_profiles:
        # Domain must match.
        if entity.domain != target.domain:
            continue

        # Direction compatibility.
        direction = dp_def.direction
        if direction in ("read_only", "bidirectional") and not entity.readable:
            continue
        if direction in ("write_only", "bidirectional") and not entity.writable:
            continue

        # Required features must all be present on the entity.
        if required_features and not required_features.issubset(
            entity.supported_features
        ):
            continue

        # component_hint: skip entities with entity_fallback source.
        if dp_def.component_hint is not None:
            if entity.component_source == "entity_fallback":
                continue
            if entity.component != dp_def.component_hint:
                continue

        result.append(entity)

    return result


def _rank_by_match_hints(
    entities: list[EntityProfile],
    match_hints,
    require_preferred: bool = False,
) -> list[EntityProfile]:
    """Re-order *entities* so those matching preferred_keywords come first.

    Entities whose search text contains any excluded_keyword are removed.

    When *require_preferred* is True and preferred_keywords are declared, an
    entity matching NONE of them is dropped entirely (not merely ranked last).
    Callers set this for featureless candidate targets (``features == []``) — a
    name match is then the only discriminator, so "no preferred hit" means "do
    not bind" instead of "bind to the first arbitrary same-domain entity".
    """
    preferred = [kw.lower() for kw in (match_hints.preferred_keywords or [])]
    excluded = [kw.lower() for kw in (match_hints.excluded_keywords or [])]

    filtered: list[EntityProfile] = []
    for entity in entities:
        search = " ".join(
            [entity.entity_id, entity.friendly_name, entity.original_name]
        ).lower()
        if any(kw in search for kw in excluded):
            continue
        filtered.append(entity)

    if not preferred:
        return filtered

    def _score(entity: EntityProfile) -> int:
        search = " ".join(
            [entity.entity_id, entity.friendly_name, entity.original_name]
        ).lower()
        return sum(1 for kw in preferred if kw in search)

    if require_preferred:
        # Featureless target: a preferred-keyword hit is mandatory. Drop blind
        # fall-back candidates so the DP stays UNMATCHED rather than binding to
        # an arbitrary same-domain entity (e.g. filter_life → a temperature
        # sensor on a purifier that exposes no filter entity).
        filtered = [entity for entity in filtered if _score(entity) > 0]

    filtered.sort(key=_score, reverse=True)
    return filtered


def _check_mutex(
    entity_id: str,
    component_hint: str | None,
    assigned: dict[str, str | None],
) -> bool:
    """Return True if assigning *entity_id* with *component_hint* is allowed.

    An entity already claimed by another DP may be reused only when both DPs
    share the same component_hint (i.e. they belong to the same component).
    """
    if entity_id not in assigned:
        return True
    return assigned[entity_id] == component_hint


def _find_dp_definition(spec: PidSpec, dpcode: str) -> DPDefinition | None:
    """Look up a DPDefinition by dpcode within *spec*."""
    for dp_def in spec.dp_definitions:
        if dp_def.dpcode == dpcode:
            return dp_def
    return None


def _find_target_for_result(
    dp_def: DPDefinition, result: DPResolveResult
) -> CandidateTarget | None:
    """Return the CandidateTarget whose domain and converter match the result."""
    for target in dp_def.candidate_targets:
        if target.domain == result.domain and target.converter == result.converter:
            return target
    return None
