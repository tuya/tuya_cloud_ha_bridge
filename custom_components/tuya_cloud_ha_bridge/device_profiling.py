"""Device profiling — builds DeviceProfile from HA device/entity registry.

Collects all entity information for a device and constructs the
DeviceProfile needed by the pidspec inference engine.

Public API:
- async_build_device_profile(hass, device_id, rule_cache) → DeviceProfile
- async_build_all_device_profiles(hass, device_ids, rule_cache) → dict[device_id, DeviceProfile]
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .pidspec.entity_filter import INFERENCE_EXCLUDED_DOMAINS, should_include_entity
from .pidspec.features import discover_features
from .pidspec.components import discover_component
from .pidspec.models import (
    DeviceProfile,
    EntityProfile,
    FeatureRules,
    LocalBaselineComponentRules,
)
from .pidspec.rule_cache import LocalRuleCache
from .const import LOGGER


def _get_entity_state_attributes(
    hass: HomeAssistant, entity_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (state_dict, attributes_dict) for an entity."""
    state = hass.states.get(entity_id)
    if state is None:
        return {"state": ""}, {}
    return {"state": state.state}, dict(state.attributes)


def _entity_is_readable(domain: str) -> bool:
    """Most HA entities are readable (have state)."""
    return True


def _entity_is_writable(domain: str) -> bool:
    """Entities in these domains support service calls (commands)."""
    return domain in {
        "switch", "light", "fan", "climate", "cover",
        "humidifier", "vacuum", "water_heater", "media_player",
        "lock", "siren", "valve", "button", "number", "select",
    }


def _build_entity_profile(
    hass: HomeAssistant,
    entity_entry: er.RegistryEntry,
    feature_rules: FeatureRules,
    local_component_rules: LocalBaselineComponentRules | None,
) -> EntityProfile:
    """Construct an EntityProfile from an HA entity registry entry."""
    entity_id = entity_entry.entity_id
    domain = entity_entry.domain
    integration = entity_entry.platform or ""

    state = hass.states.get(entity_id)
    attributes: dict[str, Any] = dict(state.attributes) if state else {}
    state_value = state.state if state else ""

    # Extract supported_features int
    ha_features_int = attributes.get("supported_features")
    if not isinstance(ha_features_int, int):
        ha_features_int = None

    # Determine device_class, state_class, entity_category
    device_class = (
        entity_entry.device_class
        or entity_entry.original_device_class
        or attributes.get("device_class")
    )
    state_class = attributes.get("state_class")
    entity_category = (
        entity_entry.entity_category.value if entity_entry.entity_category else None
    )

    # Build initial profile (component will be filled in below)
    profile = EntityProfile(
        entity_id=entity_id,
        domain=domain,
        integration=integration,
        friendly_name=entity_entry.name or entity_entry.original_name or "",
        original_name=entity_entry.original_name or "",
        unique_id=entity_entry.unique_id or "",
        device_class=device_class,
        state_class=state_class,
        entity_category=entity_category,
        unit=attributes.get("unit_of_measurement"),
        state_type=type(state_value).__name__ if state_value else "",
        readable=_entity_is_readable(domain),
        writable=_entity_is_writable(domain),
        notifiable=True,
        attributes=attributes,
        ha_supported_features_int=ha_features_int,
        supported_features=set(),
        component="",
        component_source="",
    )

    # Discover features using rules
    profile.supported_features = discover_features(profile, feature_rules)

    # Discover component
    if local_component_rules is not None:
        component, component_source = discover_component(
            profile, local_component_rules
        )
        profile.component = component
        profile.component_source = component_source
    else:
        profile.component = f"entity:{entity_id}"
        profile.component_source = "entity_fallback"

    return profile


def async_build_device_profile(
    hass: HomeAssistant,
    device_id: str,
    rule_cache: LocalRuleCache,
    local_component_rules: LocalBaselineComponentRules | None = None,
) -> DeviceProfile | None:
    """Build a DeviceProfile for a single HA device.

    Returns None if the device doesn't exist or has no entities.
    """
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    device = device_registry.async_get(device_id)
    if device is None:
        return None

    entity_entries = er.async_entries_for_device(entity_registry, device_id)
    if not entity_entries:
        return None

    entity_profiles: list[EntityProfile] = []
    domains: set[str] = set()

    for entry in entity_entries:
        if entry.disabled_by is not None:
            continue

        # Build a temporary EntityProfile (without features/component yet)
        # so we can run the global inclusion filter before doing extra work.
        temp_profile = EntityProfile(
            entity_id=entry.entity_id,
            domain=entry.domain,
            integration=entry.platform or "",
            friendly_name=entry.name or entry.original_name or "",
            original_name=entry.original_name or "",
            unique_id=entry.unique_id or "",
            # entry.device_class 是用户覆盖值，集成默认的 device_class 在
            # original_device_class；diagnostic 白名单依赖真实 device_class，
            # 必须 fallback，否则 battery/tamper 等实体会被当作 None 过滤掉。
            device_class=entry.device_class or entry.original_device_class,
            state_class=None,
            entity_category=(
                entry.entity_category.value if entry.entity_category else None
            ),
            unit=None,
            state_type="",
            readable=True,
            writable=entry.domain in {
                "switch", "light", "fan", "climate", "cover",
                "humidifier", "vacuum", "water_heater", "media_player",
                "lock", "siren", "valve", "button", "number", "select",
            },
            notifiable=True,
            attributes={},
            ha_supported_features_int=None,
            supported_features=set(),
            component="",
            component_source="",
        )

        # Global filter: EXCLUDED_DOMAINS / EXCLUDED_CATEGORIES /
        # CONDITIONAL_DOMAINS (default-excluded).
        # NOTE: At construction time we don't know which PidSpec will match,
        # so CONDITIONAL_DOMAINS is excluded unconditionally here. The report
        # serialization layer applies the same filter on the way out as a
        # safety net.
        if not should_include_entity(temp_profile):
            continue

        profile = _build_entity_profile(
            hass, entry, rule_cache.feature_rules, local_component_rules
        )
        entity_profiles.append(profile)
        # Inference-excluded domains (e.g. button) are kept in the profile so a
        # matched spec's DPs can still route to them, but MUST stay out of
        # domain_set so they never influence PID inference / scoring.
        if entry.domain not in INFERENCE_EXCLUDED_DOMAINS:
            domains.add(entry.domain)

    if not entity_profiles:
        return None

    return DeviceProfile(
        device_id=device_id,
        name=device.name_by_user or device.name or device_id,
        model=device.model or "",
        manufacturer=device.manufacturer or "",
        entity_profiles=entity_profiles,
        domain_set=frozenset(domains),
    )


def async_build_all_device_profiles(
    hass: HomeAssistant,
    device_ids: list[str],
    rule_cache: LocalRuleCache,
    local_component_rules: LocalBaselineComponentRules | None = None,
) -> dict[str, DeviceProfile]:
    """Build DeviceProfiles for multiple devices.

    Returns a dict keyed by device_id (only includes devices with profiles).
    """
    profiles: dict[str, DeviceProfile] = {}

    for device_id in device_ids:
        profile = async_build_device_profile(
            hass, device_id, rule_cache, local_component_rules
        )
        if profile is not None:
            profiles[device_id] = profile

    return profiles
