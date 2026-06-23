"""Inference orchestrator — coordinates rule refresh and low-confidence reporting.

Implements the retry-before-report flow:
1. Run local inference
2. If low-confidence devices exist, check cloud rule version
3. If newer version available, fetch bundle and re-infer
4. If still low-confidence after refresh, report to cloud

Public API:
- async_infer_with_refresh(cache, device_profiles, cloud_client, hass) → InferenceResults
- build_low_confidence_report_payload(device_profiles, low_confidence, hass) → list[dict]
  (emits the cloud Skill's DeviceReport batch[] shape; see §4.4 of
   docs/cloud_inference_architecture.md)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .inference import infer_pid
from .entity_filter import should_include_entity
from .models import DeviceProfile, PidInferResult
from .rule_cache import LocalRuleCache
from ..const import LOGGER


# Mapping from a HA vol.Schema key descriptor → (type_name, min, max, step, unit, options).
# Covers the field types that appear in cover / fan / climate / light / humidifier
# service schemas. Anything outside this map falls back to type="str".
_VOL_TYPE_MAP: dict[str, str] = {
    "int": "int",
    "integer": "int",
    "float": "float",
    "number": "float",
    "str": "str",
    "string": "str",
    "bool": "bool",
    "boolean": "bool",
    "list": "list",
    "dict": "dict",
}


def _extract_service_params(fields: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Convert a HA service ``fields`` dict to a JSON-friendly param schema.

    Each HA field is a vol.Schema descriptor with keys like ``"selector"``,
    ``"required"``, ``"default"``, ``"example"``, ``"description"``. We pick
    the most informative type / range from ``selector`` if present.

    Returns an empty dict for services with no parameters (or a fields=None
    input). ``entity_id`` is filtered out — it is a target selector, not a
    real parameter.
    """
    out: dict[str, dict[str, Any]] = {}
    if not fields:
        return out
    for param_name, spec in fields.items():
        if param_name == "entity_id":
            # target selector, not a service param
            continue
        if not isinstance(spec, dict):
            out[param_name] = {"type": "str"}
            continue

        entry: dict[str, Any] = {"type": "str", "required": bool(spec.get("required", False))}

        selector = spec.get("selector") or {}
        # Each selector looks like {"number": {"min": 0, "max": 100, "step": 1,
        # "unit_of_measurement": "%"}} or {"select": {"options": ["a","b"]}}.
        if "number" in selector:
            num = selector["number"] or {}
            entry["type"] = "int" if num.get("mode", "slider") != "box" and isinstance(
                num.get("min"), int
            ) and isinstance(num.get("max"), int) else "float"
            if "min" in num:
                entry["min"] = num["min"]
            if "max" in num:
                entry["max"] = num["max"]
            if "step" in num:
                entry["step"] = num["step"]
            unit = num.get("unit_of_measurement")
            if unit is not None:
                entry["unit"] = str(unit)
        elif "select" in selector:
            sel = selector["select"] or {}
            entry["type"] = "str"
            options = sel.get("options")
            if options:
                entry["options"] = list(options)
        elif "boolean" in selector:
            entry["type"] = "bool"
        elif "text" in selector:
            entry["type"] = "str"
        elif "time" in selector:
            entry["type"] = "str"
        elif "date" in selector:
            entry["type"] = "str"
        elif "entity" in selector:
            entry["type"] = "str"
        elif "color_rgb" in selector or "color_temp" in selector:
            entry["type"] = "str"

        # Fallback: try to derive from raw spec if selector wasn't informative
        if entry["type"] == "str":
            for key, type_name in _VOL_TYPE_MAP.items():
                if key in spec:
                    entry["type"] = type_name
                    break

        if "default" in spec:
            entry["default"] = spec["default"]
        if "example" in spec and "default" not in spec:
            entry["example"] = spec["example"]

        out[param_name] = entry
    return out


async def _fetch_service_descriptions(hass: Any | None) -> dict[str, Any] | None:
    """Fetch HA's full service-description map, or None when unavailable.

    Returns the result of :func:`homeassistant.helpers.service.async_get_all_descriptions`,
    shaped as ``{domain: {service_name: {"name", "description", "fields", "target"}}}``.

    This is the authoritative source of service *parameter* metadata (selectors,
    min/max, options, units). ``hass.services.async_services()`` only returns
    ``{domain: {service_name: Service}}`` whose ``Service`` objects carry no
    ``fields`` — which is why the previous implementation produced empty lists.

    Returns None when ``hass`` is None (unit tests / cloud-side replay) or the
    lookup fails; never raises — service metadata is best-effort enrichment.
    """
    if hass is None:
        return None
    try:
        from homeassistant.helpers.service import async_get_all_descriptions

        return await async_get_all_descriptions(hass)
    except Exception:  # noqa: BLE001
        LOGGER.debug(
            "orchestrator: async_get_all_descriptions failed", exc_info=True
        )
        return None


