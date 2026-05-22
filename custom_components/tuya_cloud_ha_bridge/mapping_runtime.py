"""Runtime helpers for Tuya ↔ Home Assistant mapping modules."""

from __future__ import annotations

import asyncio
import importlib
import time
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.storage import Store

from .const import DOMAIN, DOMAIN_PRIORITY_ORDER, LOGGER

_STORAGE_VERSION = 2
_STORAGE_KEY = f"{DOMAIN}.pid_mapping"

# ---------------------------------------------------------------------------
# CategoryPidEntry: a single category candidate under each domain
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CategoryPidEntry:
    """A single category→PID entry within a domain."""

    category_code: str   # Tuya category code ("fs"/"kqjhq" etc.), empty string means domain default
    product_id: str      # Tuya PID


# ---------------------------------------------------------------------------
# Category inference: keyword matching first, attribute scoring as fallback
# ---------------------------------------------------------------------------

# Keyword matching table: category → keywords in device name/model (case-insensitive)
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "wf_kj": [
        "净化器", "净化", "purifier", "air cleaner", "空气净化",
        "air purifier", "空净",
    ],
    "wf_ble_fs": [
        "风扇", "电扇", "fan", "ceiling fan", "tower fan",
        "台扇", "落地扇", "塔扇", "吊扇", "壁扇",
    ],
}

# Attribute scoring signatures (only used as fallback when keyword matching is inconclusive)
CATEGORY_SIGNATURES: dict[str, dict[str, Any]] = {
    "wf_kj": {
        "domain": "fan",
        "positive_attrs": {"percentage", "preset_mode"},
        "positive_preset_modes": {"manual", "auto", "comfortable", "silent", "turbo"},
        "negative_attrs": {"direction", "oscillating"},
        "weight": 0,
    },
    "wf_ble_fs": {
        "domain": "fan",
        "positive_attrs": {"direction", "oscillating", "percentage"},
        "positive_preset_modes": {"nature", "sleep", "normal", "baby"},
        "negative_attrs": set(),
        "weight": 0,
    },
}


# ---------------------------------------------------------------------------
# PID Mapping Cache — supports multiple categories per domain
# ---------------------------------------------------------------------------

_PID_MAPPING_CACHE: dict[str, list[CategoryPidEntry]] | None = None
_PID_MAPPING_LOAD_LOCK = asyncio.Lock()
_MAPPING_MODULE_CACHE: dict[str, ModuleType | None] = {}
_MAPPING_MODULE_LOAD_LOCK = asyncio.Lock()


def _get_pid_mapping_store(hass: HomeAssistant) -> Store:
    """Return the storage helper used for PID mapping cache."""
    return Store(
        hass,
        _STORAGE_VERSION,
        _STORAGE_KEY,
        private=True,
    )


def _reorder_by_priority(
    mapping: dict[str, list[CategoryPidEntry]],
) -> dict[str, list[CategoryPidEntry]]:
    """Reorder a domain→entries mapping dict according to DOMAIN_PRIORITY_ORDER.

    Higher-priority domains (later in DOMAIN_PRIORITY_ORDER) come first,
    followed by any remaining domains in their original order.
    """
    ordered: dict[str, list[CategoryPidEntry]] = {}
    for domain in reversed(DOMAIN_PRIORITY_ORDER):
        if domain in mapping:
            ordered[domain] = mapping[domain]
    for domain, entries in mapping.items():
        if domain not in ordered:
            ordered[domain] = entries
    return ordered


def _cache_to_store_format(
    cache: dict[str, list[CategoryPidEntry]],
) -> dict[str, Any]:
    """Serialize the in-memory cache to a JSON-safe dict for storage."""
    result: dict[str, Any] = {}
    for domain, entries in cache.items():
        result[domain] = [
            {"category_code": e.category_code, "product_id": e.product_id} for e in entries
        ]
    return result


def _store_format_to_cache(
    data: dict[str, Any],
) -> dict[str, list[CategoryPidEntry]] | None:
    """Deserialize stored data into the in-memory cache format.

    Supports both v2 format (lists of dicts) and v1 legacy format (str values).
    """
    if not isinstance(data, dict) or not data:
        return None
    result: dict[str, list[CategoryPidEntry]] = {}
    for domain, value in data.items():
        if not isinstance(domain, str) or not domain:
            continue
        if isinstance(value, list):
            # v2 format
            entries = []
            for item in value:
                if isinstance(item, dict):
                    cat = item.get("category_code", "")
                    pid = item.get("product_id", "")
                    if pid:
                        entries.append(CategoryPidEntry(category_code=str(cat), product_id=str(pid)))
            if entries:
                result[domain] = entries
        elif isinstance(value, str) and value:
            # v1 legacy format: domain → pid (no category_code)
            result[domain] = [CategoryPidEntry(category_code="", product_id=value)]
    return _reorder_by_priority(result) if result else None


