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
    DOMAIN,
    LOGGER,
    LOCAL_CREDENTIALS_FILE,
)

_CREDENTIALS_STORAGE_VERSION = 1
_CREDENTIALS_STORAGE_KEY = f"{LOCAL_CREDENTIALS_FILE}.credentials"

# Gateway bindings are intentionally NOT persisted to disk. The Tuya cloud
# topo/get is the single source of truth: on every HA startup the in-memory
# map below starts empty and is rebuilt from the cloud during topo
# reconciliation (filter + inference → unbind non-conforming / bind
# conforming). This keeps cloud and local state consistent and removes the
# stale-cache class of bugs. Keyed by config-entry id inside hass.data.
_BINDINGS_RUNTIME_KEY = f"{DOMAIN}_gateway_bindings"


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


def _get_bindings_memory(hass: HomeAssistant) -> dict[str, GatewayBindingsData]:
    """Return the in-memory bindings map (keyed by config-entry id).

    Bindings live only for the lifetime of the HA process — they are rebuilt
    from the Tuya cloud (topo/get) on startup, so there is no disk persistence
    and nothing to migrate across a remove/re-add.
    """
    return hass.data.setdefault(_BINDINGS_RUNTIME_KEY, {})


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
    """Return the in-memory gateway binding data for an entry (None if absent).

    Kept ``async`` for call-site compatibility; the cloud topo/get
    reconciliation is the single source of truth that populates this map.
    """
    return _get_bindings_memory(hass).get(entry_id)


async def async_save_gateway_bindings(
    hass: HomeAssistant, entry_id: str, data: GatewayBindingsData
) -> None:
    """Store gateway binding data in memory (no disk persistence)."""
    _get_bindings_memory(hass)[entry_id] = data


async def async_delete_gateway_bindings(hass: HomeAssistant, entry_id: str) -> None:
    """Drop the in-memory gateway binding data for one entry."""
    _get_bindings_memory(hass).pop(entry_id, None)
