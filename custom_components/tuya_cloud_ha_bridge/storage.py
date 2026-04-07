"""Local file helpers for Tuya HA New virtual gateways."""

from __future__ import annotations

import os
from typing import TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_DEVICE_SECRET,
    CONF_PRODUCT_ID,
    CONF_QR_CODE_DATA,
    LOGGER,
    LOCAL_CREDENTIALS_FILE,
)

_BINDINGS_STORAGE_VERSION = 1
_BINDINGS_STORAGE_KEY = f"{LOCAL_CREDENTIALS_FILE}.bindings"
_CREDENTIALS_STORAGE_VERSION = 1
_CREDENTIALS_STORAGE_KEY = f"{LOCAL_CREDENTIALS_FILE}.credentials"


class GatewayCredentials(TypedDict):
    """Stored credentials for a virtual gateway."""

    product_id: str
    device_id: str
    device_secret: str


class GatewayStorageData(GatewayCredentials, total=False):
    """Persisted virtual gateway storage structure."""

    device_name: str
    qr_code_data: str


class GatewayBoundDeviceData(TypedDict):
    """Stored device binding data."""

    name: str
    entity_ids: list[str]
    primary_entity_id: str
    domain: str
    product_id: str
    client_id: str
    tuya_device_id: str | None


class GatewayBoundEntityData(TypedDict):
    """Stored entity binding data."""

    device_id: str | None
    domain: str


class GatewayBindingsData(TypedDict, total=False):
    """Persisted gateway bindings data."""

    devices: dict[str, GatewayBoundDeviceData]
    entities: dict[str, GatewayBoundEntityData]
    deleted_device_ids: list[str]


def _get_credentials_store(
    hass: HomeAssistant,
) -> Store[dict[str, object]]:
    """Return the storage helper used for gateway credentials."""
    return Store[dict[str, object]](
        hass,
        _CREDENTIALS_STORAGE_VERSION,
        _CREDENTIALS_STORAGE_KEY,
        private=True,
    )


def _get_legacy_credentials_path(hass: HomeAssistant) -> str:
    """Return the path of the legacy plaintext credential file."""
    return hass.config.path(LOCAL_CREDENTIALS_FILE)


def _get_bindings_store(hass: HomeAssistant) -> Store[dict[str, GatewayBindingsData]]:
    """Return the storage helper used for gateway bindings."""
    return Store[dict[str, GatewayBindingsData]](
        hass,
        _BINDINGS_STORAGE_VERSION,
        _BINDINGS_STORAGE_KEY,
        private=True,
    )


def _normalize_credentials(data: dict[str, object] | None) -> GatewayStorageData | None:
    """Validate and normalize stored credentials."""
    if not isinstance(data, dict):
        return None

    product_id = data.get(CONF_PRODUCT_ID)
    device_id = data.get(CONF_DEVICE_ID)
    device_secret = data.get(CONF_DEVICE_SECRET)

    if not all(
        isinstance(value, str) and value
        for value in (product_id, device_id, device_secret)
    ):
        return None

    device_name = data.get(CONF_DEVICE_NAME)
    qr_code_data = data.get(CONF_QR_CODE_DATA)

    return {
        CONF_PRODUCT_ID: product_id,
        CONF_DEVICE_ID: device_id,
        CONF_DEVICE_SECRET: device_secret,
        CONF_DEVICE_NAME: device_name if isinstance(device_name, str) else "",
        CONF_QR_CODE_DATA: qr_code_data if isinstance(qr_code_data, str) else "",
    }


async def async_load_gateway_credentials(hass: HomeAssistant) -> GatewayStorageData | None:
    """Load virtual gateway credentials from secure store.

    Falls back to the legacy plaintext YAML file and migrates if found.
    """
    store = _get_credentials_store(hass)
    data = await store.async_load()
    if data:
        return _normalize_credentials(data)

    # Migrate from legacy plaintext YAML file if it exists.
    legacy_path = _get_legacy_credentials_path(hass)

    def _load_legacy(path: str) -> dict[str, object]:
        try:
            from homeassistant.util.yaml import load_yaml_dict

            return load_yaml_dict(path)
        except FileNotFoundError:
            return {}
        except Exception as err:
            LOGGER.warning("Failed to load legacy gateway credentials: %s", err)
            return {}

    legacy_data = await hass.async_add_executor_job(_load_legacy, legacy_path)
    creds = _normalize_credentials(legacy_data)
    if creds:
        await store.async_save(dict(creds))

        def _remove_legacy(path: str) -> None:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

        await hass.async_add_executor_job(_remove_legacy, legacy_path)

    return creds


async def async_save_gateway_credentials(
    hass: HomeAssistant, credentials: GatewayStorageData
) -> None:
    """Persist virtual gateway credentials to secure store."""
    data: dict[str, object] = {
        CONF_PRODUCT_ID: credentials[CONF_PRODUCT_ID],
        CONF_DEVICE_ID: credentials[CONF_DEVICE_ID],
        CONF_DEVICE_SECRET: credentials[CONF_DEVICE_SECRET],
        CONF_DEVICE_NAME: credentials.get(CONF_DEVICE_NAME, ""),
        CONF_QR_CODE_DATA: credentials.get(CONF_QR_CODE_DATA, ""),
    }
    await _get_credentials_store(hass).async_save(data)


async def async_delete_gateway_credentials(hass: HomeAssistant) -> None:
    """Delete gateway credentials from secure store and legacy file."""
    await _get_credentials_store(hass).async_remove()

    def _delete_legacy(path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            return

    await hass.async_add_executor_job(
        _delete_legacy, _get_legacy_credentials_path(hass)
    )


async def async_load_gateway_bindings(
    hass: HomeAssistant, entry_id: str
) -> GatewayBindingsData | None:
    """Load persisted gateway binding data."""
    store_data = await _get_bindings_store(hass).async_load()
    if store_data is None:
        return None
    return store_data.get(entry_id)


async def async_save_gateway_bindings(
    hass: HomeAssistant, entry_id: str, data: GatewayBindingsData
) -> None:
    """Persist gateway binding data."""
    store = _get_bindings_store(hass)
    store_data = await store.async_load() or {}
    store_data[entry_id] = data
    await store.async_save(store_data)


async def async_delete_gateway_bindings(hass: HomeAssistant, entry_id: str) -> None:
    """Delete persisted gateway binding data for one entry."""
    store = _get_bindings_store(hass)
    store_data = await store.async_load() or {}
    if entry_id not in store_data:
        return

    del store_data[entry_id]
    await store.async_save(store_data)


async def async_migrate_gateway_bindings(
    hass: HomeAssistant, new_entry_id: str
) -> GatewayBindingsData | None:
    """Migrate bindings from a previous (orphaned) entry to the new entry.

    When the integration is removed and re-added the entry_id changes.
    This function finds any orphaned bindings (keyed by the old entry_id),
    re-keys them under *new_entry_id*, and removes the old key.

    Returns the migrated bindings or ``None`` if nothing was migrated.
    """
    store = _get_bindings_store(hass)
    store_data = await store.async_load()
    if not store_data:
        return None

    # If the new entry already has bindings, nothing to migrate.
    if new_entry_id in store_data:
        return None

    # Find orphaned bindings — any key that is NOT the new entry.
    orphaned_entries = [
        (key, data) for key, data in store_data.items() if key != new_entry_id
    ]
    if not orphaned_entries:
        return None

    # Take the first (and typically only) orphaned entry.
    old_entry_id, old_bindings = orphaned_entries[0]
    store_data[new_entry_id] = old_bindings
    del store_data[old_entry_id]
    await store.async_save(store_data)
    return old_bindings
