"""Runtime helpers for Tuya ↔ Home Assistant mapping modules."""

from __future__ import annotations

import asyncio
import importlib
import time
from types import ModuleType
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.storage import Store

from .const import DOMAIN, DOMAIN_PRIORITY_ORDER, LOGGER

_STORAGE_VERSION = 1
_STORAGE_KEY = f"{DOMAIN}.pid_mapping"

_PID_MAPPING_CACHE: dict[str, str] | None = None
_PID_MAPPING_LOAD_LOCK = asyncio.Lock()
_MAPPING_MODULE_CACHE: dict[str, ModuleType | None] = {}
_MAPPING_MODULE_LOAD_LOCK = asyncio.Lock()


def _get_pid_mapping_store(hass: HomeAssistant) -> Store[dict[str, str]]:
    """Return the storage helper used for PID mapping cache."""
    return Store[dict[str, str]](
        hass,
        _STORAGE_VERSION,
        _STORAGE_KEY,
        private=True,
    )


def _reorder_by_priority(mapping: dict[str, str]) -> dict[str, str]:
    """Reorder a domain→PID mapping dict according to DOMAIN_PRIORITY_ORDER.

    Higher-priority domains (later in DOMAIN_PRIORITY_ORDER) come first,
    followed by any remaining domains in their original order.
    """
    ordered: dict[str, str] = {}
    for domain in reversed(DOMAIN_PRIORITY_ORDER):
        if domain in mapping:
            ordered[domain] = mapping[domain]
    for domain, pid in mapping.items():
        if domain not in ordered:
            ordered[domain] = pid
    return ordered


async def _async_load_pid_mapping_from_store(
    hass: HomeAssistant,
) -> dict[str, str] | None:
    """Load cached PID mapping from local store."""
    store_data = await _get_pid_mapping_store(hass).async_load()
    if isinstance(store_data, dict) and store_data:
        filtered = {
            k: v
            for k, v in store_data.items()
            if isinstance(k, str) and isinstance(v, str) and v
        }
        return _reorder_by_priority(filtered) if filtered else None
    return None


async def _async_save_pid_mapping_to_store(
    hass: HomeAssistant, mapping: dict[str, str]
) -> None:
    """Persist PID mapping to local store."""
    await _get_pid_mapping_store(hass).async_save(mapping)


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

    new_cache = _reorder_by_priority({m.code: m.product_id for m in mappings})

    async with _PID_MAPPING_LOAD_LOCK:
        _PID_MAPPING_CACHE = new_cache

    await _async_save_pid_mapping_to_store(hass, new_cache)

    LOGGER.debug(
        "Loaded %s category-PID mappings from cloud: %s",
        len(mappings),
        list(_PID_MAPPING_CACHE),
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
                "Loaded %s category-PID mappings from local store: %s",
                len(stored),
                list(stored),
            )


def get_product_id_for_domain(domain: str) -> str | None:
    """Return the configured Tuya product ID for a domain."""
    if _PID_MAPPING_CACHE is None:
        return None
    return _PID_MAPPING_CACHE.get(domain)


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
        return None

    # Only proceed if cloud PID mapping exists for this domain.
    if get_product_id_for_domain(selected) is None:
        return None

    return selected


def _load_mapping_module(domain: str) -> ModuleType | None:
    """Return the Python mapping module for a domain."""
    if domain in _MAPPING_MODULE_CACHE:
        return _MAPPING_MODULE_CACHE[domain]
    try:
        module = importlib.import_module(f".mapping.{domain}", __package__)
    except ModuleNotFoundError:
        module = None
    _MAPPING_MODULE_CACHE[domain] = module
    return module


def has_mapping_module(domain: str) -> bool:
    """Return True if a local mapping module exists for the domain."""
    return _load_mapping_module(domain) is not None


async def async_ensure_mapping_module_loaded(
    hass: HomeAssistant, domain: str
) -> ModuleType | None:
    """Load a mapping module without blocking the event loop."""
    if domain in _MAPPING_MODULE_CACHE:
        return _MAPPING_MODULE_CACHE[domain]

    async with _MAPPING_MODULE_LOAD_LOCK:
        if domain in _MAPPING_MODULE_CACHE:
            return _MAPPING_MODULE_CACHE[domain]
        module = await hass.async_add_executor_job(_load_mapping_module, domain)
        _MAPPING_MODULE_CACHE[domain] = module
        return module


def build_service_calls_from_tuya(
    domain: str, tuya_data: dict[str, Any], entity_id: str
) -> list[dict[str, Any]]:
    """Convert Tuya payload data into HA service call payloads."""
    if not (mapping_module := _load_mapping_module(domain)):
        return []

    if not hasattr(mapping_module, "tuya_to_ha"):
        return []

    try:
        mapped_calls = mapping_module.tuya_to_ha(tuya_data, entity_id)
    except Exception as err:
        LOGGER.warning("Failed to map Tuya payload for domain %s: %s", domain, err)
        return []

    if isinstance(mapped_calls, dict):
        mapped_calls = [mapped_calls]
    elif isinstance(mapped_calls, list):
        mapped_calls = [call for call in mapped_calls if isinstance(call, dict)]
    else:
        return []

    # Flatten nested service dicts (e.g. mode_service) into top-level calls.
    result: list[dict[str, Any]] = []
    for call in mapped_calls:
        result.append(call)
        if isinstance(call.get("mode_service"), dict):
            result.append(call.pop("mode_service"))
    return result


def build_tuya_properties_from_state(
    domain: str,
    state: State,
) -> dict[str, dict[str, Any]]:
    """Convert an HA state into Tuya property report payloads."""
    if not (mapping_module := _load_mapping_module(domain)):
        return {}

    if not hasattr(mapping_module, "ha_to_tuya"):
        return {}

    try:
        tuya_data = mapping_module.ha_to_tuya(
            {"state": state.state},
            dict(state.attributes),
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


def get_property_metadata(domain: str) -> dict[str, dict[str, Any]]:
    """Return property metadata (name, range) from a mapping module.

    Each mapping module may define TUYA_PROPERTY_METADATA, a dict keyed by
    property code with optional ``name`` and ``range`` fields.
    """
    if not (mapping_module := _load_mapping_module(domain)):
        return {}

    return getattr(mapping_module, "TUYA_PROPERTY_METADATA", {})
