"""Entity capability knowledge — which HA service requires which feature bit.

Single source of truth shared by:

- Bind-time trimming (method B): ``prune_value_map`` removes ``value_map``
  entries whose target service the bound entity does not support, so the
  dpProperties advertised to the Tuya cloud only carry commands the device can
  actually execute. This is the primary control point.
- Runtime gate (method A): ``feature_int_supports`` is a thin last-resort net in
  ``services.py`` for the window between a capability change (e.g. firmware
  update) and the next re-bind. Operationally that drift is handled by asking
  the customer to unbind / re-bind, so A rarely fires.

The map keys on HA's documented service names (``pause``, ``set_cover_position``,
…). Built lazily and per-domain-guarded: a HA version that renamed / removed an
EntityFeature enum simply drops that domain rather than breaking the whole map.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def service_feature_requirements() -> dict[str, dict[str, tuple[int, ...]]]:
    """Return ``domain → {service: (acceptable feature bits, ...)}``.

    A service passes the capability check when the entity's
    ``supported_features`` bitmask intersects ANY of the listed bits. Domains /
    services absent from this table are never gated (they pass through).
    """
    table: dict[str, dict[str, tuple[int, ...]]] = {}

    try:
        from homeassistant.components.vacuum import VacuumEntityFeature as V

        table["vacuum"] = {
            "start": (int(V.START),),
            "pause": (int(V.PAUSE),),
            "stop": (int(V.STOP),),
            "return_to_base": (int(V.RETURN_HOME),),
            "clean_spot": (int(V.CLEAN_SPOT),),
            "locate": (int(V.LOCATE),),
            "set_fan_speed": (int(V.FAN_SPEED),),
            "send_command": (int(V.SEND_COMMAND),),
        }
    except Exception:  # noqa: BLE001 - version-tolerant best effort
        pass

    try:
        from homeassistant.components.fan import FanEntityFeature as F

        table["fan"] = {
            "set_percentage": (int(F.SET_SPEED),),
            "set_preset_mode": (int(F.PRESET_MODE),),
            "oscillate": (int(F.OSCILLATE),),
            "set_direction": (int(F.DIRECTION),),
        }
    except Exception:  # noqa: BLE001
        pass

    try:
        from homeassistant.components.cover import CoverEntityFeature as C

        table["cover"] = {
            "open_cover": (int(C.OPEN),),
            "close_cover": (int(C.CLOSE),),
            "stop_cover": (int(C.STOP),),
            "set_cover_position": (int(C.SET_POSITION),),
            "open_cover_tilt": (int(C.OPEN_TILT),),
            "close_cover_tilt": (int(C.CLOSE_TILT),),
            "stop_cover_tilt": (int(C.STOP_TILT),),
            "set_cover_tilt_position": (int(C.SET_TILT_POSITION),),
        }
    except Exception:  # noqa: BLE001
        pass

    try:
        from homeassistant.components.climate import ClimateEntityFeature as Cl

        # set_temperature is satisfied by either single-target or range support.
        table["climate"] = {
            "set_temperature": (
                int(Cl.TARGET_TEMPERATURE),
                int(Cl.TARGET_TEMPERATURE_RANGE),
            ),
            "set_humidity": (int(Cl.TARGET_HUMIDITY),),
            "set_fan_mode": (int(Cl.FAN_MODE),),
            "set_preset_mode": (int(Cl.PRESET_MODE),),
            "set_swing_mode": (int(Cl.SWING_MODE),),
        }
    except Exception:  # noqa: BLE001
        pass

    try:
        from homeassistant.components.media_player import (
            MediaPlayerEntityFeature as M,
        )

        table["media_player"] = {
            "media_play": (int(M.PLAY),),
            "media_pause": (int(M.PAUSE),),
            "media_stop": (int(M.STOP),),
            "media_next_track": (int(M.NEXT_TRACK),),
            "media_previous_track": (int(M.PREVIOUS_TRACK),),
            "volume_set": (int(M.VOLUME_SET),),
            "volume_mute": (int(M.VOLUME_MUTE),),
        }
    except Exception:  # noqa: BLE001
        pass

    return table


def feature_int_supports(
    domain: str, service: str, supported_features_int: Any
) -> bool:
    """Return True if a ``supported_features`` bitmask satisfies *service*.

    Untracked domain/service, or a non-int bitmask, pass through as True. A real
    ``0`` bitmask means the (live) entity advertises no features, so a gated
    service is blocked — used by the runtime gate where the state is current.
    """
    required = service_feature_requirements().get(domain, {}).get(service)
    if required is None:
        return True
    if not isinstance(supported_features_int, int):
        return True
    return any(supported_features_int & bit for bit in required)


def prune_value_map(
    value_map: dict[str, Any], supported_features_int: Any
) -> dict[str, Any]:
    """Drop ``value_map`` entries whose target service the entity cannot run.

    *value_map* maps ``tuya_enum_value → "domain.service"`` (the
    ``std:value_to_service`` config). Each entry's service is checked against
    *supported_features_int*; unsupported ones are dropped so the binding only
    advertises executable commands to the Tuya cloud.

    Guard (avoid over-trim on an incomplete bind-time snapshot): when the
    bitmask is unknown (``None`` / non-int) or a falsy ``0`` — e.g. the carrier
    entity was momentarily unavailable — the map is returned UNCHANGED. The
    next re-bind re-evaluates against a fresh snapshot.

    Returns a NEW dict when anything is dropped; otherwise returns the input
    unchanged (callers must not assume a copy).
    """
    if not value_map:
        return value_map
    # Guard: unknown / zero capabilities → keep everything, defer to re-bind.
    if not isinstance(supported_features_int, int) or supported_features_int == 0:
        return value_map

    reqs = service_feature_requirements()
    pruned: dict[str, Any] = {}
    dropped = False
    for tuya_val, service_ref in value_map.items():
        parts = str(service_ref).split(".", 1)
        if len(parts) != 2:
            pruned[tuya_val] = service_ref
            continue
        domain, service = parts
        required = reqs.get(domain, {}).get(service)
        if required is None or any(supported_features_int & bit for bit in required):
            pruned[tuya_val] = service_ref
        else:
            dropped = True

    return pruned if dropped else value_map
