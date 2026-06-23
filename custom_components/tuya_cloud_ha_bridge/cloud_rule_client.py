"""Cloud rule client — fetches rules and reports low-confidence devices.

API endpoints (Tuya HA Fusion OpenAPI — see HA接口文档):
- GET  /v1.0/end-user/services/ha/rules/version  → {ruleVersion, updatedAt}
- GET  /v1.0/end-user/services/ha/rules/bundle   → {ruleVersion, pidSpecs, featureRules, componentRules}
- POST /v1.0/end-user/services/ha/devices/report-low-confidence → low-confidence report (interface 8)

The two rule endpoints return the Fusion-style camelCase envelope; this client
normalizes them back to the internal snake_case shape
(``rule_version/pidspecs/feature_rules/component_rules``) that the on-disk
bundle, ``rule_cache`` and ``bundle`` parser all expect. Inner pidSpec / rule
contents are treated as opaque and passed through untouched.

Public API:
- TuyaCloudRuleClient(hass, api_key) — implements orchestrator.CloudRuleClient protocol
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import LOGGER
from .region_mapping import get_region_endpoints_for_api_key

_RULES_VERSION_PATH = "/v1.0/end-user/services/ha/rules/version"
_RULES_BUNDLE_PATH = "/v1.0/end-user/services/ha/rules/bundle"
_REPORT_LOW_CONFIDENCE_PATH = "/v1.0/end-user/services/ha/devices/report-low-confidence"

# Valid trigger values per interface 8 (HaReportLowConfidenceRequest.trigger).
_VALID_TRIGGERS = frozenset(
    {"plugin_start", "device_added", "device_removed", "scheduled"}
)

# Fallback gateway sub-device cap used when the cloud limit can't be fetched
# (interface 6 currently returns a fixed 30).
_DEFAULT_SUB_DEVICE_LIMIT = 30
# Headroom added on top of the sub-device limit when capping the low-confidence
# report. Reported devices are *candidates* for binding; once the cloud repairs
# rules only up to `sub_device_limit` of them can actually bind, so we report a
# few extra (margin) to leave slack for ones that still fail to bind — but cap
# the total for performance instead of uploading an unbounded list.
_REPORT_OVER_LIMIT_MARGIN = 10

_REQUEST_TIMEOUT = 15


def _pick(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first present key from *keys* in *d* (camel/snake tolerant)."""
    for k in keys:
        if k in d:
            return d[k]
    return default


def _normalize_bundle_envelope(result: dict[str, Any]) -> dict[str, Any]:
    """Map the Fusion camelCase rules-bundle envelope to internal snake_case.

    Only the four top-level wrapper keys are translated; the pidSpec / feature
    / component contents are passed through verbatim (they are authored in
    snake_case and parsed as-is downstream). Accepts either casing for
    robustness against future server changes.
    """
    return {
        "rule_version": int(_pick(result, "ruleVersion", "rule_version", default=0) or 0),
        "pidspecs": _pick(result, "pidSpecs", "pidspecs", default=[]) or [],
        "feature_rules": _pick(result, "featureRules", "feature_rules", default={}) or {},
        "component_rules": _pick(result, "componentRules", "component_rules", default=[]) or [],
    }


