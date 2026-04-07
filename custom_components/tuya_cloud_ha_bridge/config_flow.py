"""Config flow for Tuya HA New Gateway."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_API_KEY
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    QrCodeSelector,
    QrCodeSelectorConfig,
    QrErrorCorrectionLevel,
)

from . import (
    async_store_pending_gateway_client,
)
from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_DEVICE_SECRET,
    CONF_PRODUCT_ID,
    CONF_QR_CODE_DATA,
    DOMAIN,
    LOGGER,
    TUYA_CLOUD_API_KEY_GUIDE_URL,
)
from .region_mapping import get_region_endpoints_for_api_key, is_api_key_region_supported
from .storage import async_load_gateway_credentials
from .tuya_link_mqtt import TuyaLinkMqttClient
from .tuya_openapi import TuyaOpenApiError, async_create_ha_gateway, async_get_sub_device_limit

DEFAULT_ENTRY_TITLE = "Tuya_New_HA"


def _default_gateway_details(
    data: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Return normalized gateway details for the flow state."""
    source = data or {}
    api_key_value = source.get(CONF_API_KEY, "")
    api_key = api_key_value.strip() if isinstance(api_key_value, str) else ""
    return {
        CONF_API_KEY: api_key,
    }


def _gateway_details_schema(defaults: dict[str, str]) -> vol.Schema:
    """Return the schema used to collect gateway details."""
    return vol.Schema(
        {
            vol.Required(CONF_API_KEY, default=defaults[CONF_API_KEY]): str,
        }
    )


def _bind_gateway_schema(qr_code_data: str) -> vol.Schema:
    """Return the schema used to show the binding QR code."""
    return vol.Schema({}).extend(
        {
            vol.Optional("qr_code"): QrCodeSelector(
                config=QrCodeSelectorConfig(
                    data=qr_code_data,
                    scale=6,
                    error_correction_level=QrErrorCorrectionLevel.QUARTILE,
                )
            )
        }
    )


def _gateway_title(gateway_data: dict[str, Any]) -> str:
    """Return the title used for the config entry."""
    if device_name := gateway_data.get(CONF_DEVICE_NAME):
        return str(device_name)
    if device_id := gateway_data.get(CONF_DEVICE_ID):
        return str(device_id)
    return DEFAULT_ENTRY_TITLE


def _gateway_placeholders(gateway_data: dict[str, Any]) -> dict[str, str]:
    """Return placeholders shared by flow steps."""
    return {
        "guide_url": TUYA_CLOUD_API_KEY_GUIDE_URL,
        CONF_DEVICE_ID: str(gateway_data.get(CONF_DEVICE_ID, "")),
        "device_name": str(gateway_data.get(CONF_DEVICE_NAME, "")),
        "sub_device_limit": str(gateway_data.get("sub_device_limit", "")),
    }


