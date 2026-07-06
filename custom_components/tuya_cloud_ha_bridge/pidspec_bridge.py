"""PidSpec bridge — drop-in replacements for mapping_runtime functions.

Provides pidspec-driven alternatives to:
- build_service_calls_from_tuya → pidspec_build_service_calls
- build_tuya_properties_from_state → pidspec_build_properties

Also handles initialization and route table management.

Usage in async_setup_entry:
    from .pidspec_bridge import async_init_pidspec, pidspec_build_service_calls, pidspec_build_properties
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.entity import get_capability

from .const import DOMAIN, LOGGER
from .pidspec.dispatcher import dispatch_ha_to_tuya, dispatch_tuya_to_ha
from .pidspec.dp_routing import resolve_dp_routes, build_device_route_table
from .pidspec.models import DeviceProfile, DeviceRouteTable, DPRoute, PidInferResult, PidSpec
from .pidspec.orchestrator import (
    async_infer_with_refresh,
    build_low_confidence_report_payload,
    InferenceResults,
)
from .pidspec.rule_cache import LocalRuleCache


# ---------------------------------------------------------------------------
# Domain-level state (stored in hass.data[DOMAIN])
# ---------------------------------------------------------------------------

_PIDSPEC_RULE_CACHE = "pidspec_rule_cache"
_PIDSPEC_ROUTE_TABLES = "pidspec_route_tables"
_PIDSPEC_CLOUD_CLIENT = "pidspec_cloud_client"


def _get_domain_data(hass: HomeAssistant) -> dict[str, Any]:
    return hass.data.setdefault(DOMAIN, {})


def get_rule_cache(hass: HomeAssistant) -> LocalRuleCache | None:
    """Return the shared rule cache, or None if not initialized."""
    return _get_domain_data(hass).get(_PIDSPEC_RULE_CACHE)


def get_route_table(
    hass: HomeAssistant, tuya_device_id: str
) -> DeviceRouteTable | None:
    """Return the route table for a bound Tuya device, or None."""
    tables: dict[str, DeviceRouteTable] = _get_domain_data(hass).get(
        _PIDSPEC_ROUTE_TABLES, {}
    )
    return tables.get(tuya_device_id)


def store_route_table(
    hass: HomeAssistant, tuya_device_id: str, route_table: DeviceRouteTable
) -> None:
    """Store a route table for a bound Tuya device."""
    domain_data = _get_domain_data(hass)
    tables: dict[str, DeviceRouteTable] = domain_data.setdefault(
        _PIDSPEC_ROUTE_TABLES, {}
    )
    tables[tuya_device_id] = route_table


def remove_route_table(hass: HomeAssistant, tuya_device_id: str) -> None:
    """Remove a route table when a device is unbound."""
    tables: dict[str, DeviceRouteTable] = _get_domain_data(hass).get(
        _PIDSPEC_ROUTE_TABLES, {}
    )
    tables.pop(tuya_device_id, None)


def find_route_table_by_entity(
    hass: HomeAssistant, entity_id: str
) -> DeviceRouteTable | None:
    """Return the route table that binds *entity_id*, or None.

    Reverse lookup across all stored route tables (keyed by tuya_device_id).
    The pidspec route table is the single source of truth for what a device
    binds, so the state-change → report trigger resolves an entity to its Tuya
    device THROUGH this — not the legacy gateway ``bindings["entities"]`` map,
    which is filled by a separate path and can drift out of sync (e.g. after a
    topo/get restore that picked a different entity among duplicate entities),
    silently dropping a bound entity's changes.
    """
    tables: dict[str, DeviceRouteTable] = _get_domain_data(hass).get(
        _PIDSPEC_ROUTE_TABLES, {}
    )
    for route_table in tables.values():
        if entity_id in route_table.entity_ids:
            return route_table
    return None


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


async def async_init_pidspec(
    hass: HomeAssistant,
    api_key: str | None = None,
    gateway_id: str | None = None,
) -> LocalRuleCache:
    """Initialize the pidspec engine: load rules, optionally set up cloud client.

    Call this in async_setup_entry after credentials are validated. *gateway_id*
    is the HA gateway's device id, threaded into the cloud client so the
    low-confidence report (interface 8) can carry it.
    Returns the loaded rule cache.
    """
    domain_data = _get_domain_data(hass)

    # Load rule cache from the local file — offload blocking I/O to a thread to
    # avoid blocking the Home Assistant event loop. When the file is missing the
    # cache comes back empty (rule_version=0, no pidspecs).
    cache = await LocalRuleCache.load_from_local_file_async(hass)
    domain_data[_PIDSPEC_RULE_CACHE] = cache
    domain_data.setdefault(_PIDSPEC_ROUTE_TABLES, {})

    # Set up cloud client if api_key available
    cloud_client = None
    if api_key:
        from .cloud_rule_client import TuyaCloudRuleClient

        # Fetch the gateway sub-device limit (interface 6) at startup so the
        # low-confidence report can be capped at limit + margin. Best-effort —
        # the client falls back to the documented default when this is None.
        sub_device_limit: int | None = None
        try:
            from .tuya_openapi import async_get_sub_device_limit

            sub_device_limit = await async_get_sub_device_limit(hass, api_key)
            LOGGER.debug(
                "pidspec_bridge: gateway sub-device limit=%d", sub_device_limit
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug(
                "pidspec_bridge: failed to fetch sub-device limit (%s), "
                "using default",
                exc,
            )

        cloud_client = TuyaCloudRuleClient(
            hass, api_key, gateway_id=gateway_id, sub_device_limit=sub_device_limit
        )
        domain_data[_PIDSPEC_CLOUD_CLIENT] = cloud_client

    # Bootstrap from cloud when local rules are missing/empty: fetch the full
    # bundle and persist it to disk (rules.json) so subsequent restarts load
    # locally. Best-effort — failure leaves an empty cache, which the periodic
    # version check can still recover later.
    if not cache.pidspecs and cloud_client is not None:
        LOGGER.warning(
            "pidspec_bridge: local rules empty/missing — fetching full bundle from cloud"
        )
        try:
            bundle = await cloud_client.async_fetch_rules_bundle()
            if cache.load_from_bundle(bundle):
                from .rules import async_save_rules_bundle

                await async_save_rules_bundle(bundle)
                LOGGER.info(
                    "pidspec_bridge: bootstrapped rules from cloud version=%d (%d pidspecs)",
                    cache.rule_version,
                    len(cache.pidspecs),
                )
        except Exception as exc:  # noqa: BLE001
            LOGGER.error(
                "pidspec_bridge: failed to bootstrap rules from cloud: %s", exc
            )

    LOGGER.debug(
        "pidspec_bridge: initialized rule cache version=%d (%d pidspecs)",
        cache.rule_version,
        len(cache.pidspecs),
    )

    return cache


# ---------------------------------------------------------------------------
# Lightweight inference for device discovery (publish_eligible)
# ---------------------------------------------------------------------------


@dataclass
class PidSpecDiscoveryResult:
    """Result of pidspec inference for device discovery."""

    product_id: str
    category_code: str
    ha_device_id: str
    routes: list[DPRoute] = field(default_factory=list)


def _unresolved_required_dps(
    diagnostics: list[dict[str, Any]], spec: PidSpec
) -> list[dict[str, Any]]:
    """Return diagnostics for unresolved REQUIRED DPs only.

    A diagnostic whose status != "ok" means that DP failed to bind to an entity.
    For an OPTIONAL DP that is an EXPECTED outcome — the device simply lacks
    that capability (e.g. a curtain with no fault sensor for the optional
    ``sensor_fault`` DP, or an air purifier missing one of its optional sensor
    DPs) — and must NOT demote / reject / unbind an otherwise-matched device.
    Only unresolved REQUIRED DPs constitute a real binding failure.

    All route-table consumers (full-property report fill,
    pidspec_build_default_properties) iterate the route table, never the spec's
    full DP list, so an optional DP that is simply absent from the routes is
    handled correctly downstream (it is neither reported nor declared).
    """
    required = set(spec.required_dps)
    return [
        d
        for d in diagnostics
        if d.get("status") != "ok" and d.get("dpcode") in required
    ]


def _infer_pidspec_with_diagnosis(
    hass: HomeAssistant,
    ha_device_id: str,
) -> tuple[PidSpecDiscoveryResult | None, str, PidInferResult | None]:
    """Run pidspec inference for one device, returning the discovery result, a
    human-readable diagnosis line (suitable for INFO logging), and the raw
    PidInferResult.

    Returns:
        (result, diagnosis, infer_result) where:
        - result is None for low-confidence / failed cases.
        - diagnosis is always populated and explains the outcome in one
          compact line:

              ✓ matched <pid> <category> score=N.N (M routes)
              ✗ <status> reason=<r> signal=<s> top=<pid> score=N.N
              ✗ no_profile_built
              ✗ rule_cache_empty

        - infer_result is the raw PidInferResult from infer_pid() (real signal +
          candidates), or None when the cache is empty / no profile could be
          built. Low-confidence reporting MUST forward this real result to the
          cloud Skill instead of fabricating a synthetic signal — the Skill's
          decision routing keys on signal (filter_empty / score_low /
          margin_tight / insufficient_domain_coverage) and top_candidates.
    """
    from .device_profiling import async_build_device_profile
    from .pidspec.inference import infer_pid

    domain_data = _get_domain_data(hass)
    cache: LocalRuleCache | None = domain_data.get(_PIDSPEC_RULE_CACHE)
    if cache is None or not cache.pidspecs:
        return None, "✗ rule_cache_empty", None

    profile = async_build_device_profile(hass, ha_device_id, cache)
    if profile is None:
        return None, "✗ no_profile_built (all entities filtered or device has none)", None

    result = infer_pid(profile, cache.pidspecs)
    if result.status != "matched" or result.product_id is None:
        top_info = ""
        if result.candidates:
            top_spec, top_score = result.candidates[0]
            top_info = f" top={top_spec.product_id} score={top_score:.1f}"
        diagnosis = (
            f"✗ {result.status} reason={result.reason} "
            f"signal={result.low_confidence_signal}{top_info}"
        )
        return None, diagnosis, result

    # Find matched PidSpec and validate DP routes
    matched_spec = None
    for spec in cache.pidspecs:
        if spec.product_id == result.product_id:
            matched_spec = spec
            break

    routes: list[DPRoute] = []
    if matched_spec is not None:
        routes, diagnostics = resolve_dp_routes(profile, matched_spec)
        # Only unresolved REQUIRED DPs are a real failure; unresolved OPTIONAL
        # DPs (e.g. a curtain without a fault sensor) are expected and must not
        # misclassify an otherwise-matched device as dp_route_failed.
        unresolved = _unresolved_required_dps(diagnostics, matched_spec)
        if unresolved:
            resolved_dps = [r.dpcode for r in routes]
            unresolved_dps = [u.get("dpcode") for u in unresolved]
            top_score = result.candidates[0][1] if result.candidates else 0.0
            diagnosis = (
                f"✗ dp_route_failed pid={matched_spec.product_id} score={top_score:.1f} "
                f"resolved={resolved_dps} unresolved={unresolved_dps}"
            )
            return None, diagnosis, result

    category_code = matched_spec.category_code if matched_spec else ""
    top_score = result.candidates[0][1] if result.candidates else 0.0
    diagnosis = (
        f"✓ matched pid={result.product_id} category={category_code} "
        f"score={top_score:.1f} entities={len(profile.entity_profiles)} "
        f"domains={list(profile.domain_set)} routes={len(routes)}"
    )

    return (
        PidSpecDiscoveryResult(
            product_id=result.product_id,
            category_code=category_code,
            ha_device_id=ha_device_id,
            routes=routes if matched_spec is not None else [],
        ),
        diagnosis,
        result,
    )


def pidspec_infer_for_discovery(
    hass: HomeAssistant,
    ha_device_id: str,
) -> PidSpecDiscoveryResult | None:
    """Run pidspec inference on a single device for discovery purposes.

    Returns PidSpecDiscoveryResult if high-confidence match with valid DP routes,
    None otherwise. Validates both PID match and DP route resolution.
    """
    result, _diagnosis, _infer = _infer_pidspec_with_diagnosis(hass, ha_device_id)
    return result


def pidspec_infer_batch_for_discovery(
    hass: HomeAssistant,
    ha_device_ids: list[str],
) -> tuple[dict[str, PidSpecDiscoveryResult], list[str]]:
    """Run pidspec inference on multiple devices.

    Returns:
        (matched_dict, low_confidence_ids) where matched_dict is
        {ha_device_id: PidSpecDiscoveryResult} for high-confidence devices,
        and low_confidence_ids are devices that need cloud upgrade.
    """
    from .device_profiling import async_build_device_profile
    from .pidspec.inference import infer_pid

    domain_data = _get_domain_data(hass)
    cache: LocalRuleCache | None = domain_data.get(_PIDSPEC_RULE_CACHE)
    if cache is None or not cache.pidspecs:
        return {}, ha_device_ids

    matched: dict[str, PidSpecDiscoveryResult] = {}
    low_confidence: list[str] = []
    low_confidence_detail: list[str] = []

    for ha_device_id in ha_device_ids:
        profile = async_build_device_profile(hass, ha_device_id, cache)
        if profile is None:
            continue

        result = infer_pid(profile, cache.pidspecs)
        if result.status == "matched" and result.product_id is not None:
            # Find matched spec and validate DP routes
            matched_spec = None
            for spec in cache.pidspecs:
                if spec.product_id == result.product_id:
                    matched_spec = spec
                    break

            if matched_spec is not None:
                routes, diagnostics = resolve_dp_routes(profile, matched_spec)
                # Only unresolved REQUIRED DPs are a real failure; unresolved
                # OPTIONAL DPs (device lacks that capability) are expected.
                unresolved = _unresolved_required_dps(diagnostics, matched_spec)
                if unresolved:
                    # PID matched but DP routes failed → low confidence
                    resolved_dps = [r.dpcode for r in routes]
                    unresolved_dps = [u.get("dpcode") for u in unresolved]
                    top_score = result.candidates[0][1] if result.candidates else 0.0
                    low_confidence.append(ha_device_id)
                    low_confidence_detail.append(
                        f"  ✗ {ha_device_id} (name={profile.name}) → dp_route_failed "
                        f"pid={matched_spec.product_id} score={top_score:.1f} "
                        f"resolved={resolved_dps} unresolved={unresolved_dps}"
                    )
                    continue

                category_code = matched_spec.category_code
            else:
                routes = []
                category_code = ""

            top_score = result.candidates[0][1] if result.candidates else 0.0
            matched[ha_device_id] = PidSpecDiscoveryResult(
                product_id=result.product_id,
                category_code=category_code,
                ha_device_id=ha_device_id,
            )
            LOGGER.info(
                "pidspec_batch_discovery: ✓ %s (name=%s) → %s category=%s score=%.1f\n"
                "  dpcode routes (%d):\n%s",
                ha_device_id, profile.name, result.product_id,
                category_code, top_score,
                len(routes),
                "\n".join(
                    f"    {r.dpcode} → {r.entity_id} ({r.domain}/{r.direction}, "
                    f"converter={r.converter}, component={r.component})"
                    for r in routes
                ) if routes else "    (none)",
            )
        else:
            low_confidence.append(ha_device_id)
            top_info = ""
            if result.candidates:
                top_info = f" top={result.candidates[0][0].product_id} score={result.candidates[0][1]:.1f}"
            low_confidence_detail.append(
                f"  ✗ {ha_device_id} (name={profile.name}) → {result.status} "
                f"reason={result.reason} signal={result.low_confidence_signal}{top_info}"
            )

    # Batch summary
    LOGGER.info(
        "pidspec_batch_discovery: %d/%d matched (%d low-confidence)\n%s",
        len(matched), len(ha_device_ids), len(low_confidence),
        "\n".join(low_confidence_detail) if low_confidence_detail else "  (all matched)",
    )

    return matched, low_confidence


# ---------------------------------------------------------------------------
# Inference + Route Table Building
# ---------------------------------------------------------------------------


async def async_infer_and_build_routes(
    hass: HomeAssistant,
    device_profiles: dict[str, DeviceProfile],
    tuya_device_id_map: dict[str, str],
) -> InferenceResults:
    """Run inference for device profiles and build route tables for matched devices.

    Args:
        hass: HomeAssistant instance.
        device_profiles: {ha_device_id: DeviceProfile} from device_profiling.
        tuya_device_id_map: {ha_device_id: tuya_device_id} for bound devices.

    Returns:
        InferenceResults with matched/low_confidence info.
    """
    domain_data = _get_domain_data(hass)
    cache: LocalRuleCache | None = domain_data.get(_PIDSPEC_RULE_CACHE)
    if cache is None:
        LOGGER.warning("pidspec_bridge: rule cache not initialized")
        return InferenceResults()

    cloud_client = domain_data.get(_PIDSPEC_CLOUD_CLIENT)

    # Run inference with optional refresh
    results = await async_infer_with_refresh(
        cache, device_profiles, cloud_client, hass=hass
    )

    # Build route tables for matched devices; demote incomplete ones
    demoted: dict[str, PidInferResult] = {}

    for ha_device_id, infer_result in list(results.matched.items()):
        if infer_result.product_id is None:
            continue

        tuya_device_id = tuya_device_id_map.get(ha_device_id)
        if not tuya_device_id:
            LOGGER.debug(
                "pidspec_bridge: skip dp_routing for %s — no tuya_device_id mapping",
                ha_device_id,
            )
            continue

        profile = device_profiles[ha_device_id]

        # Find the matched PidSpec
        matched_spec = None
        for spec in cache.pidspecs:
            if spec.product_id == infer_result.product_id:
                matched_spec = spec
                break

        if matched_spec is None:
            LOGGER.debug(
                "pidspec_bridge: skip dp_routing for %s — matched spec not found for pid=%s",
                ha_device_id, infer_result.product_id,
            )
            continue

        # Resolve DP routes
        routes, diagnostics = resolve_dp_routes(profile, matched_spec)
        # Only unresolved REQUIRED DPs are a real failure; unresolved OPTIONAL
        # DPs (device lacks that capability) are expected — do not demote.
        unresolved = _unresolved_required_dps(diagnostics, matched_spec)

        if unresolved:
            resolved_lines = [
                f"    ✓ {r.dpcode} → {r.entity_id} ({r.domain}/{r.direction}, "
                f"converter={r.converter}, component={r.component})"
                for r in routes
            ]
            unresolved_lines = [
                f"    ✗ {u.get('dpcode')} status={u.get('status')} reason={u.get('reason')}"
                for u in unresolved
            ]
            LOGGER.info(
                "pidspec_bridge: demoting device %s (name=%s)\n"
                "  product_id=%s category=%s tuya_device_id=%s\n"
                "  resolved (%d):\n%s\n"
                "  unresolved (%d):\n%s",
                ha_device_id, profile.name,
                matched_spec.product_id, matched_spec.category_code, tuya_device_id,
                len(routes), "\n".join(resolved_lines) if resolved_lines else "    (none)",
                len(unresolved), "\n".join(unresolved_lines),
            )
            demoted[ha_device_id] = PidInferResult(
                product_id=infer_result.product_id,
                status="low_confidence",
                reason="unresolved_dps",
                low_confidence_signal={
                    "matched_pid": infer_result.product_id,
                    "unresolved_dps": [u.get("dpcode") for u in unresolved],
                },
            )
            continue

        # All DPs resolved — build route table
        route_table = build_device_route_table(
            profile, matched_spec, tuya_device_id, routes
        )
        store_route_table(hass, tuya_device_id, route_table)

        # Log matched device with PID and dpcode routes
        route_lines = [
            f"    {r.dpcode} → {r.entity_id} ({r.domain}/{r.direction}, "
            f"converter={r.converter}, component={r.component})"
            for r in routes
        ]
        # Extract top score from candidates list
        top_score = 0.0
        if infer_result.candidates:
            top_score = infer_result.candidates[0][1]
        LOGGER.info(
            "pidspec_bridge: ✓ matched device %s (name=%s)\n"
            "  product_id=%s category=%s score=%.1f\n"
            "  tuya_device_id=%s primary_entity=%s\n"
            "  dpcode routes (%d):\n%s",
            ha_device_id,
            profile.name,
            matched_spec.product_id,
            matched_spec.category_code,
            top_score,
            tuya_device_id,
            route_table.primary_entity_id,
            len(routes),
            "\n".join(route_lines),
        )

    # Move demoted devices from matched to low_confidence
    for ha_device_id in demoted:
        results.matched.pop(ha_device_id, None)
    results.low_confidence.update(demoted)

    # Summary
    LOGGER.info(
        "pidspec_bridge: inference complete — %d matched, %d low-confidence, rules_refreshed=%s",
        len(results.matched),
        len(results.low_confidence),
        results.rules_refreshed,
    )

    # Report demoted devices to cloud if client available. These are
    # carrier_failed cases (PID matched but DP routes could not be assigned) —
    # distinct from the pure-inference low-confidence devices already reported
    # inside async_infer_with_refresh (step 4).
    if demoted:
        cloud_client = domain_data.get(_PIDSPEC_CLOUD_CLIENT)
        if cloud_client is not None:
            report_payload = await build_low_confidence_report_payload(
                device_profiles, demoted, hass=hass
            )
            try:
                await cloud_client.async_report_low_confidence(
                    report_payload,
                    local_rule_version=cache.rule_version,
                    trigger="plugin_start",
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning(
                    "pidspec_bridge: failed to report demoted devices: %s", exc
                )

    return results


# ---------------------------------------------------------------------------
# Bind-time resolution: pidspec-driven entity selection
# ---------------------------------------------------------------------------


@dataclass
class PidSpecBindResult:
    """Result of pidspec-driven bind resolution."""

    entity_ids: list[str]
    primary_entity_id: str
    primary_domain: str
    category_code: str
    route_table: DeviceRouteTable


def pidspec_resolve_bind(
    hass: HomeAssistant,
    ha_device_id: str,
    product_id: str,
    tuya_device_id: str,
) -> PidSpecBindResult | None:
    """Resolve binding using pidspec DP definitions.

    Looks up PidSpec by product_id, builds DeviceProfile, resolves all DPs
    to entities. Returns PidSpecBindResult if ALL DPs resolve, None otherwise.

    Called from bind response handler as replacement for mapping-based
    domain/entity selection.
    """
    from .device_profiling import async_build_device_profile

    domain_data = _get_domain_data(hass)
    cache: LocalRuleCache | None = domain_data.get(_PIDSPEC_RULE_CACHE)
    if cache is None:
        return None

    # Find PidSpec by product_id
    matched_spec: PidSpec | None = None
    for spec in cache.pidspecs:
        if spec.product_id == product_id:
            matched_spec = spec
            break

    if matched_spec is None:
        LOGGER.debug(
            "pidspec_resolve_bind: no PidSpec for product_id=%s", product_id
        )
        return None

    # Build DeviceProfile
    profile = async_build_device_profile(hass, ha_device_id, cache)
    if profile is None:
        LOGGER.debug(
            "pidspec_resolve_bind: no DeviceProfile for device %s", ha_device_id
        )
        return None

    # Resolve DP routes
    routes, diagnostics = resolve_dp_routes(profile, matched_spec)
    # Only unresolved REQUIRED DPs are a real failure; unresolved OPTIONAL DPs
    # (device lacks that capability) are expected — do not reject the bind.
    unresolved = _unresolved_required_dps(diagnostics, matched_spec)

    if unresolved:
        LOGGER.info(
            "pidspec_resolve_bind: device %s has %d unresolved DPs: %s — rejecting bind",
            ha_device_id,
            len(unresolved),
            [u.get("dpcode") for u in unresolved],
        )
        return None

    # All DPs resolved — build route table
    route_table = build_device_route_table(
        profile, matched_spec, tuya_device_id, routes
    )

    # Determine primary domain from primary entity
    primary_domain = ""
    if route_table.primary_entity_id:
        parts = route_table.primary_entity_id.split(".")
        if parts:
            primary_domain = parts[0]

    return PidSpecBindResult(
        entity_ids=sorted(route_table.entity_ids),
        primary_entity_id=route_table.primary_entity_id,
        primary_domain=primary_domain,
        category_code=matched_spec.category_code,
        route_table=route_table,
    )


# ---------------------------------------------------------------------------
# Runtime dispatch (drop-in replacements)
# ---------------------------------------------------------------------------


def pidspec_build_service_calls(
    hass: HomeAssistant,
    tuya_device_id: str,
    tuya_data: dict[str, Any],
    entity_id: str,
    context: dict[str, Any] | None = None,
) -> list[dict[str, Any]] | None:
    """Convert Tuya property/set data to HA service calls using pidspec.

    Drop-in replacement for build_service_calls_from_tuya.
    Returns None if no route table exists (caller should fall back to legacy).
    """
    route_table = get_route_table(hass, tuya_device_id)
    if route_table is None:
        return None

    return dispatch_tuya_to_ha(route_table, tuya_data, context)


def pidspec_build_properties(
    hass: HomeAssistant,
    tuya_device_id: str,
    entity_id: str,
    entity_state: State,
    context: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]] | None:
    """Convert HA state to Tuya property report using pidspec.

    Drop-in replacement for build_tuya_properties_from_state.
    Returns None if no route table exists (caller should fall back to legacy).
    """
    route_table = get_route_table(hass, tuya_device_id)
    if route_table is None:
        return None

    ha_state = {"state": entity_state.state}
    ha_attributes = dict(entity_state.attributes)

    return dispatch_ha_to_tuya(route_table, entity_id, ha_state, ha_attributes, context)


def pidspec_build_full_device_properties(
    hass: HomeAssistant,
    tuya_device_id: str,
    context: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]] | None:
    """Assemble a COMPLETE PID-level property report from ALL bound entities.

    Tuya expects FULL-STATE reporting at the PID level: every report must carry
    every resolvable DP for the device, not just the DPs of the entity that
    happened to change. We assemble the report from each bound entity's LIVE HA
    state (HA is the single source of truth), so concurrent changes on different
    entities can never overwrite each other with a stale partial snapshot — each
    flush always reflects the current state of every entity.

    Return contract:
    - ``None``  → no route table for this device; caller should fall back to the
      legacy single-entity path.
    - ``{}``    → route table exists but a REQUIRED DP's entity is unavailable/
      unknown, so the device's core state can't be represented; the caller skips
      this round and a later change / periodic report catches up. (An OPTIONAL
      DP's entity being unknown does NOT defer — that DP is emitted as a type
      default so the report stays a COMPLETE state, which Tuya requires since the
      cloud never merges partial reports. b2 behaviour.)
    - non-empty dict → the complete PID payload.
    """
    route_table = get_route_table(hass, tuya_device_id)
    if route_table is None:
        return None
    return _build_full_properties_from_route_table(hass, route_table, context)


def _build_full_properties_from_route_table(
    hass: HomeAssistant,
    route_table: DeviceRouteTable,
    context: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build the complete PID payload from a (stored or freshly-built) route
    table. Returns {} only when a REQUIRED DP's entity is unavailable/unknown
    (b2); unknown OPTIONAL-DP entities are emitted as type defaults rather than
    deferring the whole report, so the payload stays a complete Tuya state."""
    properties: dict[str, dict[str, Any]] = {}
    required_dpcodes = route_table.required_dpcodes
    for entity_id in sorted(route_table.entity_ids):
        state = hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            # b2: defer the WHOLE report only when a REQUIRED DP's entity is
            # unreportable — without its core DPs the device state is
            # meaningless. An OPTIONAL DP's entity being unknown must NOT block
            # the report; otherwise one stuck secondary sensor freezes the whole
            # panel. Tuya needs a COMPLETE state every time (the cloud never
            # merges partial reports), so the unknown optional DP is still
            # emitted — as a type default — by the fill loop below.
            #
            # Conservative fallback: an empty required_dpcodes (e.g. a route
            # table built before this field existed) is treated as "everything
            # required" → legacy defer-on-any-unknown.
            carries_required = not required_dpcodes or any(
                r.dpcode in required_dpcodes
                for r in route_table.entity_to_routes.get(entity_id, [])
            )
            if carries_required:
                LOGGER.debug(
                    "full report deferred for tuya_device_id=%s: required entity "
                    "%s not reportable (state=%s)",
                    route_table.tuya_device_id,
                    entity_id,
                    None if state is None else state.state,
                )
                return {}
            LOGGER.debug(
                "tuya_device_id=%s: optional entity %s not reportable (state=%s); "
                "emitting its DP(s) as default instead of deferring",
                route_table.tuya_device_id,
                entity_id,
                None if state is None else state.state,
            )
            continue
        ha_state = {"state": state.state}
        ha_attributes = dict(state.attributes)
        properties.update(
            dispatch_ha_to_tuya(
                route_table, entity_id, ha_state, ha_attributes, context
            )
        )

    # Tuya is STATELESS: every report must carry EVERY DP. A code whose
    # converter could not produce a value (attribute momentarily missing, enum
    # reverse-map miss, etc.) is filled with a type default rather than dropped.
    # The rule is "unobtainable value → default", never "unobtainable → omit the
    # code". (Device-level unavailability is handled above by deferring entirely.)
    now_ms = int(time.time() * 1000)
    for dpcode, route in route_table.dpcode_to_route.items():
        if dpcode not in properties:
            properties[dpcode] = {
                "time": now_ms,
                "value": _default_value_for_route(route),
            }

    return properties