class TuyaCloudRuleClient:
    """Cloud client for rule fetching and low-confidence reporting."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_key: str,
        gateway_id: str | None = None,
        sub_device_limit: int | None = None,
    ) -> None:
        self._hass = hass
        self._api_key = api_key
        self._gateway_id = gateway_id
        # Gateway sub-device cap (interface 6). Falls back to the documented
        # default when not provided / fetch failed.
        self._sub_device_limit = (
            sub_device_limit
            if isinstance(sub_device_limit, int) and sub_device_limit > 0
            else _DEFAULT_SUB_DEVICE_LIMIT
        )
        endpoints = get_region_endpoints_for_api_key(api_key)
        self._base_url = f"https://{endpoints.api_gateway_host}"

    @property
    def max_report_devices(self) -> int:
        """Max low-confidence devices to report in one batch (limit + margin)."""
        return self._sub_device_limit + _REPORT_OVER_LIMIT_MARGIN

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def async_get_rule_version(self) -> int:
        """Fetch the current rule version from cloud.

        GET /v1.0/end-user/services/ha/rules/version
        Response: {"success": true, "result": {"ruleVersion": 42, "updatedAt": 0}}
        """
        url = f"{self._base_url}{_RULES_VERSION_PATH}"
        session = async_get_clientsession(self._hass)

        resp = await session.get(url, headers=self._headers(), timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        body: dict[str, Any] = await resp.json()

        if not body.get("success"):
            LOGGER.warning("cloud_rules: version check failed: %s", body)
            return 0

        result = body.get("result") or {}
        version = int(_pick(result, "ruleVersion", "rule_version", default=0) or 0)
        LOGGER.debug(
            "cloud_rules: version=%d updatedAt=%s",
            version,
            _pick(result, "updatedAt", "updated_at"),
        )
        return version

    async def async_fetch_rules_bundle(self) -> dict[str, Any]:
        """Fetch the full rules bundle from cloud.

        GET /v1.0/end-user/services/ha/rules/bundle
        Response: {"success": true, "result": {ruleVersion, pidSpecs, featureRules, componentRules}}

        The Fusion camelCase envelope is normalized to the internal snake_case
        bundle shape before returning so downstream (rule_cache / bundle parser /
        on-disk persistence) is unaffected.
        """
        url = f"{self._base_url}{_RULES_BUNDLE_PATH}"
        session = async_get_clientsession(self._hass)

        resp = await session.get(url, headers=self._headers(), timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        body: dict[str, Any] = await resp.json()

        if not body.get("success"):
            error_code = body.get("errorCode", "UNKNOWN")
            error_msg = body.get("errorMsg", "Unknown error")
            raise RuntimeError(f"cloud_rules: bundle fetch failed: {error_code} - {error_msg}")

        result = body.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("cloud_rules: invalid bundle response (missing result)")

        bundle = _normalize_bundle_envelope(result)
        LOGGER.debug(
            "cloud_rules: fetched bundle version=%s pidspecs=%d",
            bundle["rule_version"],
            len(bundle["pidspecs"]),
        )
        return bundle

    async def async_report_low_confidence(
        self,
        devices: list[dict[str, Any]],
        *,
        local_rule_version: int,
        trigger: str,
    ) -> None:
        """Report low-confidence devices to cloud for LLM repair (interface 8).

        POST /v1.0/end-user/services/ha/devices/report-low-confidence

        The Fusion endpoint takes a single ``requestBody`` string parameter
        whose value is the JSON-encoded ``HaReportLowConfidenceRequest``:

            {
              "request_id": "<uuid>",        // idempotency key
              "gateway_id": "<gw>",
              "local_rule_version": <int>,
              "trigger": "plugin_start|device_added|device_removed|scheduled",
              "devices": [ DeviceItem, ... ]
            }

        ``request_id`` is generated per call so retries of the *same* logical
        report can be deduplicated cloud-side (the caller may reuse a client but
        each invocation is a fresh report). The response is an ACK
        (``FusionResult<Boolean>``); we do not block on analysis results.
        """
        if not devices:
            return

        # Cap the batch to sub_device_limit + margin. Reported devices are
        # binding candidates; only `sub_device_limit` can ultimately bind, so a
        # small margin is kept while the total is bounded for performance.
        cap = self.max_report_devices
        if len(devices) > cap:
            LOGGER.info(
                "cloud_rules: capping low-confidence report from %d to %d "
                "(sub_device_limit=%d + margin=%d)",
                len(devices),
                cap,
                self._sub_device_limit,
                _REPORT_OVER_LIMIT_MARGIN,
            )
            devices = devices[:cap]

        if trigger not in _VALID_TRIGGERS:
            LOGGER.warning(
                "cloud_rules: invalid trigger %r, falling back to 'scheduled'",
                trigger,
            )
            trigger = "scheduled"

        url = f"{self._base_url}{_REPORT_LOW_CONFIDENCE_PATH}"
        session = async_get_clientsession(self._hass)

        request: dict[str, Any] = {
            "request_id": uuid.uuid4().hex,
            "gateway_id": self._gateway_id or "",
            "local_rule_version": int(local_rule_version),
            "trigger": trigger,
            "devices": devices,
        }
        # Fusion wraps the report as a single JSON-string parameter.
        payload = {"requestBody": json.dumps(request, ensure_ascii=False, default=str)}

        LOGGER.debug(
            "cloud_rules: reporting low-confidence gateway_id: %s\n%s",
            self._gateway_id,
            json.dumps(request, indent=2, ensure_ascii=False, default=str),
        )

        resp = await session.post(
            url, headers=self._headers(), json=payload, timeout=_REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        body: dict[str, Any] = await resp.json()

        if not body.get("success"):
            LOGGER.warning(
                "cloud_rules: low-confidence report failed: errorCode=%s errorMsg=%s",
                body.get("errorCode"),
                body.get("errorMsg"),
            )