def _get_entity_services(
    descriptions: dict[str, Any] | None, domain: str
) -> list[dict[str, Any]]:
    """Return the list of service specs (JSON-friendly) for *domain*.

    Reads from the pre-fetched *descriptions* map (see
    :func:`_fetch_service_descriptions`). Returns an empty list if descriptions
    are unavailable or the domain exposes no services. Never raises; a single
    malformed service descriptor is skipped rather than blocking the report.
    """
    if not descriptions:
        return []

    svc_map = descriptions.get(domain) or {}
    result: list[dict[str, Any]] = []
    for svc_name, desc in svc_map.items():
        try:
            fields = desc.get("fields") if isinstance(desc, dict) else None
            params = _extract_service_params(fields)
            result.append({
                "name": svc_name,
                "domain": domain,
                "parameters": params,
                "target_entity_supported": isinstance(desc, dict) and "target" in desc,
            })
        except Exception:  # noqa: BLE001
            # Skip services whose schema we can't introspect — never block the
            # report on a single malformed service descriptor.
            LOGGER.debug(
                "orchestrator: skipping service %s.%s (introspection failed)",
                domain, svc_name,
            )
    return result


def _extract_capabilities(attributes: dict[str, Any]) -> dict[str, Any]:
    """Pull thing-model-relevant capability VALUES from an entity's attributes.

    The cloud Skill needs the actual ranges/bounds (not just attribute names)
    to author correct thing-model declaration keys (range_attr/fallback/
    min_attr/max_attr/…) in each candidate_target's converter_config for
    value/enum DPs — otherwise it has to guess numeric ranges, which is exactly
    the failure we want to avoid. We emit:

    - enum ranges: any attribute whose value is a non-empty list of primitives
      (e.g. operation_list / hvac_modes / preset_modes / fan_modes / options).
    - numeric bounds: attributes that name a bound or step (min / max / step,
      ``min_*`` / ``max_*`` / ``*_step``) with a numeric value (e.g. min_temp,
      max_humidity, target_temp_step).

    Bounded on purpose (skip absurdly long lists) to keep the upload small.
    Returns {} when nothing capability-shaped is present.
    """
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        if isinstance(value, (list, tuple)):
            items = list(value)
            if items and len(items) <= 64 and all(
                isinstance(v, (str, int, float, bool)) for v in items
            ):
                out[key] = items
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            low = key.lower()
            if (
                low in ("min", "max", "step")
                or low.startswith(("min_", "max_"))
                or low.endswith(("_min", "_max", "_step"))
            ):
                out[key] = value
    return out


def _entity_live_state(hass: Any | None, entity_id: str) -> str | None:
    """Best-effort read of an entity's current state string from hass."""
    if hass is None:
        return None
    try:
        st = hass.states.get(entity_id)
        return st.state if st is not None else None
    except Exception:  # noqa: BLE001
        return None


def _resolve_area_name(hass: Any | None, device_id: str | None) -> str | None:
    """Best-effort resolution of a device's area name via HA registries."""
    if hass is None or not device_id:
        return None
    try:
        from homeassistant.helpers import area_registry as ar, device_registry as dr

        dev_reg = dr.async_get(hass)
        device = dev_reg.async_get(device_id)
        if device is None or device.area_id is None:
            return None
        area = ar.async_get(hass).async_get_area(device.area_id)
        return area.name if area is not None else None
    except Exception:  # noqa: BLE001
        return None


