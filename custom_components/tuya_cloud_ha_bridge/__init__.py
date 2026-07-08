"""Tuya HA New Gateway: HA plugin gateway via Tuya Link MQTT."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import timedelta
import json
import time
from typing import Any

from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity import get_capability
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_API_KEY,
    EVENT_CORE_CONFIG_UPDATE,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CoreState, Event, HomeAssistant, State, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_SECRET,
    CONF_PRODUCT_ID,
    DOMAIN,
    LOGGER,
    TOPO_GET_PAGE_SIZE,
    TOPO_SYNC_INTERVAL_SECONDS,
    RULE_CHECK_INTERVAL_SECONDS,
    TUYA_LINK_TOPIC_DEVICE_UNBIND_NOTICE,
    TUYA_LINK_TOPIC_SUBDEVICE_DELETE,
    TUYA_LINK_TOPIC_SUBDEVICE_BIND_RESPONSE,
    TUYA_LINK_TOPIC_SUBDEVICE_DELETE_RESPONSE,
    TUYA_LINK_TOPIC_SUBDEVICE_TO_BIND,
    TUYA_LINK_TOPIC_TOPO_CHANGE,
    TUYA_LINK_TOPIC_TOPO_GET_RESPONSE,
)
from .region_mapping import get_region_endpoints_for_api_key
from .pidspec_bridge import (
    async_init_pidspec,
    find_route_table_by_entity,
    get_route_table,
    pidspec_build_full_device_properties,
    pidspec_build_service_calls,
)
from .storage import (
    async_delete_gateway_bindings,
    async_load_gateway_bindings,
    async_load_gateway_credentials,
    async_save_gateway_bindings,
)
from .services import (
    async_execute_service_calls,
    async_setup_services,
)
from .tuya_link_mqtt import TuyaLinkMqttClient

type TuyaHaNewConfigEntry = ConfigEntry[TuyaLinkMqttClient | None]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
REGISTRY_SYNC_DEBOUNCE_SECONDS = 1.0
# Per-entry structural fingerprint of inferable devices, keyed by entry_id.
# Used to skip registry-driven re-inference when no inference-relevant device
# profile actually changed (cosmetic registry churn → no re-infer / re-report).
_DEVICE_FINGERPRINTS: dict[str, dict[str, str]] = {}
_PENDING_GATEWAY_CLIENTS = "pending_gateway_clients"
_PENDING_BIND_CLIENTS = "pending_bind_clients"
_TOPO_SYNC_SESSIONS = "topo_sync_sessions"
# Delay (seconds) before the first topo sync after setup, so the HA frontend
# finishes rendering the config-flow completion dialog before any devices get
# associated with the config entry (which would trigger the "created devices"
# popup).
_INITIAL_SYNC_DELAY_SECONDS = 5
# Grace period after EVENT_HOMEASSISTANT_STARTED before kicking off the first
# topo/get. HOMEASSISTANT_STARTED fires when all integrations have finished
# async_setup_entry, but some (xiaomi_home, mqtt-discovered, …) still populate
# their entities' live state asynchronously for a few seconds after that —
# their entity_registry rows exist (persisted from a previous session) but
# `hass.states.get(entity_id).state` is still `unavailable` until the
# integration's first poll/push lands. If topo/get runs in that window, our
# offline gate (_device_has_available_entity) wrongly classifies the device as
# offline → restore-unbind → cloud unbinds a perfectly good high-confidence
# device. The grace period gives those integrations time to push first state.
_POST_STARTED_GRACE_SECONDS = 15
_TOPO_SYNC_SESSION_TIMEOUT_SECONDS = 30
_STATE_REPORT_DEBOUNCE_SECONDS = 0.15
_UNSET = object()


def _build_mapping_context(
    hass: HomeAssistant,
    runtime_data: TuyaLinkMqttClient | None = None,
    tuya_device_id: str | None = None,
) -> dict[str, Any]:
    """Build context dict for mapping functions (temperature_unit etc.)."""
    context: dict[str, Any] = {
        "temperature_unit": hass.config.units.temperature_unit
    }
    if (
        runtime_data is not None
        and isinstance(tuya_device_id, str)
        and tuya_device_id
        and hasattr(runtime_data, "get_last_child_cover_command")
    ):
        context["last_cover_control"] = runtime_data.get_last_child_cover_command(
            tuya_device_id
        )
    return context


def _mapping_temperature_unit_code(context: dict[str, Any] | None) -> str:
    """Return the active temperature unit code used by mapping metadata."""
    if context and context.get("temperature_unit") == "°F":
        return "f"
    return "c"


def _convert_temperature_value(
    value: Any,
    source_unit: str,
    target_unit: str,
) -> Any:
    """Convert a temperature value between Celsius and Fahrenheit."""
    if not isinstance(value, (int, float)):
        return value
    if source_unit == target_unit:
        return int(round(value))
    if source_unit == "c" and target_unit == "f":
        return int(round(value * 9 / 5 + 32))
    if source_unit == "f" and target_unit == "c":
        return int(round((value - 32) * 5 / 9))
    return value


def _resolve_property_dp_properties(
    hass: HomeAssistant,
    entity_id: str,
    code: str,
    metadata: dict[str, dict[str, Any]],
    context: dict[str, Any] | None = None,
    *,
    include_static: bool = True,
    include_range: bool = True,
) -> dict[str, Any] | None:
    """Resolve dpProperties for a Tuya property from mapping metadata."""
    meta = metadata.get(code, {})
    if not meta:
        return None

    target_unit = meta.get("unit")
    current_unit = _mapping_temperature_unit_code(context)

    dp_properties: dict[str, Any] = {}

    if include_static:
        static_dp_properties = meta.get("dp_properties")
        if isinstance(static_dp_properties, dict):
            dp_properties.update(static_dp_properties)

    range_value: Any | None = None
    if include_range:
        if "range_attr" in meta:
            cap_val = get_capability(hass, entity_id, meta["range_attr"])
            if isinstance(cap_val, list) and cap_val:
                range_value = cap_val
        elif "range" in meta:
            range_value = meta["range"]

        if range_value is not None:
            if "value_map" in meta and isinstance(range_value, list):
                range_value = [meta["value_map"].get(v, v) for v in range_value]
            dp_properties["range"] = range_value

    attr_map = meta.get("dp_properties_attr_map")
    if include_static and isinstance(attr_map, dict):
        for dp_key, attr_name in attr_map.items():
            cap_val = get_capability(hass, entity_id, attr_name)
            if cap_val is not None:
                if (
                    isinstance(target_unit, str)
                    and target_unit in {"c", "f"}
                    and attr_name in {"min_temp", "max_temp"}
                ):
                    cap_val = _convert_temperature_value(
                        cap_val, current_unit, target_unit
                    )
                dp_properties[str(dp_key)] = cap_val

    range_values = dp_properties.get("range")
    if "type" not in dp_properties and isinstance(range_values, (list, tuple)):
        dp_properties["type"] = "enum"

    return dp_properties or None


def _build_default_property_payload(
    hass: HomeAssistant,
    entity_id: str,
    code: str,
    metadata: dict[str, dict[str, Any]],
    context: dict[str, Any] | None = None,
    value: Any = _UNSET,
) -> dict[str, Any]:
    """Build a sub/bind defaultProperties item."""
    meta = metadata.get(code, {})
    prop: dict[str, Any] = {
        "code": code,
    }
    if "bind_value" in meta:
        prop["value"] = meta["bind_value"]
    elif value is not _UNSET:
        prop["value"] = value

    if dp_properties := _resolve_property_dp_properties(
        hass,
        entity_id,
        code,
        metadata,
        context,
        include_static=True,
        include_range=True,
    ):
        prop["dpProperties"] = dp_properties

    return prop


@dataclass(slots=True)
class _TopoSyncSession:
    """Track one in-flight paginated topo/get synchronization."""

    pending_msg_id: str
    current_start_id: int
    page_size: int
    records: list[dict[str, Any]] = field(default_factory=list)
    seen_device_ids: set[str] = field(default_factory=set)
    last_updated_monotonic: float = field(default_factory=time.monotonic)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Tuya HA integration."""
    hass.data.setdefault(DOMAIN, {})
    async_setup_services(hass)
    return True


def _get_pending_gateway_clients(hass: HomeAssistant) -> dict[str, TuyaLinkMqttClient]:
    """Return gateway clients waiting to be adopted by a config entry."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    pending_clients = domain_data.setdefault(_PENDING_GATEWAY_CLIENTS, {})
    return pending_clients


def _get_pending_bind_clients(hass: HomeAssistant) -> dict[str, set[str]]:
    """Return pending topo/add/notify client IDs by config entry ID."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    pending_bind_clients = domain_data.setdefault(_PENDING_BIND_CLIENTS, {})
    return pending_bind_clients


def _async_mark_pending_bind_clients(
    hass: HomeAssistant, entry_id: str, client_ids: set[str]
) -> None:
    """Mark client IDs as pending cloud-confirmed bind additions."""
    if not client_ids:
        return
    pending_bind_clients = _get_pending_bind_clients(hass).setdefault(
        entry_id, set()
    )
    pending_bind_clients.update(client_ids)


def _get_topo_sync_sessions(hass: HomeAssistant) -> dict[str, _TopoSyncSession]:
    """Return active topo/get pagination sessions by config entry ID."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    topo_sync_sessions = domain_data.setdefault(_TOPO_SYNC_SESSIONS, {})
    return topo_sync_sessions


def async_store_pending_gateway_client(
    hass: HomeAssistant, device_id: str, client: TuyaLinkMqttClient
) -> None:
    """Store a connected gateway client for the next config entry setup."""
    _get_pending_gateway_clients(hass)[device_id] = client


def async_take_pending_gateway_client(
    hass: HomeAssistant, device_id: str
) -> TuyaLinkMqttClient | None:
    """Take a pending gateway client if one was handed off by the flow."""
    return _get_pending_gateway_clients(hass).pop(device_id, None)


async def async_initial_sync_gateway_devices(
    hass: HomeAssistant, entry: TuyaHaNewConfigEntry
) -> None:
    """Run the first registry sync for the gateway entry."""
    await async_sync_gateway_devices_for_entry(hass, entry)


async def async_sync_gateway_devices_for_entry(
    hass: HomeAssistant, entry: TuyaHaNewConfigEntry
) -> None:
    """Run a full registry sync for the gateway entry."""
    from .registry_sync import async_sync_gateway_devices

    await async_sync_gateway_devices(hass, entry)


async def async_request_topo_sync(
    hass: HomeAssistant, entry: TuyaHaNewConfigEntry
) -> None:
    """Request paginated cloud topo/get to reconcile bindings by client ID."""
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None or not hasattr(runtime_data, "publish_topo_get"):
        LOGGER.debug("Cannot request topo sync: no MQTT client")
        return

    topo_sync_sessions = _get_topo_sync_sessions(hass)
    if current_session := topo_sync_sessions.get(entry.entry_id):
        session_age = (
            time.monotonic() - current_session.last_updated_monotonic
        )
        if session_age < _TOPO_SYNC_SESSION_TIMEOUT_SECONDS:
            LOGGER.debug(
                "Skipping topo sync request: pagination still in progress "
                "(entry_id=%s, msgId=%s, age=%.1fs)",
                entry.entry_id,
                current_session.pending_msg_id,
                session_age,
            )
            return
        LOGGER.debug(
            "Dropping stale topo sync session (entry_id=%s, msgId=%s, age=%.1fs)",
            entry.entry_id,
            current_session.pending_msg_id,
            session_age,
        )
        topo_sync_sessions.pop(entry.entry_id, None)

    msg_id = runtime_data.publish_topo_get(
        start_id=0,
        page_size=TOPO_GET_PAGE_SIZE,
    )
    if not msg_id:
        return

    topo_sync_sessions[entry.entry_id] = _TopoSyncSession(
        pending_msg_id=msg_id,
        current_start_id=0,
        page_size=TOPO_GET_PAGE_SIZE,
    )

async def _async_report_subdevices_online(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    client: TuyaLinkMqttClient,
    child_device_ids: list[str] | None = None,
) -> None:
    """Report bound child devices online through the gateway.

    Only devices whose HA entities are currently available are reported
    online; unavailable devices are reported offline so the Tuya cloud
    status stays in sync with HA.
    """
    if entry.runtime_data is not client:
        return

    bindings = await async_load_gateway_bindings(hass, entry.entry_id)
    if not bindings:
        return

    if child_device_ids is None:
        candidate_devices = bindings.get("devices", {})
    else:
        candidate_ids = set(child_device_ids)
        candidate_devices = {
            dev_id: dev_data
            for dev_id, dev_data in bindings.get("devices", {}).items()
            if isinstance(dev_data, dict)
            and dev_data.get("tuya_device_id") in candidate_ids
        }

    online_ids: list[str] = []
    offline_ids: list[str] = []
    for device_id, device_data in candidate_devices.items():
        if not isinstance(device_data, dict):
            continue
        tuya_device_id = device_data.get("tuya_device_id")
        if not isinstance(tuya_device_id, str) or not tuya_device_id:
            continue
        if _device_is_available_for_bindings(hass, bindings, device_id):
            online_ids.append(tuya_device_id)
        else:
            offline_ids.append(tuya_device_id)

    if online_ids:
        client.publish_subdevice_login(list(dict.fromkeys(online_ids)))
    if offline_ids:
        client.publish_subdevice_logout(list(dict.fromkeys(offline_ids)))


async def _async_report_all_bound_device_states(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    client: TuyaLinkMqttClient,
) -> None:
    """Report current state of all bound devices to Tuya cloud.

    Called on HA startup, MQTT reconnect, and periodically to keep
    the Tuya cloud in sync with HA device states.
    """
    if entry.runtime_data is not client:
        return

    bindings = await async_load_gateway_bindings(hass, entry.entry_id)
    if not bindings:
        return

    devices = bindings.get("devices", {})
    entities = bindings.get("entities", {})
    reported = 0

    for device_data in devices.values():
        tuya_device_id = device_data.get("tuya_device_id")
        if not isinstance(tuya_device_id, str) or not tuya_device_id:
            continue

        context = _build_mapping_context(hass, client, tuya_device_id)

        # Primary path: assemble the COMPLETE PID payload from all bound
        # entities' live states (Tuya expects PID-level full-state reports).
        properties = pidspec_build_full_device_properties(
            hass, tuya_device_id, context=context
        )

        if properties is None:
            # Binding is pidspec-gated: a device only enters this loop if its
            # PidSpec resolved (route table stored at bind). None here means the
            # route table went missing — skip rather than fall back to the
            # retired legacy mapping layer.
            LOGGER.warning(
                "report: no pidspec route table for tuya_device_id=%s product_id=%s; "
                "skipping full report (legacy mapping retired)",
                tuya_device_id,
                device_data.get("product_id"),
            )
            continue

        if not properties:
            continue

        # Keep the dedup snapshot in sync so the next change-driven flush does
        # not re-send an identical full payload.
        route_table = get_route_table(hass, tuya_device_id)
        if route_table is not None:
            route_table.last_reported_snapshot = _property_values(properties)

        await hass.async_add_executor_job(
            client.publish_child_property_report,
            tuya_device_id,
            properties,
        )
        reported += 1

    LOGGER.debug("Full state report completed: %s devices reported", reported)


def _state_is_available(state: State | None) -> bool:
    """Return if an entity state means the device is online."""
    return state is not None and state.state != STATE_UNAVAILABLE


def _state_can_be_reported(state: State | None) -> bool:
    """Return if an entity state can be mapped to a Tuya property report."""
    return state is not None and state.state not in {STATE_UNAVAILABLE, STATE_UNKNOWN}


def _property_values(properties: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return comparable Tuya property values without timestamps."""
    return {
        key: value["value"]
        for key, value in properties.items()
        if isinstance(value, dict) and "value" in value
    }