# ---------------------------------------------------------------------------
# Bind-time physical model (defaultProperties) — PID-native replacement for
# the legacy mapping TUYA_PROPERTY_METADATA path.
# ---------------------------------------------------------------------------


def _coerce_number(value: Any) -> int | float | None:
    """Return *value* if it is a real number (bools excluded), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _resolve_dp_properties(
    hass: HomeAssistant,
    entity_id: str,
    converter_config: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Resolve a winning route's ``converter_config`` into a Tuya
    ``dpProperties`` dict (min/max/step/unit/range).

    The thing-model declaration keys live directly in ``converter_config`` —
    the same dict the runtime converter reads — so a DP's declared capabilities
    always follow the carrier that actually won routing. This matters for
    multi-carrier DPs (e.g. ``suction`` → ``options`` on a select vs
    ``fan_speed_list`` on a vacuum): each candidate_target names its own
    ``range_attr``, and there is no DP-level fork to keep in sync.

    Recognised declaration keys (all optional):
      ``range_attr`` (live list attr), ``fallback`` ({min,max,step,range}
      literals used when the attr is absent), ``min_attr``/``max_attr``/
      ``step_attr`` (live numeric attrs), ``step`` (literal), ``unit``,
      ``value_map`` (range derived from keys by the caller), and
      ``tuya_contract`` ({min,max} clamp so we never declare wider than the
      product supports). Quantitative facts come from the live entity first
      (HA is the source of truth); literals are the fallback. Returns None when
      nothing resolvable is present.
    """
    cfg = converter_config or {}
    fallback = cfg.get("fallback") or {}
    dp_props: dict[str, Any] = {}

    # --- enum range ---
    rng: list[Any] | None = None
    range_attr = cfg.get("range_attr")
    if range_attr:
        cap = get_capability(hass, entity_id, range_attr)
        if isinstance(cap, (list, tuple)) and cap:
            rng = list(cap)
    if rng is None and isinstance(fallback.get("range"), (list, tuple)) and fallback["range"]:
        rng = list(fallback["range"])
    if rng is not None:
        value_map = cfg.get("value_map")
        if isinstance(value_map, dict):
            rng = [value_map.get(v, v) for v in rng]
        dp_props["range"] = rng
        dp_props["type"] = "enum"

    # --- numeric bounds (attr → fallback) ---
    def _bound(attr_key: str, fb_key: str) -> int | float | None:
        attr = cfg.get(attr_key)
        if attr:
            resolved = _coerce_number(get_capability(hass, entity_id, attr))
            if resolved is not None:
                return resolved
        return _coerce_number(fallback.get(fb_key))

    vmin = _bound("min_attr", "min")
    vmax = _bound("max_attr", "max")

    step: int | float | None = None
    if cfg.get("step_attr"):
        step = _coerce_number(get_capability(hass, entity_id, cfg["step_attr"]))
    if step is None:
        step = _coerce_number(cfg.get("step"))
    if step is None:
        step = _coerce_number(fallback.get("step"))

    # clamp to the Tuya product contract (intersection)
    contract = cfg.get("tuya_contract") or {}
    cmin = _coerce_number(contract.get("min"))
    cmax = _coerce_number(contract.get("max"))
    if vmin is not None and cmin is not None:
        vmin = max(vmin, cmin)
    if vmax is not None and cmax is not None:
        vmax = min(vmax, cmax)

        # 与 value 同域:范围也乘 DP scale(value 27.0→270 ⇒ min 17.0→170)。
        # 读同一个 converter_config["scale"](std:numeric_scale / group:climate_temp)。
        # 放在 tuya_contract clamp 之后(clamp 在真实度数域比较)。enum range 不动。
        try:
            scale = float(cfg.get("scale", 1))
            if scale <= 0:
                scale = 1.0
        except (TypeError, ValueError):
            scale = 1.0

        if vmin is not None:
            dp_props["min"] = int(round(vmin * scale)) if scale != 1 else vmin
        if vmax is not None:
            dp_props["max"] = int(round(vmax * scale)) if scale != 1 else vmax
        if step is not None:
            dp_props["step"] = int(round(step * scale)) if scale != 1 else step
        if cfg.get("unit") is not None:
                dp_props["unit"] = cfg["unit"]

    return dp_props or None