def _serialize_device_item(
    ha_device_id: str,
    profile: DeviceProfile,
    descriptions: dict[str, Any] | None = None,
    hass: Any | None = None,
) -> dict[str, Any]:
    """Serialize a DeviceProfile into a Fusion ``DeviceItem`` (interface 8).

    Emits the flat device-level fields (``ha_device_id`` / ``name`` / ``model``
    / ``manufacturer`` / ``area``) plus the ``entities`` list, each entity being
    an ``EntityItem`` (see ``HA接口文档`` §8 / ``docs/cloud_inference_architecture.md``
    §4.4). ``infer_result`` is attached by the caller.

    Re-applies the global entity filter (EXCLUDED_DOMAINS / EXCLUDED_CATEGORIES
    / CONDITIONAL_DOMAINS) so the report never contains entities that are noise
    to the cloud side, even if the source DeviceProfile predates the filter.

    The 11 documented EntityItem fields are always emitted. ``capabilities``
    (enum ranges / numeric bounds) and per-domain ``services`` are attached as
    additive enrichment — they are not part of the documented EntityItem but
    the cloud Skill reads them to author converter_config for value/enum DPs and
    DP→service bindings. The Fusion mock logs the full request, so extra keys are
    preserved; conformance only requires the documented keys to be present.
    """
    # Domain → services cache so we only transform each domain once.
    service_cache: dict[str, list[dict[str, Any]]] = {}

    visible_entities = [
        ep for ep in profile.entity_profiles if should_include_entity(ep)
    ]
    return {
        "ha_device_id": ha_device_id,
        "name": profile.name,
        "model": profile.model,
        "manufacturer": profile.manufacturer,
        "area": _resolve_area_name(hass, ha_device_id),
        "entities": [
            {
                # --- documented EntityItem fields (interface 8 §8) ---
                "entity_id": ep.entity_id,
                "domain": ep.domain,
                "device_class": ep.device_class,
                "entity_category": ep.entity_category,
                # Live state value — the Skill keys disambiguation / range
                # authoring on the current state, not just its type.
                "state": _entity_live_state(hass, ep.entity_id),
                "state_type": ep.state_type,
                "unit": ep.unit,
                "readable": ep.readable,
                "writable": ep.writable,
                # Full attribute dict WITH VALUES (not just key names); enum /
                # list attributes carry their complete option range so the Skill
                # can score and author converter_config.
                "attributes": _safe_attributes(ep.attributes),
                "supported_features_int": ep.ha_supported_features_int,
                # --- additive enrichment (consumed by the cloud Skill) ---
                **(
                    {"capabilities": caps}
                    if (caps := _extract_capabilities(ep.attributes))
                    else {}
                ),
                **(
                    {"services": _get_or_fetch_services(service_cache, descriptions, ep.domain)}
                    if descriptions is not None
                    else {}
                ),
            }
            for ep in visible_entities
        ],
    }


def _safe_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe, size-bounded copy of an entity's attributes.

    Keeps values (unlike the old key-only serialization) so the Skill sees
    actual enum lists / scalars, but drops oversized lists (>64 items) and
    non-primitive nested structures that would bloat the upload.
    """
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, (list, tuple)):
            items = list(value)
            if len(items) <= 64 and all(
                isinstance(v, (str, int, float, bool)) for v in items
            ):
                out[key] = items
            else:
                out[key] = f"<list:{len(items)}>"
        elif isinstance(value, dict):
            out[key] = {
                k: v
                for k, v in value.items()
                if isinstance(v, (str, int, float, bool)) or v is None
            }
        else:
            out[key] = str(value)
    return out


def _get_or_fetch_services(
    cache: dict[str, list[dict[str, Any]]],
    descriptions: dict[str, Any] | None,
    domain: str,
) -> list[dict[str, Any]]:
    """Return domain's services, populating *cache* on first access."""
    if domain not in cache:
        cache[domain] = _get_entity_services(descriptions, domain)
    return cache[domain]


def _normalize_signal(
    result: PidInferResult,
) -> tuple[str, dict[str, Any] | None]:
    """Map a PidInferResult's signal to (skill_signal, assignment_failure).

    The inference engine emits a string signal for pure-inference cases
    (filter_empty / score_low / margin_tight / insufficient_domain_coverage…).
    The route-resolution stage (pidspec_bridge) demotes already-matched devices
    by stuffing a dict ``{matched_pid, unresolved_dps}`` into
    ``low_confidence_signal``; that maps to interface 8's ``carrier_failed``
    signal + an ``assignment_failure`` object (carried additively — the
    documented InferResult has no field for it, but it preserves the
    DP-assignment context the cloud Skill needs to route a carrier fix).
    """
    sig = result.low_confidence_signal
    if isinstance(sig, dict):
        unresolved = sig.get("unresolved_dps") or []
        matched_pid = sig.get("matched_pid")
        return "carrier_failed", {
            "reason": "unresolved_dps",
            "detail": (
                f"matched {matched_pid} but DP route(s) {unresolved} "
                "could not be assigned to any entity"
            ),
            "unresolved_dps": list(unresolved),
        }
    return (sig or "unknown"), None