def _device_primary_entity_id(device_data: dict[str, Any]) -> str | None:
    """Return the primary bound entity ID for a device."""
    primary_entity_id = device_data.get("primary_entity_id")
    if isinstance(primary_entity_id, str) and primary_entity_id:
        return primary_entity_id

    entity_ids = device_data.get("entity_ids")
    if isinstance(entity_ids, list):
        for entity_id in entity_ids:
            if isinstance(entity_id, str) and entity_id:
                return entity_id
    return None


def _device_bound_domain(
    bindings: dict[str, Any], device_data: dict[str, Any]
) -> str | None:
    """Return the selected domain for a bound device."""
    if isinstance(domain := device_data.get("domain"), str) and domain:
        return domain

    if not (entity_id := _device_primary_entity_id(device_data)):
        return None

    entity_data = bindings.get("entities", {}).get(entity_id)
    if not isinstance(entity_data, dict):
        return None

    domain = entity_data.get("domain")
    return domain if isinstance(domain, str) and domain else None


def _device_bound_category_code(
    bindings: dict[str, Any], device_data: dict[str, Any]
) -> str:
    """Return the category_code for a bound device (empty string if unset)."""
    if isinstance(cat := device_data.get("category_code"), str):
        return cat

    if not (entity_id := _device_primary_entity_id(device_data)):
        return ""

    entity_data = bindings.get("entities", {}).get(entity_id)
    if not isinstance(entity_data, dict):
        return ""

    cat = entity_data.get("category_code", "")
    return cat if isinstance(cat, str) else ""


def _device_is_available_for_bindings(
    hass: HomeAssistant,
    bindings: dict[str, Any],
    device_id: str,
    *,
    override_entity_id: str | None = None,
    override_state: State | None = None,
) -> bool:
    """Return if any bound entity for a device is currently available."""
    device_data = bindings.get("devices", {}).get(device_id)
    if not isinstance(device_data, dict):
        return False

    for entity_id in device_data.get("entity_ids", []):
        state = (
            override_state
            if override_entity_id is not None and entity_id == override_entity_id
            else hass.states.get(entity_id)
        )
        if _state_is_available(state):
            return True

    return False


def _device_has_available_entity(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    device_id: str,
) -> bool:
    """Return True if the device has at least one available (online) entity.

    Mirrors the availability filter used by async_publish_eligible_ha_devices
    (device/list/found): a device with no available, non-disabled,
    non-excluded-domain entity is treated as offline. Used by topo/get
    reconciliation to decide whether an offline device should keep its cloud
    binding. Works off the entity registry + live state, so it does not depend
    on a local binding existing.
    """
    from .registry_sync import _async_entry_domain, _is_excluded_domain

    for entity_entry in er.async_entries_for_device(entity_registry, device_id):
        if entity_entry.disabled_by is not None:
            continue
        if _is_excluded_domain(
            _async_entry_domain(hass, entity_entry.config_entry_id)
        ):
            continue
        if _state_is_available(hass.states.get(entity_entry.entity_id)):
            return True
    return False


async def _async_handle_bound_entity_availability_change(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    entity_id: str,
    old_state: State | None,
    new_state: State | None,
) -> None:
    """Report child-device online/offline when a bound entity changes availability."""
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None:
        return

    if not (bindings := await async_load_gateway_bindings(hass, entry.entry_id)):
        return

    if not (entity_data := bindings.get("entities", {}).get(entity_id)):
        return

    device_id = entity_data.get("device_id")
    if not isinstance(device_id, str):
        return

    device_data = bindings.get("devices", {}).get(device_id)
    if not isinstance(device_data, dict):
        return

    tuya_device_id = device_data.get("tuya_device_id")
    if not isinstance(tuya_device_id, str) or not tuya_device_id:
        return

    was_available = _device_is_available_for_bindings(
        hass,
        bindings,
        device_id,
        override_entity_id=entity_id,
        override_state=old_state,
    )
    is_available = _device_is_available_for_bindings(
        hass,
        bindings,
        device_id,
        override_entity_id=entity_id,
        override_state=new_state,
    )

    if was_available == is_available:
        return

    if is_available:
        runtime_data.publish_subdevice_login([tuya_device_id])
        return

    runtime_data.publish_subdevice_logout([tuya_device_id])


_pending_device_reports: dict[str, asyncio.TimerHandle] = {}


async def _async_report_bound_entity_state_change(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    entity_id: str,
    old_state: State | None,
    new_state: State | None,
) -> None:
    """Schedule a coalescing child-device property report for state changes.

    HA may split a single logical update into multiple rapid
    EVENT_STATE_CHANGED events (e.g. a cover's ``current_position``
    attribute changes in one event and its ``state`` changes in the
    next, both within the same millisecond).  Publishing each event
    independently can produce contradictory MQTT messages (position=0
    but work_state=closing).

    To fix this without blocking ongoing dynamic updates (e.g. a cover
    moving from 0 % to 100 %), we use a *fixed-delay coalesce* strategy
    per tuya_device_id:

    * On the **first** event for a device, start a short timer and
      record the original old_state.
    * On **subsequent** events that arrive before the timer fires, do
      **not** reset the timer — just let it fire on schedule.
    * When the timer fires, read the **current** entity state from HA
      (which by then includes all attributes that were being updated)
      and publish a single consistent report.

    This gives HA ~150 ms to settle all attributes of a single logical
    change, while still reporting every subsequent position tick
    faithfully.
    """
    runtime_data = getattr(entry, "runtime_data", None)
    if (
        runtime_data is None
        or old_state is None
        or new_state is None
        or not hasattr(runtime_data, "publish_child_property_report")
    ):
        return

    if not _state_can_be_reported(new_state):
        return

    LOGGER.debug(
        "Evaluating entity change entity_id=%s old_state=%s new_state=%s",
        entity_id,
        old_state,
        new_state,
    )

    # Resolve the changed entity to its Tuya device THROUGH the pidspec route
    # table — the single source of truth for what a device binds. Previously
    # this gated on the gateway bindings["entities"] map, which is populated by
    # a separate path and can drift out of sync with the route table after a
    # topo/get restore (e.g. when duplicate entities exist), silently dropping a
    # bound entity's state changes. Reverse-looking-up the route table keeps the
    # trigger consistent with how the report PAYLOAD is built (also route-table
    # based): any entity the device actually binds can trigger a report.
    route_table = find_route_table_by_entity(hass, entity_id)
    if route_table is None:
        LOGGER.debug(
            "report-drop[entity_not_in_any_route_table] entity_id=%s",
            entity_id,
        )
        return

    tuya_device_id = route_table.tuya_device_id
    if not tuya_device_id:
        LOGGER.debug(
            "report-drop[no_tuya_device_id] entity_id=%s",
            entity_id,
        )
        return

    # domain/category_code flow through to the flush (currently unused there);
    # derive them from the route table for continuity.
    domain = (
        route_table.primary_entity_id.split(".")[0]
        if route_table.primary_entity_id
        else entity_id.split(".")[0]
    )
    category_code = route_table.category_code or None

    # Coalesce per DEVICE (tuya_device_id), NOT per entity. Tuya expects a
    # FULL-STATE report at the PID level, so when several entities of the same
    # device change at once we must merge them into ONE complete report. A
    # device-level key collapses concurrent multi-entity changes into a single
    # flush that rebuilds the full payload from live HA state — this is what
    # prevents sibling entities from overwriting each other.
    report_key = f"{entry.entry_id}:{tuya_device_id}"

    if report_key in _pending_device_reports:
        LOGGER.debug(
            "Coalesce: timer already pending for tuya_device_id=%s, "
            "all entity changes will be merged on flush",
            tuya_device_id,
        )
        return

    LOGGER.debug(
        "report-trigger[scheduled] entity_id=%s tuya_device_id=%s domain=%s",
        entity_id,
        tuya_device_id,
        domain,
    )
    handle = hass.loop.call_later(
        _STATE_REPORT_DEBOUNCE_SECONDS,
        lambda: hass.async_create_task(
            _async_flush_device_report(
                hass,
                entry,
                report_key,
                entity_id,
                tuya_device_id,
                domain,
                category_code,
            )
        ),
    )
    _pending_device_reports[report_key] = handle


async def _async_flush_device_report(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    report_key: str,
    entity_id: str,
    tuya_device_id: str,
    domain: str,
    category_code: str | None,
) -> None:
    """Publish the debounced FULL-STATE property report for a device.

    Tuya expects PID-level full-state reports, so we rebuild the complete DP
    payload from the *current* state of every bound entity (not just the one
    that triggered this flush). Building from live HA state means concurrent
    changes on sibling entities can never overwrite each other — every flush
    reflects the latest state of the whole device.
    """
    _pending_device_reports.pop(report_key, None)

    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None or not hasattr(
        runtime_data, "publish_child_property_report"
    ):
        return

    context = _build_mapping_context(hass, runtime_data, tuya_device_id)

    # Primary path: assemble the COMPLETE PID payload from all bound entities.
    new_properties = pidspec_build_full_device_properties(
        hass, tuya_device_id, context=context
    )
    if new_properties is None:
        # Binding is pidspec-gated; a missing route table means the device is
        # not (or no longer) pidspec-bound. Skip rather than use legacy mapping.
        LOGGER.warning(
            "flush report: no pidspec route table for tuya_device_id=%s; "
            "skipping (legacy mapping retired)",
            tuya_device_id,
        )
        return
    if not new_properties:
        return

    new_values = _property_values(new_properties)

    # Dedup against the last full snapshot reported for this device. The snapshot
    # is stored whole (never patched), so there is no read-modify-write race.
    route_table = get_route_table(hass, tuya_device_id)
    if route_table is not None and route_table.last_reported_snapshot == new_values:
        LOGGER.debug(
            "Skipping report tuya_device_id=%s: full PID snapshot unchanged",
            tuya_device_id,
        )
        return

    LOGGER.debug(
        "Reporting full PID state tuya_device_id=%s (triggered by %s) values=%s",
        tuya_device_id,
        entity_id,
        new_values,
    )
    await hass.async_add_executor_job(
        runtime_data.publish_child_property_report,
        tuya_device_id,
        new_properties,
    )
    if route_table is not None:
        route_table.last_reported_snapshot = new_values


async def async_handle_property_report_response(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    tuya_device_id: str,
    payload: bytes,
) -> None:
    """Handle property/report_response from cloud for a child device.

    Logs whether the individual property report was accepted (code=0) or
    rejected by the cloud, mirroring the batch_report acknowledgement pattern.
    """
    try:
        message = json.loads(payload)
    except json.JSONDecodeError:
        LOGGER.warning(
            "Failed to decode property report response for %s (length=%s)",
            tuya_device_id,
            len(payload),
        )
        return

    code = message.get("code", -1)
    msg_id = message.get("msgId", "")
    if code == 0:
        LOGGER.debug(
            "Property report accepted for tuya_device_id=%s msgId=%s",
            tuya_device_id,
            msg_id,
        )
    else:
        LOGGER.warning(
            "Property report rejected for tuya_device_id=%s code=%s msgId=%s msg=%s",
            tuya_device_id,
            code,
            msg_id,
            message,
        )


