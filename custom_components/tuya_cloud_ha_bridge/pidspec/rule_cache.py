"""Local rule cache with version-gated bundle updates (routing doc §12.2).

Public API:
- LocalRuleCache: dataclass holding all parsed rules with atomic update support.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import ComponentRule, FeatureRules, PidSpec
from ..const import LOGGER


# ---------------------------------------------------------------------------
# Bundle parsing helpers
# ---------------------------------------------------------------------------


def _parse_feature_rules(raw: dict[str, Any]) -> FeatureRules:
    """Parse the feature_rules section of a bundle dict into a FeatureRules object."""
    return FeatureRules(
        attribute_presence=raw.get("attribute_presence", {}),
        device_class_features=raw.get("device_class_features", {}),
        domain_features=raw.get("domain_features", {}),
        domain_attr_features=raw.get("domain_attr_features", {}),
        unit_features=raw.get("unit_features", []),
        state_type_features=raw.get("state_type_features", []),
    )


def _parse_component_rules(raw_list: list[dict[str, Any]]) -> list[ComponentRule]:
    """Parse the component_rules list of a bundle dict."""
    rules: list[ComponentRule] = []
    for item in raw_list:
        keywords = item.get("keywords", [])
        component = item.get("component", "")
        if component:
            rules.append(ComponentRule(keywords=keywords, component=component))
    return rules


def _parse_candidate_targets(raw_list: list[dict[str, Any]]):
    """Parse candidate_targets inside a dp_definition."""
    from .models import CandidateTarget

    targets = []
    for item in raw_list:
        targets.append(
            CandidateTarget(
                domain=item.get("domain", ""),
                converter=item.get("converter", ""),
                features=item.get("features", []),
                converter_config=item.get("converter_config", {}),
                priority=int(item.get("priority", 0)),
                tuya_enum_values=item.get("tuya_enum_values"),
            )
        )
    return targets


def _parse_match_hints(raw: dict[str, Any] | None):
    """Parse match_hints dict into a MatchHints object, or None."""
    if raw is None:
        return None
    from .models import MatchHints

    return MatchHints(
        preferred_keywords=raw.get("preferred_keywords", []),
        excluded_keywords=raw.get("excluded_keywords", []),
    )


_RW_TO_DIRECTION: dict[str, str] = {
    "rw": "bidirectional",
    "ro": "read_only",
    "wo": "write_only",
}

_WIRE_TYPE_MAP: dict[str, str] = {
    "bool": "bool",
    "value": "value",
    "enum": "enum",
    "string": "string",
    "Boolean": "bool",
    "Integer": "value",
    "Enum": "enum",
    "String": "string",
}


def _parse_dp_definitions(
    raw_list: list[dict[str, Any]],
    candidate_targets_wire: dict[str, list[dict[str, Any]]] | None = None,
):
    """Parse dp_definitions list from wire format.

    Wire format uses "rw" (ro/rw/wo) and "type" (bool/value/enum/string).
    candidate_targets may be at pidspec level keyed by dpcode.
    """
    from .models import DPDefinition

    if candidate_targets_wire is None:
        candidate_targets_wire = {}

    defs = []
    for item in raw_list:
        dpcode = item.get("dpcode", "")
        raw_rw = item.get("rw", "rw")
        direction = item.get("direction") or _RW_TO_DIRECTION.get(raw_rw, "bidirectional")
        raw_type = item.get("type", "string")
        dp_type = _WIRE_TYPE_MAP.get(raw_type, raw_type.lower())

        targets_raw = candidate_targets_wire.get(dpcode) or item.get("candidate_targets") or []
        defs.append(
            DPDefinition(
                dpcode=dpcode,
                direction=direction,
                dp_type=dp_type,
                sub_type=item.get("sub_type"),
                candidate_targets=_parse_candidate_targets(targets_raw),
                component_hint=item.get("component_hint"),
                match_hints=_parse_match_hints(item.get("match_hints")),
            )
        )
    return defs


def _parse_disambiguate(raw: dict[str, Any] | None):
    """Parse the disambiguate section into a DisambiguateHints object, or None."""
    if raw is None:
        return None
    from .models import DisambiguateHints

    return DisambiguateHints(
        name_keywords=raw.get("name_keywords", []),
        positive_attrs=set(raw.get("positive_attrs", [])),
        negative_attrs=set(raw.get("negative_attrs", [])),
        weight=float(raw.get("weight", 0.0)),
        # NOTE: bundle.py carries a second copy of this parser — keep the
        # accepted keys in sync (they have already drifted once, silently
        # dropping required_device_classes and breaking the identity gate).
        required_device_classes=set(raw.get("required_device_classes", [])),
    )


def _parse_domain_feature_reqs(raw_list: list[dict[str, Any]]):
    """Parse required_any_features list."""
    from .models import DomainFeatureReq

    reqs = []
    for item in raw_list:
        reqs.append(
            DomainFeatureReq(
                domain=item.get("domain", ""),
                features=item.get("features", []),
            )
        )
    return reqs


def _parse_pidspecs(raw_list: list[dict[str, Any]]) -> list[PidSpec]:
    """Parse the pidspecs list of a bundle dict into PidSpec objects."""
    specs: list[PidSpec] = []
    for item in raw_list:
        try:
            candidate_targets_wire = item.get("candidate_targets", {})
            spec = PidSpec(
                product_id=item.get("product_id", ""),
                category_code=item.get("category_code", ""),
                required_domains=frozenset(item.get("required_domains", [])),
                optional_domains=frozenset(item.get("optional_domains", [])),
                required_any_features=_parse_domain_feature_reqs(
                    item.get("required_any_features", [])
                ),
                required_dps=item.get("required_dps", []),
                optional_dps=item.get("optional_dps", []),
                dp_definitions=_parse_dp_definitions(
                    item.get("dp_definitions", []),
                    candidate_targets_wire if isinstance(candidate_targets_wire, dict) else None,
                ),
                disambiguate=_parse_disambiguate(item.get("disambiguate")),
                ignore_domains=frozenset(item.get("ignore_domains", [])),
                min_domain_coverage=float(
                    item.get("min_domain_coverage", 0.5)
                ),
            )
            specs.append(spec)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "rule_cache: skipping malformed pidspec entry: %s", exc
            )
    return specs


def parse_bundle(bundle: dict[str, Any]) -> tuple[int, list[PidSpec], FeatureRules, list[ComponentRule]]:
    """Parse a raw bundle dict into typed rule objects.

    Returns:
        (rule_version, pidspecs, feature_rules, component_rules)

    Raises ValueError on invalid / missing rule_version.
    """
    raw_version = bundle.get("rule_version")
    if raw_version is None:
        raise ValueError("bundle missing 'rule_version'")
    rule_version = int(raw_version)

    pidspecs = _parse_pidspecs(bundle.get("pidspecs", []))

    raw_fr = bundle.get("feature_rules")
    feature_rules = _parse_feature_rules(raw_fr) if raw_fr else FeatureRules()

    component_rules = _parse_component_rules(bundle.get("component_rules", []))

    return rule_version, pidspecs, feature_rules, component_rules


# ---------------------------------------------------------------------------
# LocalRuleCache
# ---------------------------------------------------------------------------


@dataclass
class LocalRuleCache:
    """In-memory cache of parsed rule objects.

    rule_version == 0 means only the baseline (built-in) rules are active.
    """

    rule_version: int = 0
    pidspecs: list[PidSpec] = field(default_factory=list)
    feature_rules: FeatureRules = field(default_factory=FeatureRules)
    component_rules: list[ComponentRule] = field(default_factory=list)
    last_check_time: float = 0.0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_baseline_only(self) -> bool:
        """True when no cloud / file bundle has been loaded yet."""
        return self.rule_version == 0

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------

    def load_from_bundle(self, bundle: dict[str, Any]) -> bool:
        """Parse *bundle* and atomically replace cache contents.

        Anti-downgrade: silently rejects bundles whose rule_version is lower
        than the currently cached version.

        Returns True on success, False on any parse / validation error.
        """
        try:
            rule_version, pidspecs, feature_rules, component_rules = parse_bundle(
                bundle
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.error(
                "rule_cache: failed to parse bundle: %s", exc
            )
            return False

        # Anti-downgrade guard.
        if rule_version < self.rule_version:
            LOGGER.warning(
                "rule_cache: rejecting bundle version %d (current: %d) — anti-downgrade",
                rule_version,
                self.rule_version,
            )
            return False

        # Atomic replace — all fields updated together.
        self.rule_version = rule_version
        self.pidspecs = pidspecs
        self.feature_rules = feature_rules
        self.component_rules = component_rules

        LOGGER.debug(
            "rule_cache: loaded bundle rule_version=%d (%d pidspecs, %d component_rules)",
            self.rule_version,
            len(self.pidspecs),
            len(self.component_rules),
        )
        return True

    # ------------------------------------------------------------------
    # Class-level constructors
    # ------------------------------------------------------------------

    @classmethod
    def load_from_local_file(cls) -> "LocalRuleCache":
        """Construct a LocalRuleCache pre-loaded from the local rules.json file.

        Falls back to a baseline-only cache on any error.

        NOTE: Blocking I/O. Prefer :meth:`load_from_local_file_async` when called
        from within the Home Assistant event loop.
        """
        from ..rules import load_rules_bundle

        cache = cls()
        try:
            bundle = load_rules_bundle()
            cache.load_from_bundle(bundle)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error(
                "rule_cache: could not load local file, using baseline rules: %s", exc
            )
        return cache

    @classmethod
    async def load_from_local_file_async(
        cls, hass: "HomeAssistant"
    ) -> "LocalRuleCache":
        """Async variant of :meth:`load_from_local_file`.

        Offloads the blocking file read to a thread via
        :func:`asyncio.to_thread` so the event loop is not blocked.
        """
        from ..rules import async_load_rules_bundle

        cache = cls()
        try:
            bundle = await async_load_rules_bundle()
            cache.load_from_bundle(bundle)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error(
                "rule_cache: could not load local file, using baseline rules: %s", exc
            )
        return cache