async def _async_load_pid_mapping_from_store(
    hass: HomeAssistant,
) -> dict[str, list[CategoryPidEntry]] | None:
    """Load cached PID mapping from local store."""
    store_data = await _get_pid_mapping_store(hass).async_load()
    if isinstance(store_data, dict) and store_data:
        return _store_format_to_cache(store_data)
    return None


async def _async_save_pid_mapping_to_store(
    hass: HomeAssistant, mapping: dict[str, list[CategoryPidEntry]]
) -> None:
    """Persist PID mapping to local store."""
    await _get_pid_mapping_store(hass).async_save(_cache_to_store_format(mapping))


async def async_load_pid_mapping_from_cloud(
    hass: HomeAssistant, api_key: str
) -> bool:
    """Fetch category-PID mappings from cloud, persist to store, and update cache.

    Returns True if the cloud mapping was loaded successfully.
    Falls back to the local store cache on failure.
    Always clears the in-memory cache first so a fresh cloud fetch is attempted.
    """
    global _PID_MAPPING_CACHE

    # Clear cache so this call always tries the cloud, not short-circuits.
    _PID_MAPPING_CACHE = None

    from .tuya_openapi import async_get_category_pid_mappings

    try:
        mappings = await async_get_category_pid_mappings(hass, api_key)
    except Exception:
        LOGGER.warning("Failed to fetch cloud category-PID mappings, using local cache")
        await async_ensure_pid_mapping_loaded(hass)
        return False

    if not mappings:
        LOGGER.warning("Cloud returned empty category-PID mappings, using local cache")
        await async_ensure_pid_mapping_loaded(hass)
        return False

    # Build new cache: group by domain (m.code), supporting multiple categories per domain
    new_cache: dict[str, list[CategoryPidEntry]] = {}
    for m in mappings:
        domain = m.code
        category_code = getattr(m, "category_code", "") or ""
        entry = CategoryPidEntry(category_code=category_code, product_id=m.product_id)
        new_cache.setdefault(domain, []).append(entry)

    new_cache = _reorder_by_priority(new_cache)

    async with _PID_MAPPING_LOAD_LOCK:
        _PID_MAPPING_CACHE = new_cache

    await _async_save_pid_mapping_to_store(hass, new_cache)

    LOGGER.debug(
        "Loaded %s category-PID mappings from cloud: %s",
        sum(len(v) for v in new_cache.values()),
        {d: [(e.category_code, e.product_id) for e in es] for d, es in new_cache.items()},
    )
    return True


async def async_ensure_pid_mapping_loaded(hass: HomeAssistant) -> None:
    """Ensure PID mapping is available, loading from local store if needed."""
    global _PID_MAPPING_CACHE

    if _PID_MAPPING_CACHE is not None:
        return

    async with _PID_MAPPING_LOAD_LOCK:
        if _PID_MAPPING_CACHE is not None:
            return
        stored = await _async_load_pid_mapping_from_store(hass)
        if stored:
            _PID_MAPPING_CACHE = stored
            LOGGER.debug(
                "Loaded %s domain PID mappings from local store: %s",
                len(stored),
                list(stored),
            )


def get_product_id_for_domain(domain: str) -> str | None:
    """Return the first configured Tuya product ID for a domain.

    For domains with multiple categories, returns the first entry's PID.
    Use get_category_candidates_for_domain() for full candidate list.
    """
    if _PID_MAPPING_CACHE is None:
        return None
    entries = _PID_MAPPING_CACHE.get(domain)
    if not entries:
        return None
    return entries[0].product_id


def get_category_candidates_for_domain(
    domain: str,
) -> list[CategoryPidEntry]:
    """Return all category→PID candidates for a domain."""
    if _PID_MAPPING_CACHE is None:
        return []
    return list(_PID_MAPPING_CACHE.get(domain, []))


def get_supported_domains_in_order() -> tuple[str, ...]:
    """Return supported domains in configured priority order."""
    if _PID_MAPPING_CACHE is None:
        return ()
    return tuple(_PID_MAPPING_CACHE)


def select_preferred_domain(domains: set[str]) -> str | None:
    """Return the preferred mapped domain using config order."""
    for supported_domain in get_supported_domains_in_order():
        if supported_domain in domains:
            return supported_domain
    return None