async def async_handle_subdevice_bind_response(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    payload: bytes,
) -> None:
    """Bind child devices to the virtual gateway only on successful cloud response.

    Only when code=0 do we persist bindings, associate config entries, and
    subscribe to child device MQTT topics.  Previously this was done eagerly
    in async_handle_subdevice_to_bind before knowing whether the cloud
    accepted the bind request.
    """

    try:
        message = json.loads(payload)
    except json.JSONDecodeError:
        LOGGER.warning("Failed to decode child bind response payload (length=%s)", len(payload))
        return

    code = message.get("code", -1)
    msg_id = message.get("msgId", "")

    if code == 1001:
        runtime_data = getattr(entry, "runtime_data", None)
        if (
            runtime_data is not None
            and hasattr(runtime_data, "republish_subdevice_bind")
            and isinstance(msg_id, str)
            and msg_id
            and runtime_data.republish_subdevice_bind(msg_id)
        ):
            LOGGER.warning(
                "Child bind response code=1001 (msgId=%s), retrying once", msg_id
            )
            return
        # Retry not possible (already retried or not found) — clean up and give up.
        LOGGER.warning("Child bind response rejected (code=%s): %s", code, message)
        if (
            runtime_data is not None
            and hasattr(runtime_data, "pop_pending_bind_request")
            and isinstance(msg_id, str)
            and msg_id
        ):
            runtime_data.pop_pending_bind_request(msg_id)
        return

    if code != 0:
        LOGGER.warning("Child bind response rejected (code=%s): %s", code, message)
        runtime_data = getattr(entry, "runtime_data", None)
        if (
            runtime_data is not None
            and hasattr(runtime_data, "pop_pending_bind_request")
            and isinstance(msg_id, str)
            and msg_id
        ):
            runtime_data.pop_pending_bind_request(msg_id)
        return

    response_data = message.get("data", [])
    if not isinstance(response_data, list) or not response_data:
        LOGGER.warning("Child bind response has no data: %s", message)
        return

    runtime_data = getattr(entry, "runtime_data", None)
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    bindings = await async_load_gateway_bindings(hass, entry.entry_id) or {}
    devices_map: dict[str, dict[str, str | list[str]]] = dict(
        bindings.get("devices", {})
    )
    entities_map: dict[str, dict[str, str | None | list[str]]] = dict(
        bindings.get("entities", {})
    )
    locally_bound_client_ids = set(devices_map)
    pending_bind_clients = _get_pending_bind_clients(hass)
    pending_bind_client_ids = set(pending_bind_clients.get(entry.entry_id, set()))
    responded_client_ids: set[str] = set()
    deleted_device_ids: list[str] = [
        did
        for did in bindings.get("deleted_device_ids", [])
        if isinstance(did, str) and did
    ]
    deleted_device_id_set = set(deleted_device_ids)

    updated = False
    new_tuya_device_ids: list[str] = []

    for child_data in response_data:
        if not isinstance(child_data, dict):
            continue
        client_id = child_data.get("clientId")
        tuya_device_id = child_data.get("deviceId")
        product_id = child_data.get("productId")
        if (
            not isinstance(client_id, str)
            or not client_id
            or not isinstance(tuya_device_id, str)
            or not tuya_device_id
        ):
            continue

        responded_client_ids.add(client_id)

        if (
            client_id not in locally_bound_client_ids
            and client_id not in pending_bind_client_ids
        ):
            continue

        # client_id is the HA device ID
        device = device_registry.async_get(client_id)
        if device is None:
            LOGGER.debug(
                "bind_response: device not found for clientId=%s", client_id
            )
            continue

        device_name = device.name_by_user or device.name or device.id

        # --- PidSpec-driven bind resolution (target flow) ---
        from .pidspec_bridge import pidspec_resolve_bind, store_route_table

        selected_entities: list[str] = []
        primary_entity_id: str | None = None
        selected_domain: str | None = None
        selected_category_code: str = ""

        if isinstance(product_id, str) and product_id:
            bind_result = pidspec_resolve_bind(
                hass, client_id, product_id, tuya_device_id
            )
            if bind_result is not None:
                selected_entities = bind_result.entity_ids
                primary_entity_id = bind_result.primary_entity_id
                selected_domain = bind_result.primary_domain
                selected_category_code = bind_result.category_code
                store_route_table(hass, tuya_device_id, bind_result.route_table)
                LOGGER.info(
                    "bind_response: pidspec resolved device %s → %d entities",
                    client_id,
                    len(selected_entities),
                )

        if not selected_entities:
            LOGGER.info(
                "bind_response: pidspec cannot resolve clientId=%s productId=%s, skipping bind",
                client_id,
                product_id,
            )
            continue

        # Remove from deleted list if previously deleted
        if device.id in deleted_device_id_set:
            deleted_device_ids = [
                did for did in deleted_device_ids if did != device.id
            ]
            deleted_device_id_set.discard(device.id)

        # Persist binding data
        devices_map[device.id] = {
            "name": device_name,
            "entity_ids": selected_entities,
            "primary_entity_id": primary_entity_id,
            "domain": selected_domain,
            "category_code": selected_category_code,
            "product_id": product_id,
            "client_id": client_id,
            "tuya_device_id": tuya_device_id,
        }
        for eid in selected_entities:
            entities_map[eid] = {
                "device_id": device.id,
                "domain": selected_domain,
                "category_code": selected_category_code,
            }

        # Associate config entry with the device
        if entry.entry_id not in device.config_entries:
            device_registry.async_update_device(
                device.id,
                add_config_entry_id=entry.entry_id,
            )

        if runtime_data is not None:
            runtime_data.subscribe_child_device_messages(tuya_device_id)
            new_tuya_device_ids.append(tuya_device_id)
        updated = True

        LOGGER.info(
            "Bound device %s (clientId=%s) to Tuya deviceId=%s",
            device_name,
            client_id,
            tuya_device_id,
        )

    if updated:
        bindings["devices"] = devices_map
        bindings["entities"] = entities_map
        bindings["deleted_device_ids"] = deleted_device_ids
        await async_save_gateway_bindings(hass, entry.entry_id, bindings)
        if runtime_data is not None and new_tuya_device_ids:
            runtime_data.publish_subdevice_login(new_tuya_device_ids)

    # Clean up the pending bind request now that it has been fully processed.
    if (
        runtime_data is not None
        and hasattr(runtime_data, "pop_pending_bind_request")
        and isinstance(msg_id, str)
        and msg_id
    ):
        runtime_data.pop_pending_bind_request(msg_id)

    if responded_client_ids:
        if stored_pending_client_ids := pending_bind_clients.get(entry.entry_id):
            stored_pending_client_ids.difference_update(responded_client_ids)
            if not stored_pending_client_ids:
                pending_bind_clients.pop(entry.entry_id, None)


def _extract_child_device_ids(data: Any) -> list[str]:
    """Extract Tuya child device IDs from MQTT payload data."""
    if not isinstance(data, list):
        return []

    child_device_ids: list[str] = []
    for item in data:
        if isinstance(item, str) and item:
            child_device_ids.append(item)
            continue
        if isinstance(item, dict) and isinstance(device_id := item.get("deviceId"), str):
            child_device_ids.append(device_id)
    return child_device_ids


def _extract_topology_deleted_child_device_ids(data: Any) -> list[str]:
    """Extract deleted child device IDs from topology change payload data."""
    if not isinstance(data, dict):
        return []

    return _extract_child_device_ids(data.get("delDevIds"))


_bindings_locks: dict[str, asyncio.Lock] = {}


def _get_bindings_lock(entry_id: str) -> asyncio.Lock:
    """Return a per-entry asyncio.Lock for serializing bindings modifications."""
    if entry_id not in _bindings_locks:
        _bindings_locks[entry_id] = asyncio.Lock()
    return _bindings_locks[entry_id]


async def _async_remove_subdevice_bindings(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    child_device_ids: list[str],
    *,
    mark_deleted_from_tuya_side: bool,
) -> None:
    """Remove bindings and subscriptions for deleted child devices."""
    if not child_device_ids:
        LOGGER.debug("Child delete cleanup skipped: no child device ids")
        return

    async with _get_bindings_lock(entry.entry_id):
        if not (bindings := await async_load_gateway_bindings(hass, entry.entry_id)):
            LOGGER.debug("Child delete cleanup skipped: no stored bindings")
            return

        devices = dict(bindings.get("devices", {}))
        entities = dict(bindings.get("entities", {}))
        deleted_device_ids = [
            device_id
            for device_id in bindings.get("deleted_device_ids", [])
            if isinstance(device_id, str) and device_id
        ]
        deleted_device_id_set = set(deleted_device_ids)
        runtime_data = getattr(entry, "runtime_data", None)
        device_registry = dr.async_get(hass)
        updated = False

        for child_device_id in dict.fromkeys(child_device_ids):
            matched_device_id = next(
                (
                    device_id
                    for device_id, device_data in devices.items()
                    if device_data.get("tuya_device_id") == child_device_id
                ),
                None,
            )
            if matched_device_id is None:
                LOGGER.debug(
                    "Child delete cleanup could not match tuya device id=%s",
                    child_device_id,
                )
                continue

            LOGGER.debug(
                "Child delete cleanup matched tuya device id=%s to ha device id=%s",
                child_device_id,
                matched_device_id,
            )

            device_data = devices.pop(matched_device_id)
            for entity_id in device_data.get("entity_ids", []):
                entities.pop(entity_id, None)

            if (
                mark_deleted_from_tuya_side
                and matched_device_id not in deleted_device_id_set
            ):
                deleted_device_ids.append(matched_device_id)
                deleted_device_id_set.add(matched_device_id)

            if runtime_data is not None and hasattr(
                runtime_data, "unsubscribe_child_device_messages"
            ):
                runtime_data.unsubscribe_child_device_messages(child_device_id)

            from .pidspec_bridge import remove_route_table

            remove_route_table(hass, child_device_id)

            if stale_device := device_registry.async_get(matched_device_id):
                device_registry.async_update_device(
                    stale_device.id,
                    remove_config_entry_id=entry.entry_id,
                )

            updated = True

        if not updated:
            LOGGER.debug(
                "Child delete cleanup made no changes for child ids=%s",
                child_device_ids,
            )
            return

        bindings["devices"] = devices
        bindings["entities"] = entities
        bindings["deleted_device_ids"] = deleted_device_ids
        await async_save_gateway_bindings(hass, entry.entry_id, bindings)
        LOGGER.debug(
            "Child delete cleanup saved bindings: devices=%s entities=%s deleted=%s",
            len(devices),
            len(entities),
            deleted_device_ids,
        )


async def _async_remove_local_bindings_by_device_id(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    ha_device_ids: list[str],
    *,
    mark_deleted_from_tuya_side: bool,
) -> None:
    """Remove local bindings by Home Assistant device ID."""
    if not ha_device_ids:
        LOGGER.debug("Local binding cleanup skipped: no HA device ids")
        return

    async with _get_bindings_lock(entry.entry_id):
        if not (bindings := await async_load_gateway_bindings(hass, entry.entry_id)):
            LOGGER.debug("Local binding cleanup skipped: no stored bindings")
            return

        devices = dict(bindings.get("devices", {}))
        entities = dict(bindings.get("entities", {}))
        deleted_device_ids = [
            device_id
            for device_id in bindings.get("deleted_device_ids", [])
            if isinstance(device_id, str) and device_id
        ]
        deleted_device_id_set = set(deleted_device_ids)
        runtime_data = getattr(entry, "runtime_data", None)
        device_registry = dr.async_get(hass)
        updated = False

        for ha_device_id in dict.fromkeys(ha_device_ids):
            device_data = devices.pop(ha_device_id, None)
            if device_data is None:
                LOGGER.debug(
                    "Local binding cleanup could not match HA device id=%s",
                    ha_device_id,
                )
                continue

            LOGGER.debug(
                "Local binding cleanup matched HA device id=%s to tuya device id=%s",
                ha_device_id,
                device_data.get("tuya_device_id"),
            )

            for entity_id in device_data.get("entity_ids", []):
                entities.pop(entity_id, None)

            if (
                mark_deleted_from_tuya_side
                and ha_device_id not in deleted_device_id_set
            ):
                deleted_device_ids.append(ha_device_id)
                deleted_device_id_set.add(ha_device_id)

            if (
                runtime_data is not None
                and hasattr(runtime_data, "unsubscribe_child_device_messages")
                and isinstance(
                    tuya_device_id := device_data.get("tuya_device_id"),
                    str,
                )
                and tuya_device_id
            ):
                runtime_data.unsubscribe_child_device_messages(tuya_device_id)

            _stale_tid = device_data.get("tuya_device_id")
            if isinstance(_stale_tid, str) and _stale_tid:
                from .pidspec_bridge import remove_route_table

                remove_route_table(hass, _stale_tid)

            if stale_device := device_registry.async_get(ha_device_id):
                device_registry.async_update_device(
                    stale_device.id,
                    remove_config_entry_id=entry.entry_id,
                )

            updated = True

        if not updated:
            LOGGER.debug(
                "Local binding cleanup made no changes for HA device ids=%s",
                ha_device_ids,
            )
            return

        bindings["devices"] = devices
        bindings["entities"] = entities
        bindings["deleted_device_ids"] = deleted_device_ids
        await async_save_gateway_bindings(hass, entry.entry_id, bindings)
        LOGGER.debug(
            "Local binding cleanup saved bindings: devices=%s entities=%s deleted=%s",
            len(devices),
            len(entities),
            deleted_device_ids,
        )


