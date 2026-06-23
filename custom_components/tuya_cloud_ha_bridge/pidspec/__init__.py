"""PidSpec inference engine for tuya_cloud_ha_bridge.

Public API:
- LocalRuleCache: rule storage with version-gated update
- infer_pid: PID inference from DeviceProfile
- resolve_dp_routes: DP-to-entity resolution
- discover_features: feature extraction from EntityProfile
- discover_component: component discovery from EntityProfile
"""

from .components import discover_component
from .dispatcher import dispatch_ha_to_tuya, dispatch_tuya_to_ha
from .features import discover_features
from .inference import infer_pid
from .dp_routing import resolve_dp_routes
from .orchestrator import async_infer_with_refresh, InferenceResults
from .rule_cache import LocalRuleCache

__all__ = [
    "InferenceResults",
    "LocalRuleCache",
    "async_infer_with_refresh",
    "discover_component",
    "discover_features",
    "dispatch_ha_to_tuya",
    "dispatch_tuya_to_ha",
    "infer_pid",
    "resolve_dp_routes",
]