def _build_top_candidates(result: PidInferResult) -> list[dict[str, Any]]:
    """Build interface 8's ``top_candidates[]`` from the scored candidate list.

    Each ``Candidate`` carries ``product_id`` / ``score`` (rounded to int — the
    contract types ``score`` as Integer) and a free-form ``match_detail`` map.
    The per-candidate scoring breakdown (factor contributions + deductions) is
    folded into ``match_detail`` — the documented home for scoring evidence —
    so the cloud Skill's decision routing can read why each candidate scored as
    it did without any out-of-band fields.
    """
    out: list[dict[str, Any]] = []
    for spec, score in result.candidates[:5]:
        detail: dict[str, Any] = dict(result.match_details.get(spec.product_id, {}))
        breakdown = result.scoring_breakdown.get(spec.product_id)
        if breakdown:
            detail["scoring_breakdown"] = breakdown
        out.append(
            {
                "product_id": spec.product_id,
                "category_code": spec.category_code,
                "score": int(round(score)),
                "match_detail": detail or {"note": ""},
            }
        )
    return out


async def build_low_confidence_report_payload(
    device_profiles: dict[str, DeviceProfile],
    low_confidence: dict[str, PidInferResult],
    hass: Any | None = None,
) -> list[dict[str, Any]]:
    """Build the ``devices[]`` list (Fusion ``DeviceItem``) for the report.

    Each entry matches interface 8's ``DeviceItem`` contract (see ``HA接口文档``
    §8 and ``docs/cloud_inference_architecture.md`` §4.4) so the Fusion service
    accepts it without field fabrication and the cloud Skill's
    ``decision.determine_action_hint`` can route on it:

    .. code-block:: json

        {
          "ha_device_id": "...",
          "name": "...", "model": "...", "manufacturer": "...", "area": "...",
          "entities": [ EntityItem, ... ],
          "infer_result": {
            "signal": "margin_tight",
            "top_candidates": [{"product_id", "score", "match_detail": {...}}],
            "margin": 2,
            "threshold": 8
          }
        }

    The request envelope (``request_id`` / ``gateway_id`` / ``local_rule_version``
    / ``trigger``) wraps this list and is built by the cloud client. The Skill's
    ``context`` block (existing_pidspecs / dpcode_reference / feature_rules /
    component_rules) is injected cloud-side and is intentionally NOT produced
    here.

    ``infer_result`` additionally carries ``hard_filter_passed`` /
    ``hard_filter_rejected`` (not in the documented InferResult, but consumed by
    the Skill's decision routing) and ``assignment_failure`` for the
    ``carrier_failed`` signal. These are tolerated additive keys — the Fusion
    mock logs the full request and only validates the documented required keys.

    When *hass* is provided, each entity is enriched with live ``state``, the
    device ``area`` is resolved, and per-domain HA ``services`` metadata is
    attached so the Skill can infer converter_config for DP → service bindings.
    """
    descriptions = await _fetch_service_descriptions(hass)
    payload: list[dict[str, Any]] = []
    for device_id, result in low_confidence.items():
        profile = device_profiles.get(device_id)
        signal, assignment_failure = _normalize_signal(result)

        if profile is not None:
            entry = _serialize_device_item(
                device_id, profile, descriptions=descriptions, hass=hass
            )
        else:
            entry = {
                "ha_device_id": device_id,
                "name": "", "model": "", "manufacturer": "",
                "area": None, "entities": [],
            }

        infer_result: dict[str, Any] = {
            "signal": signal,
            "top_candidates": _build_top_candidates(result),
            # Additive diagnostics — the Skill's decision routing keys on these.
            "hard_filter_passed": result.hard_filter_passed,
            "hard_filter_rejected": result.hard_filter_rejected,
            "reason": result.reason,
        }
        if result.margin is not None:
            infer_result["margin"] = int(round(result.margin))
        if result.threshold is not None:
            infer_result["threshold"] = int(round(result.threshold))
        if assignment_failure is not None:
            infer_result["assignment_failure"] = assignment_failure

        entry["infer_result"] = infer_result
        payload.append(entry)
    return payload