async def async_handle_subdevice_delete(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    payload: bytes,
) -> None:
    """Handle child-device delete requests sent from Tuya."""
    try:
        message = json.loads(payload)
    except json.JSONDecodeError:
        LOGGER.warning("Failed to decode child delete payload (length=%s)", len(payload))
        return

    runtime_data = getattr(entry, "runtime_data", None)
    if (
        runtime_data is not None
        and hasattr(runtime_data, "has_pending_subdevice_delete_request")
        and isinstance(msg_id := message.get("msgId"), str)
        and runtime_data.has_pending_subdevice_delete_request(msg_id)
    ):
        LOGGER.debug("Ignoring echoed child delete request: %s", message)
        return

    await _async_remove_subdevice_bindings(
        hass,
        entry,
        _extract_child_device_ids(message.get("data")),
        mark_deleted_from_tuya_side=True,
    )
    # Re-publish list/found so the app sees updated eligible devices,
    # consistent with HA-side device deletion which triggers a resync.
    await async_publish_eligible_ha_devices(hass, entry)


async def async_handle_subdevice_delete_response(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    payload: bytes,
) -> None:
    """Handle child-device delete responses for gateway-initiated deletes."""
    try:
        message = json.loads(payload)
    except json.JSONDecodeError:
        LOGGER.warning("Failed to decode child delete response payload (length=%s)", len(payload))
        return

    runtime_data = getattr(entry, "runtime_data", None)
    child_device_ids = _extract_child_device_ids(message.get("data"))
    if (
        not child_device_ids
        and runtime_data is not None
        and hasattr(runtime_data, "pop_pending_subdevice_delete_request")
        and isinstance(msg_id := message.get("msgId"), str)
    ):
        child_device_ids = runtime_data.pop_pending_subdevice_delete_request(msg_id)
    elif (
        runtime_data is not None
        and hasattr(runtime_data, "clear_pending_subdevice_delete_request")
        and isinstance(msg_id := message.get("msgId"), str)
    ):
        runtime_data.clear_pending_subdevice_delete_request(msg_id)

    if message.get("code") != 0:
        LOGGER.warning("Child delete response rejected: %s", message)
        return

    LOGGER.debug(
        "Resolved child delete response ids msg_id=%s child_device_ids=%s",
        message.get("msgId"),
        child_device_ids,
    )

    await _async_remove_subdevice_bindings(
        hass,
        entry,
        child_device_ids,
        mark_deleted_from_tuya_side=False,
    )


async def async_handle_topology_change(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    payload: bytes,
) -> None:
    """Handle Tuya topology changes for deleted child devices."""
    try:
        message = json.loads(payload)
    except json.JSONDecodeError:
        LOGGER.warning("Failed to decode topology change payload (length=%s)", len(payload))
        return

    await _async_remove_subdevice_bindings(
        hass,
        entry,
        _extract_topology_deleted_child_device_ids(message.get("data")),
        mark_deleted_from_tuya_side=True,
    )
    # Re-publish list/found so the app sees updated eligible devices,
    # consistent with HA-side device deletion which triggers a resync.
    await async_publish_eligible_ha_devices(hass, entry)


def _coerce_topo_index_id(item: dict[str, Any]) -> int | None:
    """Return ``indexId`` from a topo item as an integer when possible."""
    raw_index_id = item.get("indexId")
    if isinstance(raw_index_id, int) and not isinstance(raw_index_id, bool):
        return raw_index_id
    if isinstance(raw_index_id, str):
        try:
            return int(raw_index_id)
        except ValueError:
            return None
    return None


async def _async_collect_topo_response_data(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    message: dict[str, Any],
    response_data: list[Any],
) -> list[dict[str, Any]] | None:
    """Collect paginated topo/get responses and return a full data list.

    Returns ``None`` when another page has been requested and reconciliation
    should wait for that page.
    """
    topo_sync_sessions = _get_topo_sync_sessions(hass)
    session = topo_sync_sessions.get(entry.entry_id)

    normalized_items: list[dict[str, Any]] = [
        item for item in response_data if isinstance(item, dict)
    ]

    if session is None:
        return normalized_items

    response_msg_id = message.get("msgId")
    if (
        isinstance(response_msg_id, str)
        and response_msg_id
        and response_msg_id != session.pending_msg_id
    ):
        LOGGER.debug(
            "Ignoring topo/get response with unexpected msgId=%s (expected=%s)",
            response_msg_id,
            session.pending_msg_id,
        )
        return None

    session.last_updated_monotonic = time.monotonic()

    for item in normalized_items:
        if (
            isinstance(device_id := item.get("deviceId"), str)
            and device_id
        ):
            if device_id in session.seen_device_ids:
                continue
            session.seen_device_ids.add(device_id)
        session.records.append(item)

    last_index_id: int | None = None
    if normalized_items:
        for item in reversed(normalized_items):
            if (coerced_index_id := _coerce_topo_index_id(item)) is not None:
                last_index_id = coerced_index_id
                break

    has_more_pages = len(normalized_items) >= session.page_size
    if not has_more_pages:
        topo_sync_sessions.pop(entry.entry_id, None)
        return session.records

    if last_index_id is None:
        LOGGER.warning(
            "topo/get pagination: missing indexId in full page, "
            "finishing with current records"
        )
        topo_sync_sessions.pop(entry.entry_id, None)
        return session.records

    if last_index_id <= session.current_start_id:
        LOGGER.warning(
            "topo/get pagination did not advance "
            "(last_index_id=%s current_start_id=%s), "
            "finishing with current records",
            last_index_id,
            session.current_start_id,
        )
        topo_sync_sessions.pop(entry.entry_id, None)
        return session.records

    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None or not hasattr(runtime_data, "publish_topo_get"):
        LOGGER.warning(
            "Cannot request next topo/get page: no MQTT client "
            "(startId=%s pageSize=%s)",
            last_index_id,
            session.page_size,
        )
        topo_sync_sessions.pop(entry.entry_id, None)
        return session.records

    next_msg_id = runtime_data.publish_topo_get(
        start_id=last_index_id,
        page_size=session.page_size,
    )
    if not next_msg_id:
        topo_sync_sessions.pop(entry.entry_id, None)
        return session.records

    session.pending_msg_id = next_msg_id
    session.current_start_id = last_index_id
    LOGGER.debug(
        "Requested next topo/get page (startId=%s pageSize=%s msgId=%s)",
        last_index_id,
        session.page_size,
        next_msg_id,
    )
    return None