def _validate_gateway_details(
    user_input: dict[str, Any] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Validate gateway details submitted by the user."""
    gateway_details = _default_gateway_details(user_input)
    errors: dict[str, str] = {}

    if not gateway_details[CONF_API_KEY]:
        errors["base"] = "api_key_required"
    elif not is_api_key_region_supported(gateway_details[CONF_API_KEY]):
        errors["base"] = "unsupported_region"

    return gateway_details, errors


class _TemporaryGatewayClientMixin:
    """Manage the temporary MQTT session used during gateway binding."""

    hass: HomeAssistant
    _temporary_client: TuyaLinkMqttClient | None
    _temporary_client_handed_off: bool

    async def _async_disconnect_temporary_client(self) -> None:
        """Disconnect the temporary MQTT session if one exists."""
        if self._temporary_client is None:
            return

        client = self._temporary_client
        self._temporary_client = None
        await self.hass.async_add_executor_job(client.disconnect)

    async def _async_connect_temporary_client(
        self, gateway_data: dict[str, str]
    ) -> None:
        """Connect a temporary MQTT session and keep it online for binding."""
        await self._async_disconnect_temporary_client()

        client = TuyaLinkMqttClient(
            product_id=gateway_data[CONF_PRODUCT_ID],
            device_id=gateway_data[CONF_DEVICE_ID],
            device_secret=gateway_data[CONF_DEVICE_SECRET],
            broker=get_region_endpoints_for_api_key(
                gateway_data.get(CONF_API_KEY)
            ).mqtt_broker,
        )
        try:
            await self.hass.async_add_executor_job(client.connect)
        except Exception:
            await self.hass.async_add_executor_job(client.disconnect)
            raise

        self._temporary_client = client

    async def _async_prepare_gateway_for_binding(
        self, gateway_details: dict[str, str]
    ) -> tuple[dict[str, str] | None, str | None]:
        """Call OpenAPI to create gateway, then connect MQTT."""
        api_key = gateway_details[CONF_API_KEY]

        try:
            result = await async_create_ha_gateway(self.hass, api_key)
        except TuyaOpenApiError as err:
            LOGGER.warning("Tuya OpenAPI gateway creation failed: %s", err)
            return None, "cannot_create_gateway"
        except Exception:
            LOGGER.exception("Unexpected error calling Tuya OpenAPI")
            return None, "cannot_create_gateway"

        # Fetch sub-device limit (best-effort, do not block gateway creation).
        sub_device_limit = ""
        try:
            limit_count = await async_get_sub_device_limit(self.hass, api_key)
            sub_device_limit = str(limit_count)
        except Exception:
            LOGGER.debug("Failed to fetch sub-device limit, skipping")

        gateway_data: dict[str, str] = {
            CONF_API_KEY: api_key,
            CONF_PRODUCT_ID: result.product_id,
            CONF_DEVICE_ID: result.device_id,
            CONF_DEVICE_SECRET: result.device_secret,
            CONF_DEVICE_NAME: result.device_name,
            CONF_QR_CODE_DATA: result.short_url,
            "sub_device_limit": sub_device_limit,
        }

        if not gateway_data[CONF_QR_CODE_DATA]:
            LOGGER.warning("OpenAPI returned empty short_url for QR code")
            return None, "cannot_create_gateway"

        try:
            await self._async_connect_temporary_client(gateway_data)
        except Exception:
            LOGGER.exception("Failed to connect Tuya Link MQTT for binding")
            return None, "cannot_connect_mqtt"

        return gateway_data, None

    @callback
    def _async_schedule_temporary_client_disconnect(self) -> None:
        """Disconnect the temporary MQTT session when the flow ends."""
        if self._temporary_client is None:
            return

        client = self._temporary_client
        self._temporary_client = None

        if self._temporary_client_handed_off:
            self._temporary_client_handed_off = False
            return

        async def _async_disconnect() -> None:
            """Disconnect the temporary MQTT client outside the event loop."""
            await self.hass.async_add_executor_job(client.disconnect)

        self.hass.async_create_task(_async_disconnect())

    def _async_handoff_temporary_client(self, device_id: str) -> None:
        """Hand off the connected temporary client to config entry setup."""
        if self._temporary_client is None:
            return

        async_store_pending_gateway_client(
            hass=self.hass, device_id=device_id, client=self._temporary_client
        )
        self._temporary_client_handed_off = True

    @callback
    def async_remove(self) -> None:
        """Clean up the temporary MQTT session when the flow is removed."""
        self._async_schedule_temporary_client_disconnect()


class TuyaHaNewConfigFlow(_TemporaryGatewayClientMixin, ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tuya HA New Gateway."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._gateway_details = _default_gateway_details()
        self._gateway_data: dict[str, str] | None = None
        self._temporary_client = None
        self._temporary_client_handed_off = False

    async def _async_finish_gateway_setup(self) -> ConfigFlowResult:
        """Create the config entry and end the flow without a create-entry page."""
        gateway_data = self._gateway_data or {}
        entry = ConfigEntry(
            data=gateway_data,
            discovery_keys=MappingProxyType({}),
            domain=DOMAIN,
            minor_version=self.MINOR_VERSION,
            options={},
            source=self.context["source"],
            subentries_data=(),
            title=_gateway_title(gateway_data),
            unique_id=self.unique_id,
            version=self.VERSION,
        )
        await self.hass.config_entries.async_add(entry)
        return self.async_abort(
            reason="reconfigure_successful",
            description_placeholders={
                "device_name": str(gateway_data.get(CONF_DEVICE_NAME, "")),
                "device_id": str(gateway_data.get(CONF_DEVICE_ID, "")),
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect gateway details before creating the virtual gateway."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        errors: dict[str, str] = {}

        if user_input is not None:
            self._gateway_details, errors = _validate_gateway_details(user_input)
            if not errors:
                gateway_data, error = await self._async_prepare_gateway_for_binding(
                    self._gateway_details
                )
                if gateway_data is not None:
                    self._gateway_data = gateway_data
                    return await self.async_step_bind_gateway()

                errors["base"] = error or "cannot_create_gateway"

        return self.async_show_form(
            step_id="user",
            data_schema=_gateway_details_schema(self._gateway_details),
            description_placeholders=_gateway_placeholders(self._gateway_details),
            errors=errors,
        )

    async def async_step_bind_gateway(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the QR code used to bind the virtual gateway."""
        if self._gateway_data is None:
            return await self.async_step_user()

        if user_input is not None:
            self._async_handoff_temporary_client(self._gateway_data[CONF_DEVICE_ID])
            return await self._async_finish_gateway_setup()

        return self.async_show_form(
            step_id="bind_gateway",
            data_schema=_bind_gateway_schema(self._gateway_data[CONF_QR_CODE_DATA]),
            description_placeholders=_gateway_placeholders(self._gateway_data),
            last_step=True,
        )

    async def async_step_gateway_success(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the gateway creation success message."""
        if self._gateway_data is None:
            return await self.async_step_user()

        if user_input is not None:
            return await self._async_finish_gateway_setup()

        return self.async_show_form(
            step_id="gateway_success",
            data_schema=vol.Schema({}),
            description_placeholders=_gateway_placeholders(self._gateway_data),
            last_step=True,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> TuyaHaNewOptionsFlowHandler:
        """Return the options flow handler."""
        return TuyaHaNewOptionsFlowHandler()


class TuyaHaNewOptionsFlowHandler(_TemporaryGatewayClientMixin, OptionsFlowWithReload):
    """Handle Tuya HA New options for an existing gateway entry."""

    def __init__(self) -> None:
        """Initialize the options flow."""
        self._gateway_details = _default_gateway_details()
        self._gateway_data: dict[str, str] | None = None
        self._temporary_client = None
        self._temporary_client_handed_off = False

    async def async_step_init(
        self, _user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start the gateway management flow."""
        if not self.config_entry.data.get(CONF_DEVICE_ID) and (
            stored_credentials := await async_load_gateway_credentials(self.hass)
        ):
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data=stored_credentials,
                title=_gateway_title(stored_credentials),
            )
            return await self.async_step_manage_gateway()

        if self.config_entry.data.get(CONF_DEVICE_ID):
            return await self.async_step_manage_gateway()

        self._gateway_details = _default_gateway_details(self.config_entry.data)
        return await self.async_step_gateway_details()

    async def async_step_gateway_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect details for a placeholder entry that has no gateway yet."""
        errors: dict[str, str] = {}

        if self.config_entry.data.get(CONF_DEVICE_ID):
            return await self.async_step_manage_gateway()

        if user_input is not None:
            self._gateway_details, errors = _validate_gateway_details(user_input)
            if not errors:
                gateway_data, error = await self._async_prepare_gateway_for_binding(
                    self._gateway_details
                )
                if gateway_data is not None:
                    self._gateway_data = gateway_data
                    return await self.async_step_bind_gateway()

                errors["base"] = error or "cannot_create_gateway"

        return self.async_show_form(
            step_id="gateway_details",
            data_schema=_gateway_details_schema(self._gateway_details),
            description_placeholders=_gateway_placeholders(self._gateway_details),
            errors=errors,
        )

    async def async_step_bind_gateway(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the QR code used to bind the virtual gateway."""
        if self._gateway_data is None:
            return await self.async_step_gateway_details()

        if user_input is not None:
            self._async_handoff_temporary_client(self._gateway_data[CONF_DEVICE_ID])
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data=self._gateway_data,
                title=_gateway_title(self._gateway_data),
            )
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self.config_entry.entry_id)
            )
            return await self.async_step_gateway_success()

        return self.async_show_form(
            step_id="bind_gateway",
            data_schema=_bind_gateway_schema(self._gateway_data[CONF_QR_CODE_DATA]),
            description_placeholders=_gateway_placeholders(self._gateway_data),
            last_step=True,
        )

    async def async_step_gateway_success(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the gateway creation success message."""
        if user_input is not None:
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="gateway_success",
            data_schema=vol.Schema({}),
            description_placeholders=_gateway_placeholders(self._gateway_data or {}),
            last_step=True,
        )

    async def async_step_manage_gateway(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the current virtual gateway details."""
        if user_input is not None:
            return self.async_create_entry(title="", data={})

        gateway_data = {
            CONF_DEVICE_ID: self.config_entry.data.get(CONF_DEVICE_ID, ""),
            "guide_url": TUYA_CLOUD_API_KEY_GUIDE_URL,
            "device_name": self.config_entry.data.get(CONF_DEVICE_NAME, ""),
        }
        return self.async_show_form(
            step_id="manage_gateway",
            data_schema=vol.Schema({}),
            description_placeholders=gateway_data,
        )
