"""Standard DP converter registry.

Each converter handles bidirectional value transformation between
Tuya DP values and HA service calls / state attributes.

Public API:
- get_converter(name) → Converter instance (raises KeyError if unknown)
- convert_to_ha(name, tuya_value, config, entity_id, context) → service call dict
- convert_to_tuya(name, ha_state, ha_attributes, config, context) → DP value
"""

from __future__ import annotations

from typing import Any, Protocol


class Converter(Protocol):
    """Protocol for DP value converters."""

    def to_ha_service(
        self,
        tuya_value: Any,
        config: dict[str, Any],
        entity_id: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Convert a Tuya DP value into an HA service call dict.

        Returns a dict like:
            {"domain": "...", "service": "...", "service_data": {...}}
        or None if no action needed.
        """
        ...

    def to_tuya_value(
        self,
        ha_state: dict[str, Any],
        ha_attributes: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Any:
        """Convert HA state/attributes into a Tuya DP value.

        Returns the raw value to report (bool, int, str, dict, etc.)
        or None if nothing to report.
        """
        ...


from .std_bool import StdBoolSwitch
from .std_enum import StdEnumPassthrough
from .std_numeric import StdNumericScale
from .std_json import StdJsonStruct
from .std_value_to_service import StdValueToService
from .groups import (
    GROUP_CONVERTERS,
    GroupConverter,
    get_group_converter,
    is_group_converter,
)

_REGISTRY: dict[str, Converter] = {
    "std:bool_switch": StdBoolSwitch(),
    "std:enum_passthrough": StdEnumPassthrough(),
    "std:numeric_scale": StdNumericScale(),
    "std:json_struct": StdJsonStruct(),
    "std:value_to_service": StdValueToService(),
}


def get_converter(name: str) -> Converter:
    """Return the converter instance for *name*. Raises KeyError if unknown."""
    return _REGISTRY[name]


def convert_to_ha(
    name: str,
    tuya_value: Any,
    config: dict[str, Any],
    entity_id: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Shortcut: look up converter by name and call to_ha_service."""
    return _REGISTRY[name].to_ha_service(tuya_value, config, entity_id, context)


def convert_to_tuya(
    name: str,
    ha_state: dict[str, Any],
    ha_attributes: dict[str, Any],
    config: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> Any:
    """Shortcut: look up converter by name and call to_tuya_value."""
    return _REGISTRY[name].to_tuya_value(ha_state, ha_attributes, config, context)