async def async_handle_topo_get_response(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    payload: bytes,
) -> None:
    """Handle the response to a topo/get request.

    The response contains the list of child devices still bound in the
    cloud.  Three reconciliation passes are performed:

    1. **Stale local bindings** — local bindings whose HA `clientId` is
       NOT in the cloud list are removed locally.
    2. **Orphaned cloud bindings** — child devices the cloud says are
       bound but whose corresponding HA device no longer exists locally
       are deleted from the cloud via ``publish_subdevice_delete`` so the
       virtual gateway stays in sync.
    3. **Restore missing bindings** — cloud-bound devices whose
       ``clientId`` matches an existing HA device are restored or
       refreshed locally from the cloud mapping (e.g. after plugin
       uninstall and re-integration).
    """

    try:
        message = json.loads(payload)
    except json.JSONDecodeError:
        LOGGER.warning("Failed to decode topo/get response payload (length=%s)", len(payload))
        return

    code = message.get("code", -1)
    if code != 0:
        _get_topo_sync_sessions(hass).pop(entry.entry_id, None)
        LOGGER.warning("topo/get response rejected (code=%s): %s", code, message)
        return

    raw_response_data = message.get("data", [])
    if not isinstance(raw_response_data, list):
        _get_topo_sync_sessions(hass).pop(entry.entry_id, None)
        LOGGER.warning("topo/get response data is not a list: %s", message)
        return

    response_data = await _async_collect_topo_response_data(
        hass,
        entry,
        message,
        raw_response_data,
    )
    if response_data is None:
        return

    # Build the set of client IDs the cloud says are still bound.
    cloud_client_ids: set[str] = set()
    for item in response_data:
        if isinstance(item, dict):
            client_id = item.get("clientId")
            if isinstance(client_id, str) and client_id:
                cloud_client_ids.add(client_id)

    bindings = await async_load_gateway_bindings(hass, entry.entry_id) or {}

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    runtime_data = getattr(entry, "runtime_data", None)

    # --- Pass 1: remove local bindings whose clientId is not in cloud ---
    stale_local_device_ids = [
        ha_device_id
        for ha_device_id in bindings.get("devices", {})
        if ha_device_id not in cloud_client_ids
    ]

    if stale_local_device_ids:
        LOGGER.info(
            "topo/get sync: removing %s local bindings missing from cloud "
            "clientIds: %s",
            len(stale_local_device_ids),
            stale_local_device_ids,
        )
        await _async_remove_local_bindings_by_device_id(
            hass,
            entry,
            stale_local_device_ids,
            mark_deleted_from_tuya_side=False,
        )
        # Reload bindings after removal so subsequent passes see consistent state.
        bindings = await async_load_gateway_bindings(hass, entry.entry_id) or {}

    deleted_device_ids: list[str] = [
        did
        for did in bindings.get("deleted_device_ids", [])
        if isinstance(did, str) and did
    ]
    deleted_device_id_set = set(deleted_device_ids)

    # --- Pass 2: cloud-bound devices whose HA device is invalid ---
    orphaned_cloud_tuya_ids: set[str] = set()
    for item in response_data:
        if not isinstance(item, dict):
            continue
        client_id = item.get("clientId")
        tuya_device_id = item.get("deviceId")
        if not isinstance(tuya_device_id, str) or not tuya_device_id:
            continue

        if not isinstance(client_id, str) or not client_id:
            LOGGER.info(
                "topo/get sync: cloud binding without valid clientId, "
                "requesting delete for tuya_device_id=%s",
                tuya_device_id,
            )
            orphaned_cloud_tuya_ids.add(tuya_device_id)
            continue

        if device_registry.async_get(client_id) is None:
            LOGGER.info(
                "topo/get sync: cloud binding clientId=%s not found in HA, "
                "requesting cloud delete for tuya_device_id=%s",
                client_id,
                tuya_device_id,
            )
            orphaned_cloud_tuya_ids.add(tuya_device_id)

    if orphaned_cloud_tuya_ids:
        orphaned_cloud_tuya_id_list = list(orphaned_cloud_tuya_ids)
        # Ask the cloud to unbind these orphaned child devices.
        if (
            runtime_data is not None
            and hasattr(runtime_data, "publish_subdevice_delete")
        ):
            runtime_data.publish_subdevice_delete(orphaned_cloud_tuya_id_list)
        # Clean up local side as well.
        await _async_remove_subdevice_bindings(
            hass,
            entry,
            orphaned_cloud_tuya_id_list,
            mark_deleted_from_tuya_side=False,
        )
        bindings = await async_load_gateway_bindings(hass, entry.entry_id) or {}
        deleted_device_ids = [
            did
            for did in bindings.get("deleted_device_ids", [])
            if isinstance(did, str) and did
        ]
        deleted_device_id_set = set(deleted_device_ids)

    # --- Pass 3: restore / refresh bindings from cloud ---
    # Build a set of HA device IDs already bound locally.
    locally_bound_client_ids: set[str] = set(bindings.get("devices", {}).keys())
    devices_map: dict[str, dict[str, str | list[str]]] = dict(
        bindings.get("devices", {})
    )
    entities_map: dict[str, dict[str, str | None | list[str]]] = dict(
        bindings.get("entities", {})
    )

    restored_count = 0
    refreshed_count = 0
    cleared_deleted_count = 0
    new_tuya_device_ids: list[str] = []
    # Cloud-bound devices that exist in HA but fail the restore gate (excluded /
    # offline / low-confidence / DP routing failed). For these we both request a
    # cloud unbind (publish_subdevice_delete) AND remove our entry's association
    # from the HA device_registry — otherwise the device still appears as
    # belonging to tuya_cloud_ha_bridge in HA's UI even though we no longer have
    # a binding for it. The in-memory bindings map has nothing to clean in this
    # path (restore branch only runs for devices NOT in our in-memory map).
    restore_unbind_tuya_ids: list[str] = []
    restore_unbind_ha_device_ids: list[str] = []

    for item in response_data:
        if not isinstance(item, dict):
            continue
        client_id = item.get("clientId")
        tuya_device_id = item.get("deviceId")
        product_id = item.get("productId")
        if (
            not isinstance(client_id, str) or not client_id
            or not isinstance(tuya_device_id, str) or not tuya_device_id
        ):
            continue

        # If already bound locally, refresh the cloud-managed mapping
        # fields directly from topo/get_response.
        if client_id in locally_bound_client_ids:
            existing = devices_map.get(client_id)
            if existing is not None:
                if client_id in deleted_device_id_set:
                    deleted_device_ids = [
                        did for did in deleted_device_ids if did != client_id
                    ]
                    deleted_device_id_set.discard(client_id)
                    cleared_deleted_count += 1

                previous_tuya_device_id = existing.get("tuya_device_id")
                binding_changed = False

                if previous_tuya_device_id != tuya_device_id:
                    if (
                        runtime_data is not None
                        and hasattr(runtime_data, "unsubscribe_child_device_messages")
                        and isinstance(previous_tuya_device_id, str)
                        and previous_tuya_device_id
                    ):
                        runtime_data.unsubscribe_child_device_messages(
                            previous_tuya_device_id
                        )
                    if isinstance(previous_tuya_device_id, str) and previous_tuya_device_id:
                        from .pidspec_bridge import remove_route_table

                        remove_route_table(hass, previous_tuya_device_id)
                    existing["tuya_device_id"] = tuya_device_id
                    binding_changed = True

                if (
                    isinstance(product_id, str)
                    and product_id
                    and existing.get("product_id") != product_id
                ):
                    existing["product_id"] = product_id
                    binding_changed = True

                if binding_changed and runtime_data is not None:
                    runtime_data.subscribe_child_device_messages(tuya_device_id)
                    new_tuya_device_ids.append(tuya_device_id)
                # Ensure the HA device is associated with this config entry
                # so it appears on the integration page.
                device = device_registry.async_get(client_id)
                if device is not None and entry.entry_id not in device.config_entries:
                    device_registry.async_update_device(
                        device.id,
                        add_config_entry_id=entry.entry_id,
                    )
                if binding_changed:
                    refreshed_count += 1
                    LOGGER.info(
                        "topo/get refresh: updated local binding for "
                        "clientId=%s with tuya_device_id=%s",
                        client_id,
                        tuya_device_id,
                    )
            continue

        # client_id is the HA device ID — check that the device still exists.
        device = device_registry.async_get(client_id)
        if device is None:
            LOGGER.debug(
                "topo/get restore: HA device not found for clientId=%s", client_id
            )
            continue

        # Apply the SAME filters as the device/list/found discovery path
        # (async_publish_eligible_ha_devices) so cloud bindings mirror exactly
        # the currently-bindable set. Request a cloud unbind when the device:
        #   - is structurally excluded (virtual/service, owned by `tuya` or
        #     another gateway entry), or
        #   - has NO available entity (offline). list/found does not report
        #     offline devices at binding/restart, so a stale cloud binding for
        #     an offline device must be unbound to stay consistent.
        # NOTE on offline: this restore branch only runs for devices NOT
        # already bound locally — which at startup/restart (in-memory bindings
        # are empty) is *every* cloud device. A device that goes offline while
        # already bound during normal runtime takes the refresh branch + Pass 4
        # instead, where offline devices are KEPT and merely reported offline,
        # not unbound.
        from .registry_sync import _is_excluded_device

        if _is_excluded_device(hass, device, entry.entry_id):
            LOGGER.info(
                "topo/get restore: clientId=%s is an excluded device → "
                "requesting cloud unbind for tuya_device_id=%s",
                client_id,
                tuya_device_id,
            )
            restore_unbind_tuya_ids.append(tuya_device_id)
            restore_unbind_ha_device_ids.append(client_id)
            continue

        if not _device_has_available_entity(hass, entity_registry, client_id):
            LOGGER.info(
                "topo/get restore: clientId=%s is offline (no available entity) "
                "→ requesting cloud unbind for tuya_device_id=%s",
                client_id,
                tuya_device_id,
            )
            restore_unbind_tuya_ids.append(tuya_device_id)
            restore_unbind_ha_device_ids.append(client_id)
            continue

        device_name = device.name_by_user or device.name or device.id

        # --- PidSpec-driven restore resolution ---
        # Restore MUST apply the same high-confidence inference gate as the
        # device/list/found discovery path (async_publish_eligible_ha_devices).
        # Trusting the stale cloud product_id via pidspec_resolve_bind alone
        # would auto-bind devices that full inference rejects as low-confidence
        # (score_low / margin_tight / …) — devices that never appear in
        # list/found. For those we request a cloud unbind so the stale binding
        # is removed; the device returns to the unbound pool and is re-reported
        # low-confidence by async_publish_eligible_ha_devices for rule repair,
        # then re-bound once a high-confidence rule arrives.
        from .pidspec_bridge import (
            _infer_pidspec_with_diagnosis,
            pidspec_resolve_bind,
            store_route_table,
        )

        discovery_result, diagnosis, _infer_result = _infer_pidspec_with_diagnosis(
            hass, client_id
        )
        if discovery_result is None:
            LOGGER.info(
                "topo/get restore: clientId=%s not high-confidence (%s) → "
                "requesting cloud unbind for tuya_device_id=%s",
                client_id,
                diagnosis,
                tuya_device_id,
            )
            restore_unbind_tuya_ids.append(tuya_device_id)
            restore_unbind_ha_device_ids.append(client_id)
            continue

        # High-confidence — resolve the route table using the inference's
        # authoritative product_id rather than the (possibly stale) cloud one.
        bind_result = pidspec_resolve_bind(
            hass, client_id, discovery_result.product_id, tuya_device_id
        )
        if bind_result is None:
            LOGGER.info(
                "topo/get restore: clientId=%s matched %s but DP routing failed → "
                "requesting cloud unbind for tuya_device_id=%s",
                client_id,
                discovery_result.product_id,
                tuya_device_id,
            )
            restore_unbind_tuya_ids.append(tuya_device_id)
            restore_unbind_ha_device_ids.append(client_id)
            continue

        selected_entities = bind_result.entity_ids
        primary_entity_id = bind_result.primary_entity_id
        selected_domain = bind_result.primary_domain
        selected_category_code = bind_result.category_code
        store_route_table(hass, tuya_device_id, bind_result.route_table)
        # Persist the inferred product_id so the local binding stays consistent
        # with what inference matched.
        product_id = discovery_result.product_id

        # Persist binding data.
        if device.id in deleted_device_id_set:
            deleted_device_ids = [
                did for did in deleted_device_ids if did != device.id
            ]
            deleted_device_id_set.discard(device.id)
            cleared_deleted_count += 1

        devices_map[device.id] = {
            "name": device_name,
            "entity_ids": selected_entities,
            "primary_entity_id": primary_entity_id,
            "domain": selected_domain,
            "category_code": selected_category_code,
            "product_id": product_id,
            "client_id": client_id,
            "tuya_device_id": tuya_device_id,
        }
        for eid in selected_entities:
            entities_map[eid] = {
                "device_id": device.id,
                "domain": selected_domain,
                "category_code": selected_category_code,
            }

        # Associate config entry with the device.
        if entry.entry_id not in device.config_entries:
            device_registry.async_update_device(
                device.id,
                add_config_entry_id=entry.entry_id,
            )

        if runtime_data is not None:
            runtime_data.subscribe_child_device_messages(tuya_device_id)
            new_tuya_device_ids.append(tuya_device_id)

        restored_count += 1
        LOGGER.info(
            "topo/get restore: restored binding %s (clientId=%s) "
            "tuya_device_id=%s",
            device_name,
            client_id,
            tuya_device_id,
        )

    if restored_count > 0 or refreshed_count > 0 or cleared_deleted_count > 0:
        bindings["devices"] = devices_map
        bindings["entities"] = entities_map
        bindings["deleted_device_ids"] = deleted_device_ids
        await async_save_gateway_bindings(hass, entry.entry_id, bindings)
        if runtime_data is not None and new_tuya_device_ids:
            runtime_data.publish_subdevice_login(new_tuya_device_ids)
        if restored_count > 0:
            LOGGER.info(
                "topo/get sync: restored %s bindings from cloud", restored_count
            )
        if refreshed_count > 0:
            LOGGER.info(
                "topo/get sync: refreshed %s local bindings with cloud data",
                refreshed_count,
            )
        if cleared_deleted_count > 0:
            LOGGER.info(
                "topo/get sync: cleared %s stale deleted clientIds restored from cloud",
                cleared_deleted_count,
            )

    # Cloud bindings that failed the restore gate (excluded / offline /
    # low-confidence / DP routing failed): unbind them from the cloud and
    # remove this entry's association from the HA device_registry so the
    # device no longer appears as belonging to tuya_cloud_ha_bridge in HA's
    # UI. The in-memory bindings map has nothing to clean here — restore runs
    # only for devices NOT in the in-memory map (the device_registry
    # association is persistent on disk and survives our memory-only model,
    # which is why this explicit cleanup is needed).
    if restore_unbind_tuya_ids and runtime_data is not None and hasattr(
        runtime_data, "publish_subdevice_delete"
    ):
        runtime_data.publish_subdevice_delete(restore_unbind_tuya_ids)
        for ha_device_id in dict.fromkeys(restore_unbind_ha_device_ids):
            stale_device = device_registry.async_get(ha_device_id)
            if stale_device is not None and entry.entry_id in stale_device.config_entries:
                device_registry.async_update_device(
                    stale_device.id,
                    remove_config_entry_id=entry.entry_id,
                )
        LOGGER.info(
            "topo/get restore: requested cloud unbind for %d device(s) not "
            "restored (excluded / offline / low-confidence): %s",
            len(restore_unbind_tuya_ids),
            restore_unbind_tuya_ids,
        )

    if (
        not stale_local_device_ids
        and not orphaned_cloud_tuya_ids
        and not restore_unbind_tuya_ids
        and restored_count == 0
        and refreshed_count == 0
    ):
        LOGGER.debug(
            "topo/get sync: all %s local bindings still present in cloud "
            "and all HA devices exist",
            len(bindings.get("devices", {})),
        )

    await async_publish_eligible_ha_devices(hass, entry)

    # --- Pass 4: re-validate the in-memory bindings and unbind the
    # non-conforming ones (keeps cloud + local in sync). Catches devices that
    # were bound earlier in this session but have since drifted.
    #   - structurally excluded device (virtual/service, owned by `tuya` or
    #     another gateway entry, or HA device gone) → unbind
    #   - low-confidence / unresolved DPs (async_pidspec_check_and_unbind) → unbind
    #
    # OFFLINE devices are deliberately EXCLUDED from the confidence check here:
    # inference reads live state, so an offline device's profile is degraded
    # (missing supported_features/attributes) and would be misjudged as
    # low-confidence. Per the runtime rule, a device that goes offline while
    # already bound must only be reported offline (see
    # _async_report_subdevices_online), NOT unbound. Unbinding offline devices
    # only happens at binding/restart, which goes through the restore branch
    # above (in-memory bindings start empty), not here.
    from .pidspec_bridge import async_pidspec_check_and_unbind
    from .registry_sync import _is_excluded_device

    final_bindings = await async_load_gateway_bindings(hass, entry.entry_id) or {}
    final_devices = final_bindings.get("devices", {})
    if final_devices:
        excluded_unbind_tuya_ids: list[str] = []
        confidence_check_devices: dict[str, dict[str, Any]] = {}
        for ha_device_id, device_data in final_devices.items():
            if not isinstance(device_data, dict):
                continue
            tuya_device_id = device_data.get("tuya_device_id")
            if not isinstance(tuya_device_id, str) or not tuya_device_id:
                continue
            device = device_registry.async_get(ha_device_id)
            if device is None or _is_excluded_device(hass, device, entry.entry_id):
                excluded_unbind_tuya_ids.append(tuya_device_id)
                continue
            # Offline bound device → keep it (reported offline elsewhere); skip
            # the confidence check so a degraded offline profile can't unbind it.
            if not _device_has_available_entity(hass, entity_registry, ha_device_id):
                continue
            confidence_check_devices[ha_device_id] = device_data

        confidence_unbind_tuya_ids = await async_pidspec_check_and_unbind(
            hass, confidence_check_devices
        )
        unbind_tuya_ids = list(
            dict.fromkeys(excluded_unbind_tuya_ids + confidence_unbind_tuya_ids)
        )
        if unbind_tuya_ids and runtime_data is not None:
            if hasattr(runtime_data, "publish_subdevice_delete"):
                runtime_data.publish_subdevice_delete(unbind_tuya_ids)
            await _async_remove_subdevice_bindings(
                hass, entry, unbind_tuya_ids, mark_deleted_from_tuya_side=False,
            )
            LOGGER.info(
                "topo/get sync pass 4: unbound %d device(s) "
                "(%d excluded, %d low-confidence)",
                len(unbind_tuya_ids),
                len(excluded_unbind_tuya_ids),
                len(confidence_unbind_tuya_ids),
            )

    # --- Pass 5: orphan device_registry sweep ---
    # In-memory bindings are the truth source. The HA device_registry persists
    # `entry.entry_id ∈ device.config_entries` on disk, so stale associations
    # can outlive a binding (orphaned by a prior crash, by an older code path
    # that forgot to clean it, or by a cloud-side unbind we observed but only
    # cleaned in cloud + memory). Without this sweep such devices keep showing
    # the `tuya_cloud_ha_bridge` badge in HA UI forever, even after the cloud
    # confirmed deletion and topo/get no longer returns them. Sweep: for every
    # HA device that carries our entry_id but has no in-memory binding (and is
    # not mid-bind), strip the association so HA UI matches the in-memory truth.
    final_bindings_for_sweep = (
        await async_load_gateway_bindings(hass, entry.entry_id) or {}
    )
    bound_ha_ids = set(final_bindings_for_sweep.get("devices", {}).keys())
    pending_ha_ids = set(
        _get_pending_bind_clients(hass).get(entry.entry_id, set())
    )
    orphan_count = 0
    for device in list(device_registry.devices.values()):
        if entry.entry_id not in device.config_entries:
            continue
        if device.id in bound_ha_ids or device.id in pending_ha_ids:
            continue
        device_registry.async_update_device(
            device.id,
            remove_config_entry_id=entry.entry_id,
        )
        orphan_count += 1
        LOGGER.info(
            "topo/get sync pass 5: dropped orphan device_registry association "
            "for clientId=%s (name=%s) — no in-memory binding, not pending bind",
            device.id,
            device.name_by_user or device.name or device.id,
        )
    if orphan_count:
        LOGGER.info(
            "topo/get sync pass 5: cleaned %d orphan device_registry "
            "association(s) to match in-memory bindings",
            orphan_count,
        )

    # Report sub-devices online and their current states AFTER cloud
    # reconciliation so we never report devices that the cloud has deleted.
    if runtime_data is not None:
        await _async_report_subdevices_online(hass, entry, runtime_data)
        await _async_report_all_bound_device_states(hass, entry, runtime_data)


