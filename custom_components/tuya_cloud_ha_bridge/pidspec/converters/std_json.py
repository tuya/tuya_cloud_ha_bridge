"""std:json_struct converter.

Handles JSON-structured DP values (e.g. colour_data HSV) with per-field scaling.

converter_config options:
- schema: dict of field definitions, each with:
    - ha_attr: HA attribute path (e.g. "hs_color[0]", "brightness")
    - type: "int" or "float"
    - tuya_min / tuya_max: Tuya value range
    - ha_min / ha_max: HA value range
- service: HA service to call (e.g. "turn_on")
- serialize: if true, to_tuya returns JSON string (default: true)
- default: default Tuya value (JSON dict) when HA attrs missing
"""

from __future__ import annotations

import json
from typing import Any


def _parse_attr_path(path: str) -> tuple[str, int | None]:
    """Parse "attr[index]" into (attr_name, index_or_None)."""
    if "[" in path and path.endswith("]"):
        name, idx_str = path[:-1].split("[", 1)
        return name, int(idx_str)
    return path, None


def _get_ha_value(ha_attributes: dict[str, Any], path: str) -> Any:
    """Read a value from ha_attributes using an attr path like "hs_color[0]"."""
    name, idx = _parse_attr_path(path)
    value = ha_attributes.get(name)
    if value is None:
        return None
    if idx is not None:
        if isinstance(value, (list, tuple)) and len(value) > idx:
            return value[idx]
        return None
    return value


def _scale_to_ha(tuya_val: float, field: dict[str, Any]) -> float:
    """Scale a single field from Tuya range to HA range."""
    t_min = field.get("tuya_min", 0)
    t_max = field.get("tuya_max", 1000)
    h_min = field.get("ha_min", 0)
    h_max = field.get("ha_max", 255)

    t_range = t_max - t_min
    h_range = h_max - h_min
    if t_range == 0:
        return float(h_min)
    return (tuya_val - t_min) / t_range * h_range + h_min


def _scale_to_tuya(ha_val: float, field: dict[str, Any]) -> float:
    """Scale a single field from HA range to Tuya range."""
    t_min = field.get("tuya_min", 0)
    t_max = field.get("tuya_max", 1000)
    h_min = field.get("ha_min", 0)
    h_max = field.get("ha_max", 255)

    h_range = h_max - h_min
    t_range = t_max - t_min
    if h_range == 0:
        return float(t_min)
    return (ha_val - h_min) / h_range * t_range + t_min


class StdJsonStruct:

    def to_ha_service(
        self,
        tuya_value: Any,
        config: dict[str, Any],
        entity_id: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        schema = config.get("schema")
        if not schema:
            return None

        if isinstance(tuya_value, str):
            try:
                tuya_value = json.loads(tuya_value)
            except (json.JSONDecodeError, TypeError):
                return None

        if not isinstance(tuya_value, dict):
            return None

        domain = entity_id.split(".")[0] if "." in entity_id else "light"
        service = config.get("service", "turn_on")
        service_data: dict[str, Any] = {"entity_id": entity_id}

        # Group fields by ha_attr base name to reconstruct compound attrs (e.g. hs_color)
        compound: dict[str, list[tuple[int | None, float]]] = {}

        for field_name, field_def in schema.items():
            raw = tuya_value.get(field_name)
            if raw is None:
                continue

            ha_attr_path = field_def.get("ha_attr", field_name)
            ha_val = _scale_to_ha(float(raw), field_def)

            name, idx = _parse_attr_path(ha_attr_path)
            if idx is not None:
                compound.setdefault(name, []).append((idx, ha_val))
            else:
                field_type = field_def.get("type", "int")
                service_data[name] = int(round(ha_val)) if field_type == "int" else round(ha_val, 1)

        for name, parts in compound.items():
            parts.sort(key=lambda t: t[0] if t[0] is not None else 0)
            values = []
            for idx, val in parts:
                field_type = "float"
                for _fn, fd in schema.items():
                    attr_name, attr_idx = _parse_attr_path(fd.get("ha_attr", ""))
                    if attr_name == name and attr_idx == idx:
                        field_type = fd.get("type", "int")
                        break
                values.append(round(val, 1) if field_type == "float" else int(round(val)))
            service_data[name] = values

        return {"domain": domain, "service": service, "service_data": service_data}

    def to_tuya_value(
        self,
        ha_state: dict[str, Any],
        ha_attributes: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Any:
        schema = config.get("schema")
        if not schema:
            return config.get("default")

        result: dict[str, int] = {}
        has_any = False

        for field_name, field_def in schema.items():
            ha_attr_path = field_def.get("ha_attr", field_name)
            ha_val = _get_ha_value(ha_attributes, ha_attr_path)
            if ha_val is None:
                result[field_name] = int(field_def.get("tuya_min", 0))
                continue

            has_any = True
            tuya_val = _scale_to_tuya(float(ha_val), field_def)
            result[field_name] = int(round(tuya_val))

        if not has_any:
            default = config.get("default")
            if default is not None:
                return json.dumps(default, separators=(",", ":")) if config.get("serialize", True) else default
            return None

        if config.get("serialize", True):
            return json.dumps(result, separators=(",", ":"))
        return result
