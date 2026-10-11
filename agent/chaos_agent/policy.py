"""AI policy: extra rules for experiments proposed by an AI agent, on top of the hard caps in templates.py.

A human can still run any experiment within the caps (e.g. a frontend pod kill for a demo); the AI may not.
"""
from __future__ import annotations

from . import config as cfg
from . import templates
from .models import ExperimentSpec, ExperimentType, Graph

OUTAGE_TYPES = {ExperimentType.pod_kill, ExperimentType.pod_failure}
ERROR_TYPES = {ExperimentType.pod_failure, ExperimentType.network_loss}

RULES = [
    f"no pod_kill / pod_failure on {', '.join(sorted(cfg.AI_NO_OUTAGE_TARGETS))} (that is a total outage, not an experiment)",
    f"pod_failure and network_loss last at most {cfg.AI_ERROR_FAULT_MAX_S} s",
    f"traffic_spike at most {cfg.AI_MAX_LOAD_USERS} users",
    "guard thresholds may only be made stricter than the defaults (lower max_error_rate / max_p95_ms)",
    "no duplicate experiments (same type, target and parameters)",
]


def check(spec: ExperimentSpec, graph: Graph) -> None:
    """Hard caps (templates.validate) + AI rules. Raises templates.SpecError with the reason."""
    params = templates.validate(spec, graph)
    if spec.type in OUTAGE_TYPES and spec.target in cfg.AI_NO_OUTAGE_TARGETS:
        raise templates.SpecError(f"AI policy: no {spec.type.value} on {spec.target} (total outage)")
    if spec.type in ERROR_TYPES and spec.duration_s > cfg.AI_ERROR_FAULT_MAX_S:
        raise templates.SpecError(f"AI policy: {spec.type.value} may last at most {cfg.AI_ERROR_FAULT_MAX_S}s")
    if isinstance(params, templates.TrafficSpikeParams) and params.users > cfg.AI_MAX_LOAD_USERS:
        raise templates.SpecError(f"AI policy: traffic_spike at most {cfg.AI_MAX_LOAD_USERS} users")
    if spec.max_error_rate is not None and spec.max_error_rate > cfg.GUARD_ERROR_RATE:
        raise templates.SpecError(f"AI policy: max_error_rate may only be stricter than {cfg.GUARD_ERROR_RATE}")
    if spec.max_p95_ms is not None and spec.max_p95_ms > cfg.GUARD_P95_FLOOR_MS:
        raise templates.SpecError(f"AI policy: max_p95_ms may only be stricter than {cfg.GUARD_P95_FLOOR_MS:.0f}")


def key(spec: ExperimentSpec) -> tuple:
    """Identity of an experiment for duplicate detection (a different duration alone is still a duplicate)."""
    try:                                                   # normalise: fill in template defaults
        params = templates.TEMPLATES[spec.type].params(**spec.params).model_dump()
    except Exception:
        params = dict(spec.params)
    return spec.type.value, spec.target, tuple(sorted(params.items()))