def _compute_device_fingerprints(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
) -> dict[str, str]:
    """Return a structural fingerprint per inferable (unbound) device.

    Captures only the inputs pidspec inference reads — device
    name/model/manufacturer and, per entity, domain / device_class /
    entity_category / supported_features / the SET of attribute keys — but NOT
    volatile state values, so live readings (brightness, temperature) changing
    does not invalidate the fingerprint. Bound and excluded devices are omitted
    (they are not re-inferred). Used to gate registry-driven re-inference so
    cosmetic registry churn (area/icon change, no-op rename) no longer triggers
    re-inference and low-confidence re-reporting.
    """
    from .registry_sync import (
        _is_excluded_device,
        _is_excluded_domain,
        _async_entry_domain,
    )

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    fingerprints: dict[str, str] = {}

    for device in device_registry.devices.values():
        if _is_excluded_device(hass, device, entry.entry_id):
            continue
        if entry.entry_id in device.config_entries:
            continue  # already bound to this gateway — not re-inferred
        has_available = False
        entity_parts: list[str] = []
        for ent in er.async_entries_for_device(entity_registry, device.id):
            if ent.disabled_by is not None:
                continue
            if _is_excluded_domain(_async_entry_domain(hass, ent.config_entry_id)):
                continue
            state = hass.states.get(ent.entity_id)
            attrs = state.attributes if state is not None else {}
            if _state_is_available(state):
                has_available = True
            entity_parts.append(
                "|".join(
                    [
                        ent.entity_id,
                        ent.domain,
                        str(ent.device_class or ent.original_device_class or ""),
                        str(ent.entity_category.value if ent.entity_category else ""),
                        str(attrs.get("supported_features", "")),
                        ",".join(sorted(str(k) for k in attrs.keys())),
                    ]
                )
            )
        if not entity_parts or not has_available:
            continue
        parts = [
            device.name_by_user or device.name or "",
            device.model or "",
            device.manufacturer or "",
            *sorted(entity_parts),
        ]
        fingerprints[device.id] = hashlib.sha1(
            "\x1e".join(parts).encode("utf-8")
        ).hexdigest()

    return fingerprints


async def async_publish_eligible_ha_devices(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    *,
    allow_rule_refresh: bool = True,
) -> None:
    """Scan HA registries and publish eligible sub-devices via MQTT (Step 4).

    Uses pidspec inference first: high-confidence devices get pidspec product_id.
    Devices not covered by pidspec fall back to mapping-based PID resolution.
    Low-confidence devices are collected for cloud reporting.

    Publishes to tylink/${deviceId}/ha/devices so the app can show
    the user which HA devices are available for binding.
    """
    from .registry_sync import (
        _is_excluded_device,
        _is_excluded_domain,
        _async_entry_domain,
    )
    from .pidspec_bridge import _infer_pidspec_with_diagnosis

    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None or not hasattr(runtime_data, "publish_ha_devices"):
        LOGGER.debug("Cannot publish HA devices: no MQTT client")
        return

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    area_registry = ar.async_get(hass)
    ha_devices: list[dict[str, str]] = []
    low_confidence_results: dict[str, Any] = {}
    matched_results: list[Any] = []

    for device in device_registry.devices.values():
        if _is_excluded_device(hass, device, entry.entry_id):
            continue
        # Skip devices already bound to this gateway — they are not candidates
        # for (re-)binding and don't need re-inference / low-confidence report.
        if entry.entry_id in device.config_entries:
            continue

        # Collect all entity domains for this device.
        entity_domains: set[str] = set()
        has_available_entity = False
        entity_state_summary: list[str] = []
        for entity_entry in er.async_entries_for_device(entity_registry, device.id):
            if entity_entry.disabled_by is not None:
                continue
            if _is_excluded_domain(_async_entry_domain(hass, entity_entry.config_entry_id)):
                continue
            entity_domains.add(entity_entry.domain)
            if not has_available_entity:
                state = hass.states.get(entity_entry.entity_id)
                st = state.state if state is not None else "<missing>"
                entity_state_summary.append(f"{entity_entry.entity_id}={st}")
                if _state_is_available(state):
                    has_available_entity = True

        if not entity_domains or not has_available_entity:
            LOGGER.info(
                "publish_eligible: skip device %s (%s): entity_domains=%s has_available=%s\n"
                "  entity_states: %s",
                device.id, device.name, entity_domains, has_available_entity,
                ", ".join(entity_state_summary) if entity_state_summary else "(none)",
            )
            continue

        # --- pidspec-first: try inference to get product_id ---
        discovery_result, diagnosis, infer_result = _infer_pidspec_with_diagnosis(
            hass, device.id
        )
        LOGGER.info(
            "publish_eligible: pidspec %s → %s (name=%s)",
            device.id, diagnosis, device.name,
        )
        if discovery_result is not None:
            ha_device: dict[str, str] = {
                "productId": discovery_result.product_id,
                "clientId": device.id,
                "deviceName": device.name_by_user or device.name or device.id,
                "deviceType": discovery_result.category_code,
            }
            if device.area_id:
                area_entry = area_registry.async_get_area(device.area_id)
                if area_entry is not None:
                    ha_device["area"] = area_entry.name
            ha_devices.append(ha_device)
            matched_results.append(discovery_result)
            continue

        # pidspec didn't match — report to cloud for rule generation.
        # Forward the REAL inference result (signal + candidates) so the cloud
        # Skill can route correctly. Fall back to a synthetic result only when
        # inference produced none (cache empty / no profile).
        LOGGER.debug(
            "publish_eligible: device %s (%s) pidspec no match, domains=%s → low_confidence",
            device.id, device.name, entity_domains,
        )
        low_confidence_results[device.id] = infer_result

    LOGGER.debug(
        "publish_eligible: total_devices=%d, eligible=%d, low_confidence=%d",
        len(device_registry.devices), len(ha_devices), len(low_confidence_results),
    )
    runtime_data.publish_ha_devices(ha_devices)

    # Print high-confidence matched devices (PID + DP routes) for visibility.
    # Placed AFTER inference is done and BEFORE low-confidence reporting, per
    # operator request — gives a snapshot of what pidspec successfully inferred
    # before the cloud-report step runs.
    if matched_results:
        lines: list[str] = []
        for r in matched_results:
            # Find the matching ha_device dict to pull display name
            display_name = next(
                (
                    d["deviceName"]
                    for d in ha_devices
                    if d["clientId"] == r.ha_device_id
                ),
                r.ha_device_id,
            )
            lines.append(
                f"  ✓ {r.ha_device_id} (name={display_name}) → "
                f"product_id={r.product_id} category={r.category_code} "
                f"dp_routes={len(r.routes)}"
            )
            for route in r.routes:
                lines.append(
                    f"      {route.dpcode:<24} → {route.entity_id:<48} "
                    f"({route.domain}/{route.direction}, "
                    f"converter={route.converter}, component={route.component})"
                )
        LOGGER.info(
            "publish_eligible: %d high-confidence devices (PID + DP routes):\n%s",
            len(matched_results),
            "\n".join(lines),
        )

    # Report low-confidence devices to cloud for rule generation.
    # Lazy rule refresh: local rules are tried first; only when inference still
    # leaves low-confidence devices do we pull the latest cloud rules. If newer
    # rules arrive, re-infer once with the updated cache (without refreshing
    # again) — that re-run reports whatever is still low-confidence. The cloud
    # does not aggregate, so each report carries the full current set.
    if low_confidence_results:
        if allow_rule_refresh:
            rules_updated = await async_check_and_refresh_pidspec_rules(
                hass, entry, republish=False
            )
            if rules_updated:
                LOGGER.info(
                    "publish_eligible: %d low-confidence device(s) with local "
                    "rules, fetched newer cloud rules → re-inferring",
                    len(low_confidence_results),
                )
                await async_publish_eligible_ha_devices(
                    hass, entry, allow_rule_refresh=False
                )
                return
        await _async_report_low_confidence_devices(hass, low_confidence_results)


