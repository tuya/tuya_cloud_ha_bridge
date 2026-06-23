"""Runtime dispatcher — routes Tuya DP payloads through converters.

Replaces the legacy mapping_runtime.build_service_calls_from_tuya and
build_tuya_properties_from_state with a converter-driven approach.

Public API:
- dispatch_tuya_to_ha(route_table, payload, context) → list[ServiceCall]
- dispatch_ha_to_tuya(route_table, entity_id, ha_state, ha_attributes, context) → dict[dpcode, value]
"""

from __future__ import annotations

import time
from typing import Any

from .models import DeviceRouteTable, DPRoute
from .converters import convert_to_ha, convert_to_tuya, is_group_converter, get_group_converter


def dispatch_tuya_to_ha(
    route_table: DeviceRouteTable,
    payload: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Convert a Tuya property/set payload into HA service calls.

    Dispatches each DP through its assigned converter. Group converters
    are collected first and processed as a batch.

    Args:
        route_table: The device's resolved route table.
        payload: Tuya DP payload dict (e.g. {"switch": true, "temp_set": 26}).
        context: Runtime context (temperature_unit, last_cover_control, etc.)

    Returns:
        List of HA service call dicts, each with:
        {"domain": str, "service": str, "service_data": dict}
    """
    service_calls: list[dict[str, Any]] = []
    groups: dict[str, dict[str, Any]] = {}

    for dpcode, value in payload.items():
        route = route_table.dpcode_to_route.get(dpcode)
        if route is None:
            continue
        if route.direction == "read_only":
            continue

        if is_group_converter(route.converter):
            gid = route.converter_config.get("group_id", route.converter)
            groups.setdefault(gid, {})[dpcode] = value
        else:
            call = convert_to_ha(
                route.converter, value, route.converter_config, route.entity_id, context
            )
            if call:
                service_calls.append(call)

            # Handle extra services embedded in converter_config
            # (e.g. fan with direction_service, oscillate_service)

    # Group-level dispatch
    for gid, group_payload in groups.items():
        converter_name = None
        group_routes: list[DPRoute] = []
        for r in route_table.routes:
            if is_group_converter(r.converter) and r.converter_config.get("group_id", r.converter) == gid:
                group_routes.append(r)
                if converter_name is None:
                    converter_name = r.converter

        if converter_name and group_routes:
            group_conv = get_group_converter(converter_name)
            calls = group_conv.tuya_to_ha(group_payload, group_routes, context)
            service_calls.extend(calls)

    return service_calls


def dispatch_ha_to_tuya(
    route_table: DeviceRouteTable,
    entity_id: str,
    ha_state: dict[str, Any],
    ha_attributes: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Convert HA entity state into Tuya property report payload.

    Iterates over all routes bound to *entity_id*, calls each converter's
    to_tuya_value, and assembles the full property report dict.

    Args:
        route_table: The device's resolved route table.
        entity_id: The HA entity that changed state.
        ha_state: Dict with at least {"state": str}.
        ha_attributes: Dict of entity attributes.
        context: Runtime context.

    Returns:
        Tuya property report dict: {dpcode: {"time": ms, "value": Any}}
    """
    routes = route_table.entity_to_routes.get(entity_id)
    if not routes:
        return {}

    # Separate group and solo routes
    group_routes_by_gid: dict[str, list[DPRoute]] = {}
    solo_routes: list[DPRoute] = []

    for r in routes:
        if r.direction == "write_only":
            continue
        if is_group_converter(r.converter):
            gid = r.converter_config.get("group_id", r.converter)
            group_routes_by_gid.setdefault(gid, []).append(r)
        else:
            solo_routes.append(r)

    now_ms = int(time.time() * 1000)
    properties: dict[str, dict[str, Any]] = {}

    # Solo routes
    for route in solo_routes:
        value = convert_to_tuya(
            route.converter, ha_state, ha_attributes, route.converter_config, context
        )
        if value is not None:
            properties[route.dpcode] = {"time": now_ms, "value": value}

    # Group routes
    for gid, g_routes in group_routes_by_gid.items():
        converter_name = g_routes[0].converter
        group_conv = get_group_converter(converter_name)
        dp_values = group_conv.ha_to_tuya(ha_state, ha_attributes, g_routes, context)
        for dpcode, value in dp_values.items():
            if value is not None:
                properties[dpcode] = {"time": now_ms, "value": value}

    return properties
