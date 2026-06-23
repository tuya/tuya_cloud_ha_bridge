"""Registry sync helpers for Tuya HA gateway bindings."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from . import TuyaHaNewConfigEntry
from .const import DOMAIN, LOGGER
from .storage import (
    GatewayBindingsData,
    async_load_gateway_bindings,
    async_save_gateway_bindings,
)

@dataclass(slots=True)
class BoundEntity:
    """Normalized entity data stored for gateway command routing."""

    entity_id: str
    device_id: str | None
    domain: str
    category_code: str = ""


def _async_entry_domain(hass: HomeAssistant, entry_id: str) -> str | None:
    """Return the domain for a config entry ID."""
    if config_entry := hass.config_entries.async_get_entry(entry_id):
        return config_entry.domain
    return None


_VIRTUAL_MANUFACTURERS = {
    "official apps",
    "home assistant",
    "template",
    "default",
    "unknown",
    "unknown manufacturer",
    "undefined",
    "",
}


def _is_excluded_domain(domain: str | None) -> bool:
    """Return if a config entry domain should be ignored."""
    return domain in {"tuya", DOMAIN}


def _is_virtual_device(device: dr.DeviceEntry) -> bool:
    """Return if a device is virtual and should be excluded from binding."""
    if device.entry_type == "service":
        return True

    manufacturer = (device.manufacturer or "").strip().lower()
    model = (device.model or "").strip()
    identifiers = device.identifiers

    if manufacturer in _VIRTUAL_MANUFACTURERS:
        return True

    if manufacturer == "mqtt":
        return not bool(model)

    return not bool(model or identifiers)


def _is_excluded_device(
    hass: HomeAssistant, device: dr.DeviceEntry, target_entry_id: str
) -> bool:
    """Return if a device should be excluded from gateway binding."""
    if _is_virtual_device(device):
        return True
    for config_entry_id in device.config_entries:
        domain = _async_entry_domain(hass, config_entry_id)
        if domain == "tuya":
            return True
        if domain == DOMAIN and config_entry_id != target_entry_id:
            return True
    return False


async def async_sync_gateway_devices(
    hass: HomeAssistant, entry: TuyaHaNewConfigEntry
) -> GatewayBindingsData:
    """Discover, filter, and persist eligible devices."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    previous_bindings = await async_load_gateway_bindings(hass, entry.entry_id) or {}
    deleted_device_ids = [
        device_id
        for device_id in previous_bindings.get("deleted_device_ids", [])
        if isinstance(device_id, str) and device_id
    ]
    deleted_device_id_set = set(deleted_device_ids)
    devices: dict[str, dict[str, str | list[str]]] = {}
    entities: dict[str, dict[str, str | None | list[str]]] = {}
    scanned_count = 0
    excluded_count = 0

    for deleted_device_id in deleted_device_ids:
        if deleted_device := device_registry.async_get(deleted_device_id):
            device_registry.async_update_device(
                deleted_device.id,
                remove_config_entry_id=entry.entry_id,
            )

    for device in device_registry.devices.values():
        scanned_count += 1
        if _is_excluded_device(hass, device, entry.entry_id):
            excluded_count += 1
            continue
        if device.id in deleted_device_id_set:
            continue

        # Only update devices that already have a local binding.  New devices
        # must go through the cloud confirmation flow (topo/add/notify →
        # sub/bind → bind_response) before being persisted locally.
        if device.id not in previous_bindings.get("devices", {}):
            continue

        # Read stored binding info from previous_bindings (confirmed via bind flow).
        # registry_sync runs before route table construction, so we use persisted data.
        prev_device_data = previous_bindings.get("devices", {}).get(device.id, {})
        stored_domain = prev_device_data.get("domain")
        stored_product_id = prev_device_data.get("product_id")
        stored_category_code = prev_device_data.get("category_code", "")

        if not stored_domain or not stored_product_id:
            LOGGER.debug(
                "registry_sync: device %s (%s) no stored binding info, skipping",
                device.name_by_user or device.name or device.id, device.id,
            )
            continue

        # Collect entities matching the stored domain.
        bound_entities: list[BoundEntity] = []
        for entity_entry in er.async_entries_for_device(entity_registry, device.id):
            if entity_entry.disabled_by is not None:
                continue
            if _is_excluded_domain(_async_entry_domain(hass, entity_entry.config_entry_id)):
                continue
            if entity_entry.domain != stored_domain:
                continue

            bound_entities.append(
                BoundEntity(
                    entity_id=entity_entry.entity_id,
                    device_id=device.id,
                    domain=entity_entry.domain,
                    category_code=stored_category_code,
                )
            )

        if not bound_entities:
            continue

        selected_domain = stored_domain
        product_id = stored_product_id
        entity_category_code = stored_category_code
        selected_entities = bound_entities
        primary_entity = selected_entities[0]

        devices[device.id] = {
            "name": device.name_by_user or device.name or device.id,
            "entity_ids": [bound_entity.entity_id for bound_entity in selected_entities],
            "primary_entity_id": primary_entity.entity_id,
            "domain": selected_domain,
            "category_code": entity_category_code,
            "product_id": product_id,
            "client_id": device.id,
            "tuya_device_id": previous_bindings.get("devices", {})
            .get(device.id, {})
            .get("tuya_device_id"),
        }
        # Associate the device with this config entry so it appears on the
        # integration page.
        if entry.entry_id not in device.config_entries:
            device_registry.async_update_device(
                device.id,
                add_config_entry_id=entry.entry_id,
            )
        for bound_entity in selected_entities:
            entities[bound_entity.entity_id] = {
                "device_id": bound_entity.device_id,
                "domain": bound_entity.domain,
                "category_code": bound_entity.category_code,
            }

    stale_device_ids = set(previous_bindings.get("devices", {})) - set(devices)
    stale_tuya_device_ids = [
        tuya_device_id
        for stale_device_id in stale_device_ids
        if isinstance(
            tuya_device_id := previous_bindings.get("devices", {})
            .get(stale_device_id, {})
            .get("tuya_device_id"),
            str,
        )
        and tuya_device_id
    ]
    runtime_data = getattr(entry, "runtime_data", None)
    if (
        stale_tuya_device_ids
        and runtime_data is not None
        and hasattr(runtime_data, "publish_subdevice_delete")
    ):
        runtime_data.publish_subdevice_delete(stale_tuya_device_ids)
    if (
        stale_tuya_device_ids
        and runtime_data is not None
        and hasattr(runtime_data, "unsubscribe_child_device_messages")
    ):
        for stale_tuya_device_id in stale_tuya_device_ids:
            runtime_data.unsubscribe_child_device_messages(stale_tuya_device_id)
    for stale_device_id in stale_device_ids:
        if stale_device := device_registry.async_get(stale_device_id):
            device_registry.async_update_device(
                stale_device.id,
                remove_config_entry_id=entry.entry_id,
            )

    bindings: GatewayBindingsData = {
        "devices": devices,
        "entities": entities,
        "deleted_device_ids": deleted_device_ids,
    }

    await async_save_gateway_bindings(hass, entry.entry_id, bindings)
    return bindings