async def _async_report_low_confidence_devices(
    hass: HomeAssistant,
    infer_results: dict[str, Any],
) -> None:
    """Report devices that pidspec couldn't match to cloud for rule generation.

    *infer_results* maps ha_device_id → the real PidInferResult produced by
    inference (carrying the true low_confidence_signal — filter_empty /
    score_low / margin_tight / insufficient_domain_coverage — and the scored
    candidates). These are forwarded verbatim so the cloud Skill's decision
    routing can act on them. A device whose inference produced no result
    (value None — cache empty / no profile at scan time) falls back to a
    synthetic no_pidspec_match result so it is still surfaced for human review.
    """
    from .pidspec_bridge import get_rule_cache, _get_domain_data, _PIDSPEC_CLOUD_CLIENT
    from .device_profiling import async_build_device_profile
    from .pidspec.models import PidInferResult
    from .pidspec.orchestrator import build_low_confidence_report_payload

    domain_data = _get_domain_data(hass)
    cloud_client = domain_data.get(_PIDSPEC_CLOUD_CLIENT)
    if cloud_client is None:
        return

    cache = get_rule_cache(hass)
    if cache is None:
        return

    device_profiles: dict[str, Any] = {}
    results: dict[str, PidInferResult] = {}
    for device_id, infer_result in infer_results.items():
        profile = async_build_device_profile(hass, device_id, cache)
        if profile is None:
            continue
        device_profiles[device_id] = profile
        results[device_id] = infer_result if infer_result is not None else PidInferResult(
            status="needs_cloud_upgrade",
            reason="no_pidspec_match",
            low_confidence_signal="discovery_fallback",
        )

    if not device_profiles:
        return

    report_payload = await build_low_confidence_report_payload(
        device_profiles, results, hass=hass
    )

    try:
        await cloud_client.async_report_low_confidence(
            report_payload,
            local_rule_version=cache.rule_version,
            trigger="plugin_start",
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning(
            "publish_eligible: failed to report low-confidence devices: %s", exc
        )


async def async_check_and_refresh_pidspec_rules(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    *,
    republish: bool = True,
) -> bool:
    """Check cloud rule version; if newer, fetch bundle, update cache, persist.

    Called periodically (1 hour) and by publish_eligible's lazy rule refresh.
    Flow:
    1. Get cloud rule version
    2. If cloud > local → fetch bundle → update cache → persist
    3. If *republish* → re-publish eligible devices (triggers re-inference)

    Returns True when the rule cache was updated to a newer version, False
    otherwise (no newer version, fetch failure, or parse failure). The lazy
    caller passes ``republish=False`` and drives its own re-inference.
    """
    from .pidspec_bridge import get_rule_cache, _get_domain_data, _PIDSPEC_CLOUD_CLIENT
    from .pidspec.rule_cache import LocalRuleCache
    from .rules import async_save_rules_bundle

    domain_data = _get_domain_data(hass)
    cloud_client = domain_data.get(_PIDSPEC_CLOUD_CLIENT)
    if cloud_client is None:
        LOGGER.debug("rule_check: no cloud client configured, skipping")
        return False

    cache: LocalRuleCache | None = get_rule_cache(hass)
    if cache is None:
        LOGGER.debug("rule_check: no rule cache initialized, skipping")
        return False

    try:
        cloud_version = await cloud_client.async_get_rule_version()
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("rule_check: failed to check cloud rule version: %s", exc)
        return False

    if cloud_version <= cache.rule_version:
        LOGGER.debug(
            "rule_check: cloud version %d <= local %d, no update needed",
            cloud_version, cache.rule_version,
        )
        return False

    LOGGER.info(
        "rule_check: cloud version %d > local %d, fetching new rules",
        cloud_version, cache.rule_version,
    )

    try:
        bundle = await cloud_client.async_fetch_rules_bundle()
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("rule_check: failed to fetch rules bundle: %s", exc)
        return False

    if not cache.load_from_bundle(bundle):
        LOGGER.warning("rule_check: failed to parse fetched bundle")
        return False

    await async_save_rules_bundle(bundle)
    LOGGER.info(
        "rule_check: rules updated to version %d (%d pidspecs)%s",
        cache.rule_version,
        len(cache.pidspecs),
        ", re-publishing eligible" if republish else "",
    )
    if republish:
        await async_publish_eligible_ha_devices(
            hass, entry, allow_rule_refresh=False
        )
    return True


async def async_handle_subdevice_to_bind(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    payload: bytes,
) -> None:
    """Handle cloud-pushed device confirmation and publish bind request.

    Receives the list of confirmed devices from
    tylink/${deviceId}/device/topo/add/notify, builds bind requests with
    deviceName and defaultProperties, publishes to
    tylink/${deviceId}/device/sub/bind, then sends a response back to
    tylink/${deviceId}/device/topo/add/notify_response.

    NOTE: This function only sends the bind request to the cloud.
    The actual binding (persisting devices/entities, associating config
    entries) is deferred to async_handle_subdevice_bind_response, which
    runs only when the cloud returns code=0.
    """
    from .pidspec_bridge import (
        pidspec_build_default_properties,
        pidspec_resolve_bind,
    )

    try:
        message = json.loads(payload)
    except json.JSONDecodeError:
        LOGGER.warning("Failed to decode topo/add/notify payload (length=%s)", len(payload))
        return

    msg_id = message.get("msgId", str(int(time.time() * 1000)))
    data = message.get("data")
    if not isinstance(data, list) or not data:
        LOGGER.warning("topo/add/notify message has no device data: %s", message)
        return

    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None or not hasattr(runtime_data, "publish_subdevice_bind"):
        LOGGER.warning("Cannot handle topo/add/notify: no MQTT client")
        return

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    bind_children: list[dict[str, Any]] = []
    response_children: list[dict[str, str]] = []
    pending_bind_client_ids: set[str] = set()

    for item in data:
        if not isinstance(item, dict):
            continue
        product_id = item.get("productId")
        client_id = item.get("clientId")
        if (
            not isinstance(product_id, str) or not product_id
            or not isinstance(client_id, str) or not client_id
        ):
            continue

        # client_id is the HA device ID
        device = device_registry.async_get(client_id)
        if device is None:
            LOGGER.debug("topo/add/notify: device not found for clientId=%s", client_id)
            continue

        device_name = device.name_by_user or device.name or device.id

        context = _build_mapping_context(hass)
        default_properties: list[dict[str, Any]] = []

        # PID-first: the cloud already told us the productId, so resolve that
        # spec's DP routes for this HA device and build the physical model
        # (defaultProperties: min/max/step/range) from each route's typespec +
        # the carrier entity's live capabilities — no legacy mapping metadata.
        bind_result = pidspec_resolve_bind(hass, client_id, product_id, "")
        if bind_result is None:
            # pidspec is the only bind path (mirrors bind_response/topo-get):
            # if no PidSpec resolves, skip binding rather than fall back to the
            # retired legacy mapping layer.
            LOGGER.warning(
                "topo/add/notify: pidspec cannot resolve clientId=%s productId=%s; "
                "skipping bind (legacy mapping retired)",
                client_id, product_id,
            )
            continue
        default_properties = pidspec_build_default_properties(
            hass, bind_result.route_table, context
        )

        bind_child: dict[str, Any] = {
            "productId": product_id,
            "clientId": client_id,
            "deviceName": device_name,
        }
        if default_properties:
            bind_child["defaultProperties"] = default_properties
        bind_children.append(bind_child)
        pending_bind_client_ids.add(client_id)

        response_children.append({
            "clientId": client_id,
            "productId": product_id,
        })

    # Send response back to cloud with the same msgId
    if response_children and hasattr(runtime_data, "publish_topo_add_notify_response"):
        runtime_data.publish_topo_add_notify_response(msg_id, response_children)

    if not bind_children:
        LOGGER.debug("topo/add/notify: no valid bind candidates found")
        return
    _async_mark_pending_bind_clients(
        hass, entry.entry_id, pending_bind_client_ids
    )
    runtime_data.publish_subdevice_bind(bind_children)


async def _async_send_immediate_property_report(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    tuya_device_id: str,
    domain: str,
    entity_id: str,
    tuya_command_data: dict[str, Any] | None = None,
    category_code: str | None = None,
) -> None:
    """Send an immediate property report for a child device.

    Used as an acknowledgment after executing a property/set command so
    the Tuya cloud always receives a report, even when the HA entity
    state does not change (e.g. work_mode=colour without colour_data).

    If *tuya_command_data* is provided, explicitly commanded property
    values (e.g. ``work_mode``) are merged into the report so the cloud
    sees the mode that was requested, not the stale HA-side mode.

    Mirrors the full-state report paths: PID-level full-device payload is
    primary, legacy single-entity mapping is the fallback (no route table).
    Keeping this consistent with _async_flush_device_report avoids the ACK
    and the debounced flush disagreeing on a DP (e.g. water_heater switch).
    """
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None or not hasattr(
        runtime_data, "publish_child_property_report"
    ):
        return

    context = _build_mapping_context(hass, runtime_data, tuya_device_id)

    # Primary path: assemble the COMPLETE PID payload from all bound entities.
    properties = pidspec_build_full_device_properties(
        hass, tuya_device_id, context=context
    )
    if properties is None:
        # Binding is pidspec-gated; a missing route table means the device is
        # not (or no longer) pidspec-bound. Skip rather than use legacy mapping.
        LOGGER.warning(
            "ack report: no pidspec route table for tuya_device_id=%s; "
            "skipping (legacy mapping retired)",
            tuya_device_id,
        )
        return
    if not properties:
        return
    # Override reported values with explicitly commanded properties so the
    # cloud receives the mode/value that was actually requested.
    if tuya_command_data:
        now = int(time.time() * 1000)
        for key, value in tuya_command_data.items():
            if key in properties:
                properties[key] = {
                    "time": now,
                    "value": value,
                }

    LOGGER.debug(
        "Sending immediate property report tuya_device_id=%s "
        "entity_id=%s properties=%s",
        tuya_device_id,
        entity_id,
        _property_values(properties),
    )
    await hass.async_add_executor_job(
        runtime_data.publish_child_property_report,
        tuya_device_id,
        properties,
    )


async def async_handle_subdevice_message(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    topic: str,
    payload: bytes,
) -> None:
    """Log and route child-device MQTT messages."""
    LOGGER.debug("Received Tuya child message topic=%s payload=%s", topic, payload)

    if not (bindings := await async_load_gateway_bindings(hass, entry.entry_id)):
        return

    try:
        message = json.loads(payload)
    except json.JSONDecodeError:
        LOGGER.warning("Failed to decode Tuya child payload (length=%s)", len(payload))
        return

    topic_parts = topic.split("/")
    if len(topic_parts) < 4 or topic_parts[0] != "tylink":
        return

    tuya_device_id = topic_parts[1]
    ha_device_id = next(
        (
            device_id
            for device_id, device_data in bindings.get("devices", {}).items()
            if device_data.get("tuya_device_id") == tuya_device_id
        ),
        None,
    )
    if ha_device_id is None:
        return
    runtime_data = getattr(entry, "runtime_data", None)

    topic_suffix = "/".join(topic_parts[2:])
    if topic_suffix == "thing/property/set":
        if not isinstance(message.get("data"), dict):
            return
        device_data = bindings["devices"].get(ha_device_id)
        if not isinstance(device_data, dict):
            return
        entity_id = _device_primary_entity_id(device_data)
        if not isinstance(entity_id, str):
            return
        if not (domain := _device_bound_domain(bindings, device_data)):
            return
        category_code = _device_bound_category_code(bindings, device_data) or None
        if (
            domain == "cover"
            and "control" in message["data"]
            and hasattr(runtime_data, "remember_child_cover_command")
        ):
            runtime_data.remember_child_cover_command(
                tuya_device_id, message["data"]["control"]
            )
        context = _build_mapping_context(hass, runtime_data, tuya_device_id)
        service_calls = pidspec_build_service_calls(
            hass, tuya_device_id, message["data"], entity_id, context=context
        )
        if service_calls is None:
            # Binding is pidspec-gated; a missing route table means the device is
            # not pidspec-bound. Skip rather than route via legacy mapping.
            LOGGER.warning(
                "inbound command: no pidspec route table for tuya_device_id=%s; "
                "dropping command (legacy mapping retired)",
                tuya_device_id,
            )
        if service_calls:
            LOGGER.debug(
                "Routing Tuya child property set tuya_device_id=%s "
                "ha_device_id=%s entity_id=%s domain=%s service_calls=%s data=%s" ,
                tuya_device_id,
                ha_device_id,
                entity_id,
                domain,
                service_calls,
                message["data"],
            )
            await async_execute_service_calls(hass, service_calls)
            # Send an immediate property report as acknowledgment.
            # The STATE_CHANGED listener will send another report if
            # the state actually changes, but when the command does not
            # alter HA state (e.g. work_mode=colour without colour_data)
            # this ensures the cloud still receives a report.
            await _async_send_immediate_property_report(
                hass, entry, tuya_device_id, domain, entity_id,
                tuya_command_data=message["data"],
                category_code=category_code,
            )
            return
        LOGGER.debug("Unhandled Tuya child property set: %s", message)
        return

    if topic_suffix == "thing/action/execute":
        LOGGER.debug("Unhandled Tuya child action: %s", message)


async def async_handle_device_unbind_notice(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    payload: bytes,
) -> None:
    """Handle gateway unbind notice from the Tuya cloud.

    When the user deletes the gateway from the Tuya app, the cloud sends
    a message to tylink/${deviceId}/device/unbind/notice.  This triggers
    automatic removal of the HA config entry for this gateway.
    """
    try:
        message = json.loads(payload)
    except json.JSONDecodeError:
        LOGGER.warning("Failed to decode device unbind notice payload (length=%s)", len(payload))
        return

    # Remove all sub-device bindings first so HA devices are disassociated.
    sub_dev_ids = message.get("data", {}).get("subDevIds", [])
    if sub_dev_ids:
        await _async_remove_subdevice_bindings(
            hass,
            entry,
            sub_dev_ids,
            # Gateway unbind removes the parent integration, not an
            # individual child device. Keep child devices eligible for
            # later topo/get restore if the gateway is added again.
            mark_deleted_from_tuya_side=False,
        )

    # Remove the config entry, which triggers async_unload_entry
    # (disconnects MQTT) and async_remove_entry (cloud cleanup).
    await hass.config_entries.async_remove(entry.entry_id)


async def _async_handle_gateway_message(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    topic: str,
    payload: bytes,
) -> None:
    """Handle gateway-scoped MQTT messages."""
    LOGGER.debug("Received Tuya gateway message topic=%s payload=%s", topic, payload)
    gateway_device_id = entry.data.get(CONF_DEVICE_ID)
    if not isinstance(gateway_device_id, str) or not gateway_device_id:
        return

    if topic == TUYA_LINK_TOPIC_SUBDEVICE_BIND_RESPONSE.format(
        device_id=gateway_device_id
    ):
        await async_handle_subdevice_bind_response(hass, entry, payload)
        return

    if topic == TUYA_LINK_TOPIC_SUBDEVICE_DELETE.format(device_id=gateway_device_id):
        await async_handle_subdevice_delete(hass, entry, payload)
        return

    if topic == TUYA_LINK_TOPIC_SUBDEVICE_DELETE_RESPONSE.format(
        device_id=gateway_device_id
    ):
        await async_handle_subdevice_delete_response(hass, entry, payload)
        return

    if topic == TUYA_LINK_TOPIC_TOPO_CHANGE.format(device_id=gateway_device_id):
        await async_handle_topology_change(hass, entry, payload)
        return

    if topic == TUYA_LINK_TOPIC_TOPO_GET_RESPONSE.format(
        device_id=gateway_device_id
    ):
        await async_handle_topo_get_response(hass, entry, payload)
        return

    if topic == TUYA_LINK_TOPIC_SUBDEVICE_TO_BIND.format(
        device_id=gateway_device_id
    ):
        await async_handle_subdevice_to_bind(hass, entry, payload)
        return

    if topic == TUYA_LINK_TOPIC_DEVICE_UNBIND_NOTICE.format(
        device_id=gateway_device_id
    ):
        await async_handle_device_unbind_notice(hass, entry, payload)
        return

    if topic.endswith("/thing/property/set") or topic.endswith("/thing/action/execute"):
        await async_handle_subdevice_message(hass, entry, topic, payload)
        return

    if topic.endswith("/thing/property/report_response"):
        topic_parts = topic.split("/")
        if len(topic_parts) >= 2 and topic_parts[0] == "tylink":
            tuya_device_id = topic_parts[1]
            await async_handle_property_report_response(
                hass, entry, tuya_device_id, payload
            )


async def async_setup_entry(
    hass: HomeAssistant, entry: TuyaHaNewConfigEntry
) -> bool:
    """Set up Tuya HA New from a config entry."""
    entry.runtime_data = None

    credentials = {
        CONF_API_KEY: entry.data.get(CONF_API_KEY),
        CONF_PRODUCT_ID: entry.data.get(CONF_PRODUCT_ID),
        CONF_DEVICE_ID: entry.data.get(CONF_DEVICE_ID),
        CONF_DEVICE_SECRET: entry.data.get(CONF_DEVICE_SECRET),
    }

    product_id = credentials.get(CONF_PRODUCT_ID)
    device_id = credentials.get(CONF_DEVICE_ID)
    device_secret = credentials.get(CONF_DEVICE_SECRET)

    if (not product_id or not device_id or not device_secret) and (
        stored_credentials := await async_load_gateway_credentials(hass)
    ):
        credentials = stored_credentials
        product_id = credentials.get(CONF_PRODUCT_ID)
        device_id = credentials.get(CONF_DEVICE_ID)
        device_secret = credentials.get(CONF_DEVICE_SECRET)

        hass.config_entries.async_update_entry(
            entry,
            data=credentials,
            title=credentials.get(CONF_API_KEY, entry.title),
        )

    if not product_id or not device_id or not device_secret:
        # Gateway not created yet; integration is loaded but no MQTT.
        return True

    api_key = credentials.get(CONF_API_KEY)

    # Initialize pidspec inference engine (loads local rules, sets up cloud client)
    await async_init_pidspec(
        hass,
        api_key if isinstance(api_key, str) else None,
        gateway_id=device_id if isinstance(device_id, str) else None,
    )

    # Startup rule sync (doc §3.1 timing 1): unconditionally check the cloud rule
    # version and pull a newer bundle, so a restart always lands on the latest
    # rules even when local rules are non-empty (async_init_pidspec only
    # bootstraps from cloud when the local cache is EMPTY). republish=False — the
    # topo/get inference pass later in this setup runs against the fresh cache.
    await async_check_and_refresh_pidspec_rules(hass, entry, republish=False)

    def on_connect() -> None:
        hass.loop.call_soon_threadsafe(
            lambda: hass.async_create_task(
                _notify_connected(hass, entry, client)
            )
        )

    def on_disconnect(reason_code: int, _msg: str | None) -> None:
        hass.loop.call_soon_threadsafe(
            lambda: hass.async_create_task(
                _notify_disconnected(hass, entry.entry_id, reason_code)
            )
        )

    def on_message(topic: str, payload: bytes) -> None:
        hass.loop.call_soon_threadsafe(
            lambda: hass.async_create_task(
                _async_handle_gateway_message(hass, entry, topic, payload)
            )
        )

    client = async_take_pending_gateway_client(hass, device_id)
    if client is not None and client.connected:
        client.set_callbacks(
            on_connect=on_connect,
            on_disconnect=on_disconnect,
            on_message=on_message,
        )
    else:
        if client is not None:
            await hass.async_add_executor_job(client.disconnect)
        client = TuyaLinkMqttClient(
            product_id=product_id,
            device_id=device_id,
            device_secret=device_secret,
            broker=get_region_endpoints_for_api_key(
                credentials.get(CONF_API_KEY)
            ).mqtt_broker,
            on_connect=on_connect,
            on_disconnect=on_disconnect,
            on_message=on_message,
        )
        try:
            await hass.async_add_executor_job(client.connect)
        except Exception as err:
            raise ConfigEntryNotReady(
                f"Tuya Link MQTT connection failed: {err}"
            ) from err
    entry.runtime_data = client

    # No local bindings are loaded here: bindings are not persisted to disk.
    # The Tuya cloud topo/get is the single source of truth — the initial sync
    # below pulls the historically-bound sub-devices from the cloud and
    # async_handle_topo_get_response rebuilds the in-memory bindings (filter +
    # inference → unbind non-conforming / bind + subscribe conforming). A short
    # initialization window after startup/binding is expected and accepted.

    # Initial topo/get sync timing:
    # - Cold-start (HA still booting): wait for EVENT_HOMEASSISTANT_STARTED so
    #   all other integrations finish async_setup_entry, then a grace delay
    #   (_POST_STARTED_GRACE_SECONDS) so their entities populate live state.
    #   Running topo/get too early misreads pending-state entities as offline
    #   (state==None or STATE_UNAVAILABLE) and unbinds good devices.
    # - Hot reload (HA already running): the cold-start race doesn't apply;
    #   keep the short _INITIAL_SYNC_DELAY_SECONDS so the config-flow dialog
    #   finishes rendering before bindings appear on the integration page.
    # Online and state reports are deferred to async_handle_topo_get_response
    # so they run AFTER cloud reconciliation.
    async def _async_initial_sync(_now: Any) -> None:
        await async_request_topo_sync(hass, entry)

    if hass.state == CoreState.running:
        entry.async_on_unload(
            async_call_later(
                hass, _INITIAL_SYNC_DELAY_SECONDS, _async_initial_sync
            )
        )
    else:
        # async_listen_once self-removes its bus listener when the event fires.
        # Wrapping that unsub directly in async_on_unload would remove it a
        # SECOND time at unload → EventBus._async_remove_listener raises
        # "ValueError: list.remove(x): x not in list". Track the unsub and only
        # cancel it on unload if EVENT_HOMEASSISTANT_STARTED has NOT fired yet
        # (entry unloaded mid-startup); once it fires the listener is gone.
        unsub_ha_started = None

        @callback
        def _on_ha_started(_event: Event) -> None:
            nonlocal unsub_ha_started
            unsub_ha_started = None  # listener already auto-removed by listen_once
            entry.async_on_unload(
                async_call_later(
                    hass, _POST_STARTED_GRACE_SECONDS, _async_initial_sync
                )
            )

        unsub_ha_started = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, _on_ha_started
        )

        @callback
        def _cancel_ha_started_listener() -> None:
            if unsub_ha_started is not None:
                unsub_ha_started()

        entry.async_on_unload(_cancel_ha_started_listener)

    # Periodic cloud topo sync every TOPO_SYNC_INTERVAL_SECONDS (1 hour).
    async def _async_periodic_topo_sync(_now: Any) -> None:
        """Periodically reconcile local bindings with cloud topo."""
        await async_request_topo_sync(hass, entry)

    entry.async_on_unload(
        async_track_time_interval(
            hass,
            _async_periodic_topo_sync,
            timedelta(seconds=TOPO_SYNC_INTERVAL_SECONDS),
        )
    )

    # Periodic full state report to keep Tuya cloud in sync.
    async def _async_periodic_state_report(_now: Any) -> None:
        """Periodically report all bound device states to Tuya cloud."""
        await _async_report_all_bound_device_states(hass, entry, client)

    entry.async_on_unload(
        async_track_time_interval(
            hass,
            _async_periodic_state_report,
            timedelta(seconds=TOPO_SYNC_INTERVAL_SECONDS),
        )
    )

    # Periodic pidspec rule version check — fetch new rules and re-infer.
    async def _async_periodic_rule_check(_now: Any) -> None:
        """Check cloud rule version; if newer, fetch and re-publish eligible."""
        await async_check_and_refresh_pidspec_rules(hass, entry)

    entry.async_on_unload(
        async_track_time_interval(
            hass,
            _async_periodic_rule_check,
            timedelta(seconds=RULE_CHECK_INTERVAL_SECONDS),
        )
    )

    async def _async_resync_gateway_devices() -> None:
        """Run a resync for registry-driven updates.

        Binding maintenance (refresh stored name/entities, re-associate config
        entry, prune stale bindings for already-bound devices) ALWAYS runs — it
        is local and cheap, and skipping it would regress rename/entity-change
        handling. Inference + cloud reporting only runs when an inferable
        device's structural fingerprint actually changed, so cosmetic registry
        churn no longer re-infers and re-reports low-confidence devices.
        publish_eligible itself lazily fetches newer cloud rules when it still
        finds low-confidence devices, so no explicit rule check is needed here.
        """
        await async_sync_gateway_devices_for_entry(hass, entry)
        fingerprints = _compute_device_fingerprints(hass, entry)
        if fingerprints == _DEVICE_FINGERPRINTS.get(entry.entry_id):
            LOGGER.debug(
                "resync: inferable device profiles unchanged (%d devices), "
                "skipping re-inference",
                len(fingerprints),
            )
            return
        _DEVICE_FINGERPRINTS[entry.entry_id] = fingerprints
        await async_publish_eligible_ha_devices(hass, entry)

    sync_debouncer = Debouncer(
        hass,
        LOGGER,
        cooldown=REGISTRY_SYNC_DEBOUNCE_SECONDS,
        immediate=False,
        function=_async_resync_gateway_devices,
        background=True,
    )

    @callback
    def _async_schedule_registry_resync(
        event: Event[
            er.EventEntityRegistryUpdatedData | dr.EventDeviceRegistryUpdatedData
        ],
    ) -> None:
        """Schedule a debounced registry resync.

        Registry events (entity/device updated) always trigger resync.
        State changes only trigger resync on availability transitions
        (online↔offline); regular state changes just report to Tuya.
        """
        if event.event_type == EVENT_STATE_CHANGED:
            entity_id = event.data.get("entity_id")
            if isinstance(entity_id, str):
                hass.async_create_task(
                    _async_report_bound_entity_state_change(
                        hass,
                        entry,
                        entity_id,
                        event.data.get("old_state"),
                        event.data.get("new_state"),
                    )
                )
                hass.async_create_task(
                    _async_handle_bound_entity_availability_change(
                        hass,
                        entry,
                        entity_id,
                        event.data.get("old_state"),
                        event.data.get("new_state"),
                    )
                )
            # Only trigger resync+inference on availability change
            old_state = event.data.get("old_state")
            new_state = event.data.get("new_state")
            if _state_is_available(old_state) != _state_is_available(new_state):
                sync_debouncer.async_schedule_call()
            return

        # Registry events always trigger resync
        sync_debouncer.async_schedule_call()

    entry.async_on_unload(sync_debouncer.async_shutdown)

    @callback
    def _async_drop_device_fingerprints() -> None:
        """Drop cached fingerprints on unload (must not return the popped dict)."""
        _DEVICE_FINGERPRINTS.pop(entry.entry_id, None)

    entry.async_on_unload(_async_drop_device_fingerprints)
    entry.async_on_unload(
        hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED,
            _async_schedule_registry_resync,
        )
    )
    entry.async_on_unload(
        hass.bus.async_listen(
            dr.EVENT_DEVICE_REGISTRY_UPDATED,
            _async_schedule_registry_resync,
        )
    )
    entry.async_on_unload(
        hass.bus.async_listen(
            EVENT_STATE_CHANGED,
            _async_schedule_registry_resync,
        )
    )

    @callback
    def _async_on_core_config_update(event: Event) -> None:
        """Re-report bound device states when the temperature unit changes."""
        if "unit_system" not in event.data:
            return
        LOGGER.debug(
            "Temperature unit changed (unit_system=%s), re-reporting bound device states",
            event.data.get("unit_system"),
        )
        hass.async_create_task(
            _async_report_all_bound_device_states(hass, entry, client)
        )

    entry.async_on_unload(
        hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, _async_on_core_config_update)
    )

    return True