def infer_domain_for_device(entity_domains: set[str]) -> str | None:
    """Infer the best domain for a device using priority-first logic.

    DOMAIN_PRIORITY_ORDER is ordered low→high: later entries win.

    1. Pick the highest-priority domain from DOMAIN_PRIORITY_ORDER that
       the device actually exposes (last match wins).
    2. If that domain has a cloud PID mapping, return it.
    3. Otherwise return None — do NOT fall back to a lower-priority domain.
    """
    selected: str | None = None
    for domain in DOMAIN_PRIORITY_ORDER:
        if domain in entity_domains:
            selected = domain

    if selected is None:
        LOGGER.debug(
            "infer_domain_for_device: no domain in DOMAIN_PRIORITY_ORDER matches %s",
            entity_domains,
        )
        return None

    # Only proceed if cloud PID mapping exists for this domain.
    pid = get_product_id_for_domain(selected)
    if pid is None:
        LOGGER.debug(
            "infer_domain_for_device: domain=%s has no PID mapping, cache keys=%s",
            selected, list(_PID_MAPPING_CACHE.keys()) if _PID_MAPPING_CACHE else "None",
        )
        return None

    return selected


def infer_category_for_entity(
    hass: HomeAssistant,
    entity_id: str,
    candidates: list[CategoryPidEntry],
    *,
    device_name: str | None = None,
    device_model: str | None = None,
    device_manufacturer: str | None = None,
) -> CategoryPidEntry | None:
    """Pick the best category from candidates for a device/entity.

    Inference priority:
    1. device.model keyword matching (most stable, hardcoded in the integration)
    2. device.name keyword matching (fairly stable, users may rename but unlikely)
    3. device.manufacturer to narrow scope (extensible in the future)
    4. entity attribute scoring as fallback (attributes can be customized by integrations, least reliable)

    Single candidate: return directly (no inference needed).
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # --- Phase 1: keyword matching ---
    keyword_match = _match_category_by_keywords(candidates, device_model, device_name)
    if keyword_match is not None:
        return keyword_match

    # --- Phase 2: attribute scoring fallback ---
    return _score_category_by_attributes(hass, entity_id, candidates)


def _match_category_by_keywords(
    candidates: list[CategoryPidEntry],
    device_model: str | None,
    device_name: str | None,
) -> CategoryPidEntry | None:
    """Match category by keyword lookup in device model/name.

    Checks model first (more stable), then name. Returns the first
    unambiguous match, or None if no match / ambiguous.
    """
    # Try model first, then name
    for text in (device_model, device_name):
        if not text:
            continue
        text_lower = text.lower()
        matched: CategoryPidEntry | None = None
        match_count = 0
        for entry in candidates:
            keywords = CATEGORY_KEYWORDS.get(entry.category_code, [])
            for kw in keywords:
                if kw.lower() in text_lower:
                    matched = entry
                    match_count += 1
                    break  # one keyword hit is enough per candidate
        # Only return if exactly one candidate matched (unambiguous)
        if match_count == 1 and matched is not None:
            return matched
    return None


def _score_category_by_attributes(
    hass: HomeAssistant,
    entity_id: str,
    candidates: list[CategoryPidEntry],
) -> CategoryPidEntry | None:
    """Score candidates by entity attributes as fallback inference."""
    state = hass.states.get(entity_id)
    if not state:
        return candidates[0]

    entity_attrs = set(state.attributes.keys())
    entity_preset_modes: set[str] = set()
    raw_presets = state.attributes.get("preset_modes")
    if isinstance(raw_presets, (list, tuple)):
        entity_preset_modes = {str(m) for m in raw_presets}

    best_entry: CategoryPidEntry | None = None
    best_score: float = -999

    for entry in candidates:
        sig = CATEGORY_SIGNATURES.get(entry.category_code)
        if not sig:
            score = 0.0
        else:
            score = float(sig.get("weight", 0))
            for attr in sig.get("positive_attrs", set()):
                if attr in entity_attrs:
                    score += 2
            for mode in sig.get("positive_preset_modes", set()):
                if mode in entity_preset_modes:
                    score += 3
            for attr in sig.get("negative_attrs", set()):
                if attr in entity_attrs:
                    score -= 4

        if score > best_score:
            best_score = score
            best_entry = entry

    return best_entry


# ---------------------------------------------------------------------------
# Mapping module loading (supports domain + category)
# ---------------------------------------------------------------------------

def _load_mapping_module_sync(
    domain: str, category_code: str | None = None
) -> ModuleType | None:
    """Import the mapping module synchronously (for executor use only).

    When *category_code* is provided, first tries ``mapping/{domain}_{category_code}.py``;
    falls back to ``mapping/{domain}.py`` if the specialised module does not exist.
    """
    # 1. Try the specialized domain_category_code mapping first
    if category_code:
        key = f"{domain}_{category_code}"
        if key not in _MAPPING_MODULE_CACHE:
            try:
                module = importlib.import_module(f".mapping.{key}", __package__)
            except ModuleNotFoundError:
                module = None
            _MAPPING_MODULE_CACHE[key] = module
        if _MAPPING_MODULE_CACHE[key] is not None:
            return _MAPPING_MODULE_CACHE[key]

    # 2. Fall back to domain.py
    if domain not in _MAPPING_MODULE_CACHE:
        try:
            module = importlib.import_module(f".mapping.{domain}", __package__)
        except ModuleNotFoundError:
            module = None
        _MAPPING_MODULE_CACHE[domain] = module
    return _MAPPING_MODULE_CACHE[domain]


def _load_mapping_module(
    domain: str, category_code: str | None = None
) -> ModuleType | None:
    """Return cached mapping module. Never calls import_module (event-loop safe).

    All modules must be pre-loaded via async_ensure_mapping_module_loaded().
    """
    if category_code:
        key = f"{domain}_{category_code}"
        cached = _MAPPING_MODULE_CACHE.get(key)
        if cached is not None:
            return cached

    return _MAPPING_MODULE_CACHE.get(domain)


def has_mapping_module(domain: str, category_code: str | None = None) -> bool:
    """Return True if a mapping module is already loaded in cache."""
    cached = _load_mapping_module(domain, category_code)
    return cached is not None


async def async_ensure_mapping_module_loaded(
    hass: HomeAssistant, domain: str, category_code: str | None = None
) -> ModuleType | None:
    """Load a mapping module without blocking the event loop."""
    # Fast path: check specialized category_code cache and domain fallback cache
    cache_key = f"{domain}_{category_code}" if category_code else domain
    if cache_key in _MAPPING_MODULE_CACHE:
        return _MAPPING_MODULE_CACHE[cache_key]
    if category_code and domain in _MAPPING_MODULE_CACHE:
        pass  # category_code module not cached yet, need to attempt loading

    async with _MAPPING_MODULE_LOAD_LOCK:
        if cache_key in _MAPPING_MODULE_CACHE:
            return _MAPPING_MODULE_CACHE[cache_key]
        module = await hass.async_add_executor_job(
            _load_mapping_module_sync, domain, category_code
        )
        return module


def build_service_calls_from_tuya(
    domain: str, tuya_data: dict[str, Any], entity_id: str,
    category_code: str | None = None,
    context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Convert Tuya payload data into HA service call payloads."""
    if not (mapping_module := _load_mapping_module(domain, category_code)):
        return []

    if not hasattr(mapping_module, "tuya_to_ha"):
        return []

    try:
        mapped_calls = mapping_module.tuya_to_ha(tuya_data, entity_id, context=context)
    except Exception as err:
        LOGGER.warning("Failed to map Tuya payload for domain %s: %s", domain, err)
        return []

    if isinstance(mapped_calls, dict):
        mapped_calls = [mapped_calls]
    elif isinstance(mapped_calls, list):
        mapped_calls = [call for call in mapped_calls if isinstance(call, dict)]
    else:
        return []

    # Flatten nested service dicts (e.g. mode_service, fan_service) into top-level calls.
    _NESTED_SERVICE_KEYS = (
        "mode_service",
        "preset_service",
        "fan_service",
        "swing_service",
        "direction_service",
        "oscillate_service",
        "suction_service",
    )
    result: list[dict[str, Any]] = []
    for call in mapped_calls:
        result.append(call)
        for key in _NESTED_SERVICE_KEYS:
            if isinstance(call.get(key), dict):
                result.append(call.pop(key))
    return result


def build_tuya_properties_from_state(
    domain: str,
    state: State,
    category_code: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Convert an HA state into Tuya property report payloads."""
    if not (mapping_module := _load_mapping_module(domain, category_code)):
        return {}

    if not hasattr(mapping_module, "ha_to_tuya"):
        return {}

    try:
        tuya_data = mapping_module.ha_to_tuya(
            {"state": state.state},
            dict(state.attributes),
            context=context,
        )
    except Exception as err:
        LOGGER.warning("Failed to map HA state for domain %s: %s", domain, err)
        return {}

    if not isinstance(tuya_data, dict):
        return {}

    now = int(time.time() * 1000)
    return {
        key: {"time": now, "value": value}
        for key, value in tuya_data.items()
        if isinstance(key, str)
    }


def get_property_metadata(
    domain: str, category_code: str | None = None
) -> dict[str, dict[str, Any]]:
    """Return property metadata (name, range) from a mapping module.

    Each mapping module may define TUYA_PROPERTY_METADATA, a dict keyed by
    property code with optional ``name`` and ``range`` fields.
    """
    if not (mapping_module := _load_mapping_module(domain, category_code)):
        return {}

    return getattr(mapping_module, "TUYA_PROPERTY_METADATA", {})
