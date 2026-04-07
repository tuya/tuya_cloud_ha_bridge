"""Tuya HA New Gateway: HA plugin gateway via Tuya Link MQTT."""

from __future__ import annotations

import asyncio
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
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, State, callback
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
    TUYA_LINK_TOPIC_DEVICE_UNBIND_NOTICE,
    TUYA_LINK_TOPIC_SUBDEVICE_DELETE,
    TUYA_LINK_TOPIC_SUBDEVICE_BIND_RESPONSE,
    TUYA_LINK_TOPIC_SUBDEVICE_DELETE_RESPONSE,
    TUYA_LINK_TOPIC_SUBDEVICE_TO_BIND,
    TUYA_LINK_TOPIC_TOPO_CHANGE,
    TUYA_LINK_TOPIC_TOPO_GET_RESPONSE,
)
from .mapping_runtime import (
    async_ensure_mapping_module_loaded,
    async_ensure_pid_mapping_loaded,
    async_load_pid_mapping_from_cloud,
    build_service_calls_from_tuya,
    build_tuya_properties_from_state,
    get_product_id_for_domain,
    get_property_metadata,
    has_mapping_module,
    infer_domain_for_device,
    select_preferred_domain,
)
from .region_mapping import get_region_endpoints_for_api_key
from .storage import (
    async_load_gateway_bindings,
    async_load_gateway_credentials,
    async_migrate_gateway_bindings,
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
_PENDING_GATEWAY_CLIENTS = "pending_gateway_clients"
_PENDING_BIND_CLIENTS = "pending_bind_clients"
_TOPO_SYNC_SESSIONS = "topo_sync_sessions"
# Delay (seconds) before the first topo sync after setup, so the HA frontend
# finishes rendering the config-flow completion dialog before any devices get
# associated with the config entry (which would trigger the "created devices"
# popup).
_INITIAL_SYNC_DELAY_SECONDS = 5
_TOPO_SYNC_SESSION_TIMEOUT_SECONDS = 30


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
    """Report bound child devices online through the gateway."""
    if entry.runtime_data is not client:
        return

    if child_device_ids is None:
        bindings = await async_load_gateway_bindings(hass, entry.entry_id)
        if not bindings:
            return
        child_device_ids = [
            tuya_device_id
            for device_data in bindings.get("devices", {}).values()
            if isinstance(
                tuya_device_id := device_data.get("tuya_device_id"),
                str,
            )
            and tuya_device_id
        ]

    if not child_device_ids:
        return

    # Preserve order while avoiding duplicate online reports.
    client.publish_subdevice_login(list(dict.fromkeys(child_device_ids)))


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

        primary_entity_id = device_data.get("primary_entity_id")
        if not isinstance(primary_entity_id, str):
            entity_ids = device_data.get("entity_ids")
            if isinstance(entity_ids, list) and entity_ids:
                primary_entity_id = entity_ids[0]
            else:
                continue

        entity_state = hass.states.get(primary_entity_id)
        if not _state_can_be_reported(entity_state):
            continue

        domain = device_data.get("domain")
        if not isinstance(domain, str):
            entity_data = entities.get(primary_entity_id)
            if entity_data is None:
                continue
            domain = str(entity_data["domain"])

        await async_ensure_mapping_module_loaded(hass, domain)
        properties = build_tuya_properties_from_state(domain, entity_state)
        if not properties:
            continue

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


async def _async_report_bound_entity_state_change(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
    entity_id: str,
    old_state: State | None,
    new_state: State | None,
) -> None:
    """Publish an immediate child-device batch report for state changes."""
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

    primary_entity_id = _device_primary_entity_id(device_data)
    if primary_entity_id is not None and entity_id != primary_entity_id:
        return

    if not (domain := _device_bound_domain(bindings, device_data)):
        return

    tuya_device_id = device_data.get("tuya_device_id")
    if not isinstance(tuya_device_id, str) or not tuya_device_id:
        return

    await async_ensure_mapping_module_loaded(hass, domain)

    LOGGER.debug(
        "Evaluating bound entity change entity_id=%s device_id=%s "
        "tuya_device_id=%s old_state=%s new_state=%s",
        entity_id,
        device_id,
        tuya_device_id,
        old_state.state,
        new_state.state,
    )

    if not (new_properties := build_tuya_properties_from_state(domain, new_state)):
        return

    old_properties = (
        build_tuya_properties_from_state(domain, old_state)
        if _state_can_be_reported(old_state)
        else {}
    )
    old_values = _property_values(old_properties)
    new_values = _property_values(new_properties)
    if old_values == new_values:
        LOGGER.debug(
            "Skipping bound entity report entity_id=%s device_id=%s "
            "tuya_device_id=%s because mapped properties did not change",
            entity_id,
            device_id,
            tuya_device_id,
        )
        return

    LOGGER.debug(
        "Reporting bound entity state change entity_id=%s device_id=%s "
        "tuya_device_id=%s old_values=%s new_values=%s",
        entity_id,
        device_id,
        tuya_device_id,
        old_values,
        new_values,
    )
    await hass.async_add_executor_job(
        runtime_data.publish_child_property_report,
        tuya_device_id,
        new_properties,
    )


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
    from .mapping_runtime import get_supported_domains_in_order

    try:
        message = json.loads(payload)
    except json.JSONDecodeError:
        LOGGER.warning("Failed to decode child bind response payload (length=%s)", len(payload))
        return

    code = message.get("code", -1)
    if code != 0:
        LOGGER.warning("Child bind response rejected (code=%s): %s", code, message)
        return

    response_data = message.get("data", [])
    if not isinstance(response_data, list) or not response_data:
        LOGGER.warning("Child bind response has no data: %s", message)
        return

    runtime_data = getattr(entry, "runtime_data", None)
    await async_ensure_pid_mapping_loaded(hass)
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

        device_name = device.name or device.id

        # Determine the domain from product_id or existing binding
        selected_domain: str | None = None
        if isinstance(product_id, str) and product_id:
            for domain in get_supported_domains_in_order():
                if get_product_id_for_domain(domain) == product_id:
                    selected_domain = domain
                    break

        # Fall back to existing binding info if product_id not in response
        if selected_domain is None and client_id in devices_map:
            existing_domain = devices_map[client_id].get("domain")
            if isinstance(existing_domain, str) and existing_domain:
                selected_domain = existing_domain

        if not selected_domain:
            LOGGER.debug(
                "bind_response: no domain resolved for clientId=%s productId=%s",
                client_id,
                product_id,
            )
            continue

        # Resolve product_id if not provided in response
        if not isinstance(product_id, str) or not product_id:
            product_id = get_product_id_for_domain(selected_domain)

        # Gather matching entities
        selected_entities: list[str] = []
        primary_entity_id: str | None = None

        for entity_entry in er.async_entries_for_device(
            entity_registry, device.id
        ):
            if entity_entry.disabled_by is not None:
                continue
            if entity_entry.domain != selected_domain:
                continue
            selected_entities.append(entity_entry.entity_id)
            if primary_entity_id is None:
                primary_entity_id = entity_entry.entity_id

        if not selected_entities or primary_entity_id is None:
            LOGGER.debug(
                "bind_response: no matching entities for clientId=%s domain=%s",
                client_id,
                selected_domain,
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
            "product_id": product_id,
            "client_id": client_id,
            "tuya_device_id": tuya_device_id,
        }
        for eid in selected_entities:
            entities_map[eid] = {
                "device_id": device.id,
                "domain": selected_domain,
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
    from .mapping_runtime import get_supported_domains_in_order

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

        device_name = device.name or device.id

        # Determine the domain from product_id.
        selected_domain: str | None = None
        if isinstance(product_id, str) and product_id:
            for domain in get_supported_domains_in_order():
                if get_product_id_for_domain(domain) == product_id:
                    selected_domain = domain
                    break

        if not selected_domain:
            LOGGER.debug(
                "topo/get restore: no domain for productId=%s clientId=%s",
                product_id,
                client_id,
            )
            continue

        # Gather matching entities.
        selected_entities: list[str] = []
        primary_entity_id: str | None = None

        for entity_entry in er.async_entries_for_device(
            entity_registry, device.id
        ):
            if entity_entry.disabled_by is not None:
                continue
            if entity_entry.domain != selected_domain:
                continue
            selected_entities.append(entity_entry.entity_id)
            if primary_entity_id is None:
                primary_entity_id = entity_entry.entity_id

        if not selected_entities or primary_entity_id is None:
            LOGGER.debug(
                "topo/get restore: no entities for clientId=%s domain=%s",
                client_id,
                selected_domain,
            )
            continue

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
            "product_id": product_id,
            "client_id": client_id,
            "tuya_device_id": tuya_device_id,
        }
        for eid in selected_entities:
            entities_map[eid] = {
                "device_id": device.id,
                "domain": selected_domain,
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

    if (
        not stale_local_device_ids
        and not orphaned_cloud_tuya_ids
        and restored_count == 0
        and refreshed_count == 0
    ):
        LOGGER.debug(
            "topo/get sync: all %s local bindings still present in cloud "
            "and all HA devices exist",
            len(bindings.get("devices", {})),
        )

    await async_publish_eligible_ha_devices(hass, entry)

    # Report sub-devices online and their current states AFTER cloud
    # reconciliation so we never report devices that the cloud has deleted.
    if runtime_data is not None:
        await _async_report_subdevices_online(hass, entry, runtime_data)
        await _async_report_all_bound_device_states(hass, entry, runtime_data)


async def async_publish_eligible_ha_devices(
    hass: HomeAssistant,
    entry: TuyaHaNewConfigEntry,
) -> None:
    """Scan HA registries and publish eligible sub-devices via MQTT (Step 4).

    Publishes to tylink/${deviceId}/ha/devices so the app can show
    the user which HA devices are available for binding.
    """
    from .registry_sync import (
        _is_excluded_device,
        _is_excluded_domain,
        _async_entry_domain,
    )

    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None or not hasattr(runtime_data, "publish_ha_devices"):
        LOGGER.debug("Cannot publish HA devices: no MQTT client")
        return

    await async_ensure_pid_mapping_loaded(hass)

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    area_registry = ar.async_get(hass)
    ha_devices: list[dict[str, str]] = []

    for device in device_registry.devices.values():
        if _is_excluded_device(hass, device, entry.entry_id):
            continue

        # Collect all entity domains for this device.
        entity_domains: set[str] = set()
        has_available_entity = False
        for entity_entry in er.async_entries_for_device(entity_registry, device.id):
            if entity_entry.disabled_by is not None:
                continue
            if _is_excluded_domain(_async_entry_domain(hass, entity_entry.config_entry_id)):
                continue
            entity_domains.add(entity_entry.domain)
            if not has_available_entity:
                state = hass.states.get(entity_entry.entity_id)
                if _state_is_available(state):
                    has_available_entity = True

        if not entity_domains or not has_available_entity:
            continue

        # Pick the highest-priority domain, then check cloud PID mapping.
        selected_domain = infer_domain_for_device(entity_domains)
        if not selected_domain:
            continue

        if not has_mapping_module(selected_domain):
            continue

        product_id = get_product_id_for_domain(selected_domain)
        if not product_id:
            continue

        ha_device: dict[str, str] = {
            "productId": product_id,
            "clientId": device.id,
            "deviceName": device.name or device.id,
            "deviceType": selected_domain,
        }

        if device.area_id:
            area_entry = area_registry.async_get_area(device.area_id)
            if area_entry is not None:
                ha_device["area"] = area_entry.name

        ha_devices.append(ha_device)

    runtime_data.publish_ha_devices(ha_devices)


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
    from .mapping_runtime import get_supported_domains_in_order

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

    await async_ensure_pid_mapping_loaded(hass)

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

        device_name = device.name or device.id

        # Find the matching domain for this product_id
        selected_domain: str | None = None
        for domain in get_supported_domains_in_order():
            if get_product_id_for_domain(domain) == product_id:
                selected_domain = domain
                break

        if not selected_domain:
            LOGGER.debug(
                "topo/add/notify: no domain found for productId=%s clientId=%s",
                product_id, client_id,
            )
            continue

        # Gather entity state for defaultProperties
        selected_entities: list[str] = []
        primary_entity_id: str | None = None

        for entity_entry in er.async_entries_for_device(entity_registry, device.id):
            if entity_entry.disabled_by is not None:
                continue
            if entity_entry.domain != selected_domain:
                continue
            selected_entities.append(entity_entry.entity_id)
            if primary_entity_id is None:
                primary_entity_id = entity_entry.entity_id

        if not selected_entities or primary_entity_id is None:
            LOGGER.debug(
                "topo/add/notify: no matching entities for clientId=%s domain=%s",
                client_id, selected_domain,
            )
            continue

        # Build defaultProperties from current entity state
        default_properties: list[dict[str, Any]] = []
        await async_ensure_mapping_module_loaded(hass, selected_domain)
        metadata = get_property_metadata(selected_domain)
        entity_state = hass.states.get(primary_entity_id)
        reported_codes: set[str] = set()
        if entity_state is not None and _state_can_be_reported(entity_state):
            properties = build_tuya_properties_from_state(selected_domain, entity_state)
            if properties:
                for k, v in properties.items():
                    reported_codes.add(k)
                    value = v["value"]
                    meta = metadata.get(k, {})
                    # Apply value_map to convert Tuya values to HA values
                    if "value_map" in meta:
                        value = meta["value_map"].get(value, value)
                    prop: dict[str, Any] = {"code": k, "value": value}
                    # range: use get_capability to resolve from state or
                    # entity registry, ensuring range is available even when
                    # state attributes have not been populated yet.
                    if "range_attr" in meta:
                        cap_val = get_capability(
                            hass, primary_entity_id, meta["range_attr"]
                        )
                        if isinstance(cap_val, list) and cap_val:
                            prop["range"] = cap_val
                    elif "range" in meta:
                        prop["range"] = meta["range"]
                    default_properties.append(prop)

        # Append metadata-defined properties not already reported by
        # ha_to_tuya (e.g. mode when the entity state lacks that attribute)
        # so that Tuya cloud still receives their range.
        if metadata:
            for code, meta in metadata.items():
                if code in reported_codes:
                    continue
                if "range_attr" not in meta:
                    continue
                cap_val = get_capability(
                    hass, primary_entity_id, meta["range_attr"]
                )
                if not isinstance(cap_val, list) or not cap_val:
                    continue
                prop = {"code": code, "range": cap_val}
                # Apply value_map so range entries use HA values
                if "value_map" in meta:
                    prop["range"] = [
                        meta["value_map"].get(v, v) for v in cap_val
                    ]
                default_properties.append(prop)

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
) -> None:
    """Send an immediate property report for a child device.

    Used as an acknowledgment after executing a property/set command so
    the Tuya cloud always receives a report, even when the HA entity
    state does not change (e.g. work_mode=colour without colour_data).

    If *tuya_command_data* is provided, explicitly commanded property
    values (e.g. ``work_mode``) are merged into the report so the cloud
    sees the mode that was requested, not the stale HA-side mode.
    """
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None or not hasattr(
        runtime_data, "publish_child_property_report"
    ):
        return

    entity_state = hass.states.get(entity_id)
    if entity_state is None or not _state_can_be_reported(entity_state):
        return

    await async_ensure_mapping_module_loaded(hass, domain)
    properties = build_tuya_properties_from_state(domain, entity_state)
    if not properties:
        return

    # Override reported values with explicitly commanded properties so the
    # cloud receives the mode/value that was actually requested.
    if tuya_command_data:
        now = int(time.time() * 1000)
        for key in ("work_mode",):
            if key in tuya_command_data and key in properties:
                properties[key] = {
                    "time": now,
                    "value": tuya_command_data[key],
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
        await async_ensure_mapping_module_loaded(hass, domain)
        if service_calls := build_service_calls_from_tuya(
            domain,
            message["data"],
            entity_id,
        ):
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

    # Refresh category-PID mappings from cloud on every setup (restart/reload).
    api_key = credentials.get(CONF_API_KEY)
    if isinstance(api_key, str) and api_key:
        await async_load_pid_mapping_from_cloud(hass, api_key)
    else:
        await async_ensure_pid_mapping_loaded(hass)
    if get_product_id_for_domain("light") is None and get_product_id_for_domain("switch") is None:
        LOGGER.warning(
            "PID mapping cache is empty after setup — "
            "device/list/found will not be published until mappings are loaded"
        )
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

    # Try to load existing bindings for this entry.  If none exist, check
    # for orphaned bindings left by a previous entry (remove + re-add
    # scenario) and migrate them to the current entry_id.
    stored_bindings = await async_load_gateway_bindings(hass, entry.entry_id)
    if not stored_bindings:
        stored_bindings = await async_migrate_gateway_bindings(
            hass, entry.entry_id
        )

    if stored_bindings:
        for device_data in stored_bindings.get("devices", {}).values():
            tuya_device_id = device_data.get("tuya_device_id")
            if isinstance(tuya_device_id, str) and tuya_device_id:
                client.subscribe_child_device_messages(tuya_device_id)

    # Delay the initial sync (topo sync, online report) so the HA frontend
    # finishes rendering the config-flow completion dialog before any
    # devices get associated with the config entry. `topo/get_response`
    # will publish the eligible device list after cloud reconciliation.
    async def _async_initial_sync(_now: Any) -> None:
        """Run the initial sync after a short delay.

        Online report and state report are deferred to
        async_handle_topo_get_response so they run AFTER cloud
        reconciliation, avoiding reports for devices the cloud has
        already deleted.
        """
        await async_request_topo_sync(hass, entry)

    entry.async_on_unload(
        async_call_later(hass, _INITIAL_SYNC_DELAY_SECONDS, _async_initial_sync)
    )

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

    async def _async_resync_gateway_devices() -> None:
        """Run a full resync for registry-driven updates."""
        await async_sync_gateway_devices_for_entry(hass, entry)
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
        """Schedule a debounced registry resync."""
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
        sync_debouncer.async_schedule_call()

    entry.async_on_unload(sync_debouncer.async_shutdown)
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
    ``tylink/${deviceId}/device/topo/get``.  Therefore we intentionally keep
    local bindings and credentials so they can be restored when the user
    re-adds the integration.
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

    # Intentionally keep gateway_bindings and gateway_credentials on disk
    # so that a subsequent re-integration can restore the sub-device
    # mapping without requiring the user to re-bind every device.