async def _notify_connected(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    client: TuyaLinkMqttClient,
) -> None:
    """Notify that Tuya Link MQTT is connected (run in event loop)."""
    await _async_report_subdevices_online(hass, entry, client)
    await _async_report_all_bound_device_states(hass, entry, client)


async def _notify_disconnected(
    hass: HomeAssistant, entry_id: str, reason_code: int
) -> None:
    """Notify that Tuya Link MQTT disconnected (run in event loop)."""
    # Can be used to mark device offline or retry.


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Remove a device from the integration.

    Called by HA when the user removes a device via the UI.  We record
    the device in ``deleted_device_ids`` so that ``registry_sync`` and
    ``topo/get_response`` will not re-discover and re-bind it.  If the
    device has a cloud binding we also ask the cloud to unbind it.
    """
    bindings = await async_load_gateway_bindings(hass, entry.entry_id) or {}
    devices = dict(bindings.get("devices", {}))
    entities = dict(bindings.get("entities", {}))
    deleted_device_ids: list[str] = [
        did
        for did in bindings.get("deleted_device_ids", [])
        if isinstance(did, str) and did
    ]

    device_data = devices.pop(device_entry.id, None)
    if device_data is not None:
        for entity_id in device_data.get("entity_ids", []):
            entities.pop(entity_id, None)

        tuya_device_id = device_data.get("tuya_device_id")
        if isinstance(tuya_device_id, str) and tuya_device_id:
            runtime_data = getattr(entry, "runtime_data", None)
            if runtime_data is not None and hasattr(
                runtime_data, "publish_subdevice_delete"
            ):
                runtime_data.publish_subdevice_delete([tuya_device_id])
            if runtime_data is not None and hasattr(
                runtime_data, "unsubscribe_child_device_messages"
            ):
                runtime_data.unsubscribe_child_device_messages(tuya_device_id)

            from .pidspec_bridge import remove_route_table

            remove_route_table(hass, tuya_device_id)

    if device_entry.id not in set(deleted_device_ids):
        deleted_device_ids.append(device_entry.id)

    bindings["devices"] = devices
    bindings["entities"] = entities
    bindings["deleted_device_ids"] = deleted_device_ids
    await async_save_gateway_bindings(hass, entry.entry_id, bindings)

    LOGGER.info(
        "User removed device %s (%s) from integration, added to deleted list",
        device_entry.name or device_entry.id,
        device_entry.id,
    )
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: TuyaHaNewConfigEntry
) -> bool:
    """Unload a config entry and disconnect MQTT."""
    _get_topo_sync_sessions(hass).pop(entry.entry_id, None)
    _get_pending_bind_clients(hass).pop(entry.entry_id, None)
    # Module-level global (survives unload/reload, only cleared on process
    # restart). Drop this entry's fingerprints so inference gating re-evaluates
    # cleanly after a reinstall instead of reusing pre-uninstall state.
    _DEVICE_FINGERPRINTS.pop(entry.entry_id, None)

    # Route tables live in hass.data[DOMAIN] keyed by tuya_device_id and are NOT
    # scoped by entry_id, so they survive unload/reload within a live process.
    # Drop this entry's route tables so a reinstall starts clean — a full HA
    # restart clears hass.data, which is why restart "fixes" stale-binding bugs.
    from .pidspec_bridge import remove_route_table

    _unload_bindings = await async_load_gateway_bindings(hass, entry.entry_id) or {}
    for _dev in _unload_bindings.get("devices", {}).values():
        _tid = _dev.get("tuya_device_id")
        if isinstance(_tid, str) and _tid:
            remove_route_table(hass, _tid)

    prefix = f"{entry.entry_id}:"
    for key in [k for k in _pending_device_reports if k.startswith(prefix)]:
        handle = _pending_device_reports.pop(key, None)
        if handle is not None:
            handle.cancel()

    client = getattr(entry, "runtime_data", None)
    if client:
        await hass.async_add_executor_job(client.disconnect)
        entry.runtime_data = None
    return True


async def async_remove_entry(
    hass: HomeAssistant, entry: TuyaHaNewConfigEntry
) -> None:
    """Remove a config entry and delete the gateway from the Tuya cloud.

    The cloud delete only unbinds the gateway from the family — sub-device
    relationships are preserved server-side and can be queried via
    ``tylink/${deviceId}/device/topo/get``.  Bindings are not persisted to
    disk, so we just drop the in-memory map for this entry; a subsequent
    re-integration rebuilds bindings from the cloud topo/get.
    """
    from .tuya_openapi import TuyaOpenApiError, async_delete_ha_gateway

    api_key = entry.data.get(CONF_API_KEY)
    device_id = entry.data.get(CONF_DEVICE_ID)

    if isinstance(api_key, str) and api_key and isinstance(device_id, str) and device_id:
        try:
            await async_delete_ha_gateway(hass, api_key, device_id)
            LOGGER.info("Deleted gateway device %s from Tuya cloud", device_id)
        except TuyaOpenApiError as err:
            LOGGER.warning(
                "Failed to delete gateway device %s from Tuya cloud: %s",
                device_id,
                err,
            )
        except Exception:
            LOGGER.exception(
                "Unexpected error deleting gateway device %s from Tuya cloud",
                device_id,
            )

    # Drop in-memory bindings for this entry. Credentials remain on disk so a
    # re-integration can reuse the same virtual gateway; the sub-device
    # mappings are rebuilt from the cloud topo/get, not from local state.
    await async_delete_gateway_bindings(hass, entry.entry_id)