@dataclass
class InferenceResults:
    """Aggregated inference results for a batch of devices."""

    matched: dict[str, PidInferResult] = field(default_factory=dict)
    low_confidence: dict[str, PidInferResult] = field(default_factory=dict)
    rules_refreshed: bool = False


class CloudRuleClient(Protocol):
    """Protocol for cloud rule fetching (to be implemented by cloud_client)."""

    async def async_get_rule_version(self) -> int:
        """Fetch the current rule version from cloud."""
        ...

    async def async_fetch_rules_bundle(self) -> dict[str, Any]:
        """Fetch the full rules bundle from cloud."""
        ...

    async def async_report_low_confidence(
        self,
        devices: list[dict[str, Any]],
        *,
        local_rule_version: int,
        trigger: str,
    ) -> None:
        """Report low-confidence devices to cloud for LLM repair."""
        ...


def _run_inference_batch(
    cache: LocalRuleCache,
    device_profiles: dict[str, DeviceProfile],
) -> tuple[dict[str, PidInferResult], dict[str, PidInferResult]]:
    """Run inference for all device profiles.

    Returns (matched, low_confidence) dicts keyed by ha_device_id.
    """
    matched: dict[str, PidInferResult] = {}
    low_confidence: dict[str, PidInferResult] = {}

    for device_id, profile in device_profiles.items():
        result = infer_pid(profile, cache.pidspecs)
        if result.status == "matched":
            matched[device_id] = result
        else:
            low_confidence[device_id] = result

    return matched, low_confidence


async def async_infer_with_refresh(
    cache: LocalRuleCache,
    device_profiles: dict[str, DeviceProfile],
    cloud_client: CloudRuleClient | None = None,
    hass: Any | None = None,
    trigger: str = "plugin_start",
) -> InferenceResults:
    """Run inference with optional cloud rule refresh for low-confidence cases.

    Flow:
    1. Infer all devices against current cache
    2. If no low-confidence → done
    3. If cloud_client available:
       a. Check cloud rule version
       b. If newer → fetch + load bundle → re-infer low-confidence devices
    4. Remaining low-confidence devices → report to cloud
    """
    results = InferenceResults()

    # Step 1: Initial inference
    matched, low_confidence = _run_inference_batch(cache, device_profiles)
    results.matched = matched

    if not low_confidence:
        return results

    LOGGER.debug(
        "orchestrator: %d devices matched, %d low-confidence after initial inference",
        len(matched),
        len(low_confidence),
    )

    # Step 2: Try rule refresh if cloud client available
    if cloud_client is not None:
        try:
            cloud_version = await cloud_client.async_get_rule_version()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("orchestrator: failed to check cloud rule version: %s", exc)
            cloud_version = 0

        if cloud_version > cache.rule_version:
            LOGGER.info(
                "orchestrator: cloud rule version %d > local %d, fetching update",
                cloud_version,
                cache.rule_version,
            )
            try:
                bundle = await cloud_client.async_fetch_rules_bundle()
                if cache.load_from_bundle(bundle):
                    results.rules_refreshed = True
                    # Persist updated rules to disk for next restart
                    from ..rules import async_save_rules_bundle
                    await async_save_rules_bundle(bundle)
                    LOGGER.info(
                        "orchestrator: rules updated to version %d (%d pidspecs)",
                        cache.rule_version,
                        len(cache.pidspecs),
                    )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("orchestrator: failed to fetch rules bundle: %s", exc)

        # Step 3: Re-infer only the low-confidence devices
        if results.rules_refreshed:
            lc_profiles = {
                did: device_profiles[did] for did in low_confidence
            }
            re_matched, still_low = _run_inference_batch(cache, lc_profiles)
            results.matched.update(re_matched)
            low_confidence = still_low

            LOGGER.debug(
                "orchestrator: after refresh, %d newly matched, %d still low-confidence",
                len(re_matched),
                len(still_low),
            )

    # Step 4: Report remaining low-confidence devices
    results.low_confidence = low_confidence

    if low_confidence and cloud_client is not None:
        report_payload = await build_low_confidence_report_payload(
            device_profiles, low_confidence, hass=hass
        )
        try:
            await cloud_client.async_report_low_confidence(
                report_payload,
                local_rule_version=cache.rule_version,
                trigger=trigger,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("orchestrator: failed to report low-confidence: %s", exc)

    return results