def pidspec_build_default_properties(
    hass: HomeAssistant,
    route_table: DeviceRouteTable,
    context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build bind-time ``defaultProperties`` from a device's route table.

    PID-native replacement for the legacy ``get_property_metadata`` +
    ``build_tuya_properties_from_state`` path used at topo/add/notify. Each
    routed DP yields a ``{"code", ["value"], ["dpProperties"]}`` item: the value
    comes from the live full-PID payload, the dpProperties (min/max/step/range)
    from the winning route's ``converter_config`` resolved against its carrier
    entity.

    Accepts a route_table directly (the bind flow builds one on the fly via
    pidspec_resolve_bind; runtime callers can pass get_route_table(...)), so it
    works before the table is stored. Returns the defaultProperties items
    (possibly empty when no DP carries a value or declaration metadata).
    """
    # Live values for every DP (may be {} if the device isn't fully reportable;
    # we still emit the model declaration so the cloud knows the capabilities).
    values = _build_full_properties_from_route_table(
        hass, route_table, context
    ) or {}

    items: list[dict[str, Any]] = []
    for dpcode, route in route_table.dpcode_to_route.items():
        item: dict[str, Any] = {"code": dpcode}
        prop = values.get(dpcode)
        if isinstance(prop, dict) and "value" in prop:
            item["value"] = prop["value"]
        else:
            # Value unavailable at bind time. Tuya does NOT persist a last
            # value, so every DP must carry one: numeric → 0, enum/string →
            # "" (empty), bool → False, JSON-struct → a valid JSON structure.
            item["value"] = _default_value_for_route(route)
        dp_props = _resolve_dp_properties(
            hass, route.entity_id, route.converter_config
        )
        # Enum DPs whose valid values are declared by the converter's value_map
        # (the rule-defined action set, e.g. cover control) derive their enum
        # range from the map keys, so the cloud learns the full enum domain even
        # without a range_attr. Device value == cloud value, so the map
        # keys ARE the tuya enum values.
        if dp_props is None or "range" not in dp_props:
            value_map = (route.converter_config or {}).get("value_map")
            if isinstance(value_map, dict) and value_map:
                dp_props = dict(dp_props or {})
                dp_props.setdefault("type", "enum")
                dp_props["range"] = list(value_map.keys())
        if dp_props:
            item["dpProperties"] = dp_props
        items.append(item)

    return items


def _default_value_for_route(route: DPRoute) -> Any:
    """Hard default for a DP whose live value can't be resolved at bind time.

    numeric → 0, bool → False, string → "". Enum DPs default to a VALID range
    member (explicit ``default`` / first ``value_map`` key), never "" — Tuya
    rejects an out-of-range enum value. JSON-structured DPs (e.g. colour_data
    HSV) must keep a valid JSON structure — an empty string would break the
    cloud's thing-model parse — so they fall back to a zero-valued structure
    derived from the converter schema / explicit default.
    """
    dp_type = route.dp_type
    if dp_type in ("value", "int", "integer"):
        return 0
    if dp_type == "bool":
        return False

    cfg = route.converter_config or {}
    is_json_struct = (
        route.sub_type == "json_struct"
        or route.converter in ("std:json_struct", "group:light_color")
    )
    if is_json_struct:
        default = cfg.get("default")
        if isinstance(default, (dict, list)):
            return json.dumps(default, separators=(",", ":"))
        if default is not None:
            return default
        schema = cfg.get("schema")
        if isinstance(schema, dict) and schema:
            zero = {f: int(fd.get("tuya_min", 0)) for f, fd in schema.items()}
            return json.dumps(zero, separators=(",", ":"))
        # colour_data-style HSV fallback
        return json.dumps({"h": 0, "s": 0, "v": 0}, separators=(",", ":"))

    # Enum DPs must default to a VALID member of their declared range — Tuya
    # rejects an out-of-range enum value (e.g. ""). The value_map keys ARE the
    # Tuya enum values ("device value == cloud value"), so use an explicit
    # ``default`` if set, else the first declared key.
    if dp_type == "enum":
        enum_default = cfg.get("default")
        if enum_default is not None:
            return enum_default
        value_map = cfg.get("value_map")
        if isinstance(value_map, dict) and value_map:
            return next(iter(value_map))
        return ""

    # string / anything else → empty
    return ""


# ---------------------------------------------------------------------------
# Startup check: infer + unbind low-confidence devices
# ---------------------------------------------------------------------------


async def async_pidspec_check_and_unbind(
    hass: HomeAssistant,
    bound_devices: dict[str, dict[str, Any]],
) -> list[str]:
    """Run pidspec inference on bound devices; return tuya_device_ids to unbind.

    Called after topo/get reconciliation. For each bound device:
    - Build DeviceProfile from HA registry
    - Run inference (with cloud refresh if needed)
    - If low confidence OR unresolved DPs → mark for unbind + report
    - If fully matched → build route table

    Args:
        hass: HomeAssistant instance.
        bound_devices: bindings["devices"] dict — {ha_device_id: {tuya_device_id, ...}}

    Returns:
        List of tuya_device_ids that should be unbound.
    """
    from .device_profiling import async_build_device_profile

    domain_data = _get_domain_data(hass)
    cache: LocalRuleCache | None = domain_data.get(_PIDSPEC_RULE_CACHE)
    if cache is None:
        LOGGER.debug("pidspec_check: rule cache not initialized, skipping")
        return []

    # Build device profiles and tuya_device_id mapping
    device_profiles: dict[str, DeviceProfile] = {}
    tuya_device_id_map: dict[str, str] = {}

    for ha_device_id, device_data in bound_devices.items():
        tuya_device_id = device_data.get("tuya_device_id")
        if not isinstance(tuya_device_id, str) or not tuya_device_id:
            continue

        profile = async_build_device_profile(hass, ha_device_id, cache)
        if profile is None:
            continue

        device_profiles[ha_device_id] = profile
        tuya_device_id_map[ha_device_id] = tuya_device_id

    if not device_profiles:
        LOGGER.debug("pidspec_check: no device profiles built, skipping")
        return []

    # Run inference + build route tables (demotes unresolved-DP devices)
    results = await async_infer_and_build_routes(hass, device_profiles, tuya_device_id_map)

    # Collect tuya_device_ids for low-confidence devices → to unbind
    unbind_tuya_ids: list[str] = []
    for ha_device_id in results.low_confidence:
        tuya_device_id = tuya_device_id_map.get(ha_device_id)
        if tuya_device_id:
            unbind_tuya_ids.append(tuya_device_id)

    if unbind_tuya_ids:
        LOGGER.info(
            "pidspec_check: %d devices marked for unbind",
            len(unbind_tuya_ids),
        )

    return unbind_tuya_ids
