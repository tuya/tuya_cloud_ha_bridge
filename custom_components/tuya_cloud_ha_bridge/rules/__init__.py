"""Rule bundle loader for the PidSpec inference engine.

Loads rules from a local JSON file (rules.json). When the file is missing,
``load_rules_bundle()`` returns an empty baseline bundle (rule_version=0); the
caller (``async_init_pidspec``) detects the empty cache and bootstraps the full
bundle from the cloud via GET /v1.0/end-user/services/ha/rules/bundle, then
persists it back to this file.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ..const import LOGGER

_RULES_FILE = Path(__file__).parent / "rules.json"


def load_rules_bundle() -> dict[str, Any]:
    """Load the rules bundle from the local JSON file (synchronous, blocking).

    Returns the parsed dict in the same wire format as the cloud API response:
    {
        "rule_version": int,
        "pidspecs": [...],
        "feature_rules": {...},
        "component_rules": [...]
    }

    NOTE: This is a blocking I/O call. Callers running inside the Home Assistant
    event loop must use `await async_load_rules_bundle()` instead. The sync
    variant is kept for backwards compatibility with non-async code paths
    (CLI tools, tests, startup hooks that run off-loop).

    When the file is absent the caller bootstraps it from the cloud, so a
    missing file is an expected (recoverable) condition logged at INFO rather
    than ERROR. A present-but-corrupt file is still logged at ERROR.
    """
    try:
        raw = _RULES_FILE.read_text(encoding="utf-8")
        bundle = json.loads(raw)
        LOGGER.debug(
            "rules: loaded local bundle rule_version=%s (%d pidspecs)",
            bundle.get("rule_version"),
            len(bundle.get("pidspecs", [])),
        )
        return bundle
    except FileNotFoundError:
        LOGGER.info(
            "rules: local rules file %s not found — will bootstrap from cloud",
            _RULES_FILE.name,
        )
        return _empty_bundle()
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.error("rules: failed to load local rules file: %s", exc)
        return _empty_bundle()


def _empty_bundle() -> dict[str, Any]:
    """Return a baseline empty bundle (rule_version=0, no pidspecs)."""
    return {
            "rule_version": 0,
            "pidspecs": [],
            "feature_rules": {
                "attribute_presence": {},
                "device_class_features": {},
                "domain_features": {},
                "domain_attr_features": {},
            },
            "component_rules": [],
        }


async def async_load_rules_bundle() -> dict[str, Any]:
    """Async wrapper around :func:`load_rules_bundle` that offloads the file
    read to a thread, keeping the Home Assistant event loop unblocked.
    """
    return await asyncio.to_thread(load_rules_bundle)


def get_rule_version() -> int:
    """Return the current local rule version without loading the full bundle.

    NOTE: Blocking I/O. Use :func:`async_get_rule_version` from event-loop code.
    """
    try:
        raw = _RULES_FILE.read_text(encoding="utf-8")
        bundle = json.loads(raw)
        return int(bundle.get("rule_version", 0))
    except (OSError, json.JSONDecodeError, ValueError):
        return 0


async def async_get_rule_version() -> int:
    """Async wrapper around :func:`get_rule_version`."""
    return await asyncio.to_thread(get_rule_version)


def save_rules_bundle(bundle: dict[str, Any]) -> bool:
    """Persist a rules bundle to the local JSON file (synchronous, blocking).

    Called after a successful cloud fetch to keep the local file
    up-to-date across restarts.

    Returns True on success, False on error.

    NOTE: Blocking I/O. Use :func:`async_save_rules_bundle` from event-loop code.
    """
    try:
        _RULES_FILE.write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        LOGGER.info(
            "rules: saved bundle to disk rule_version=%s (%d pidspecs)",
            bundle.get("rule_version"),
            len(bundle.get("pidspecs", [])),
        )
        return True
    except OSError as exc:
        LOGGER.error("rules: failed to save rules file: %s", exc)
        return False


async def async_save_rules_bundle(bundle: dict[str, Any]) -> bool:
    """Async wrapper around :func:`save_rules_bundle`."""
    return await asyncio.to_thread(save_rules_bundle, bundle)
