"""Deterministic scoring: run facts, prediction checks and resilience-rubric anchor scores.

Pure functions over saved results - the same inputs always give the same numbers. The analyst agent scores the
services against the same rubric; these anchors keep its scores honest (guardrail: within +-SCORE_TOLERANCE) and
make before/after comparisons (fix-and-rerun) fair.
"""
from __future__ import annotations

import math

from . import config as cfg
from .models import (Baseline, ExperimentType, Graph, Prediction, PredictionCheck, RunResult, RunStatus,
                     ServiceNode)

SCORE_VERSION = "1"

# Rubric dimension -> weight in a service's score. The descriptions are given to the analyst verbatim.
WEIGHTS = {"availability": 0.35, "latency": 0.25, "recovery": 0.20, "containment": 0.10, "posture": 0.10}
RUBRIC = {
    "availability": "Did users keep getting successful responses? 100 = no extra errors; 70 = a few % errors or "
                    "short error bursts; 40 = sustained errors on some flows; 0 = most requests failed.",
    "latency": "How much slower did user flows get (worst p95 vs baseline)? 100 = < 1.5x; 80 = ~3x; 60 = ~10x; "
               "30 = ~50x; 0 = several seconds / timeouts.",
    "recovery": "How fast were users back to normal after the fault ended? 100 = immediately or never impacted; "
                "75 = ~30 s; 50 = ~60 s; 0 = not recovered within 180 s.",
    "containment": "Did the failure stay contained? 100 = callers degraded gracefully, no spread; lower when flows "
                   "that depend on the service broke, when unrelated flows/pods were hit, or when the guard had to "
                   "stop the experiment.",
    "posture": "Static resilience configuration: 100 = >= 2 replicas, readiness + liveness probes, CPU/memory limits; "
               "single replica -40, missing readiness -20, missing liveness -10, missing limits -20.",
}
GRADES = [(90, "A"), (80, "B"), (70, "C"), (60, "D"), (0, "F")]


def _clamp(v: float) -> float:
    return round(max(0.0, min(100.0, v)), 1)


def grade(score: float) -> str:
    return next(g for limit, g in GRADES if score >= limit)


# --- Run facts -------------------------------------------------------------------------------------------
def affected_flows(result: RunResult) -> list[str]:
    """Flows whose error rate or p95 rose clearly above baseline during the impact window."""
    out = []
    for flow, fi in result.flow_impact.items():
        base_err, imp = fi.baseline.error_rate, fi.impact
        err_hit = imp.error_rate > base_err + cfg.AFFECTED_ERROR_SLACK
        slow_hit = ((fi.p95_factor or 0) > cfg.AFFECTED_P95_FACTOR
                    and (imp.p95_ms or 0) - (fi.baseline.p95_ms or 0) > cfg.AFFECTED_P95_MIN_MS)
        if err_hit or slow_hit:
            out.append(flow)
    return out


def slowdown_factor(result: RunResult) -> float:
    """Worst per-flow p95 factor (impact window vs baseline); 1.0 when nothing got slower."""
    factors = [fi.p95_factor for fi in result.flow_impact.values() if fi.p95_factor]
    return max(factors, default=1.0)


def error_band(rate: float) -> str:
    return "none" if rate <= cfg.ERROR_BAND_LOW else "low" if rate <= cfg.ERROR_BAND_HIGH else "high"


def slowdown_band(factor: float) -> str:
    return "none" if factor < cfg.SLOWDOWN_MODERATE else "moderate" if factor <= cfg.SLOWDOWN_SEVERE else "severe"


def observed(result: RunResult) -> Prediction:
    """What actually happened, in the same terms the planner predicts (error band uses the peak 5 s window)."""
    outcome = "ABORTED" if result.status == RunStatus.aborted else "COMPLETED"
    return Prediction(affected_flows=sorted(result.affected_flows), error_band=error_band(result.peak_error_rate),
                      slowdown_band=slowdown_band(slowdown_factor(result)), outcome=outcome)


def check_prediction(pred: Prediction, result: RunResult) -> PredictionCheck:
    """confirmed = all 4 aspects match, partly = 2-3, refuted = 0-1. Flows match on Jaccard similarity >= 0.5."""
    obs = observed(result)
    want, got = set(pred.affected_flows), set(obs.affected_flows)
    union = want | got
    flows_ok = not union or len(want & got) / len(union) >= 0.5
    checks = {
        "affected flows": (flows_ok, sorted(want), sorted(got)),
        "error band": (pred.error_band == obs.error_band, pred.error_band, obs.error_band),
        "slowdown band": (pred.slowdown_band == obs.slowdown_band, pred.slowdown_band, obs.slowdown_band),
        "outcome": (pred.outcome == obs.outcome, pred.outcome, obs.outcome),
    }
    matched = sum(ok for ok, _, _ in checks.values())
    details = [f"{name}: predicted {p}, observed {o} -> {'match' if ok else 'MISS'}"
               for name, (ok, p, o) in checks.items()]
    verdict = "confirmed" if matched == len(checks) else "partly" if matched >= 2 else "refuted"
    if result.status not in (RunStatus.completed, RunStatus.aborted):
        details.append(f"run status {result.status.value}: prediction only partly comparable")
    return PredictionCheck(verdict=verdict, matched=matched, total=len(checks), observed=obs, details=details)


# --- Anchor scores ---------------------------------------------------------------------------------------
def run_scores(result: RunResult, node: ServiceNode | None) -> dict[str, float]:
    """Rubric anchors for one experiment (availability, latency, recovery, containment)."""
    impact_err = result.impact_error_rate or 0.0
    availability = 100 - min(100.0, 150 * impact_err + 50 * result.peak_error_rate)

    f = slowdown_factor(result)
    latency = 100.0 if f <= 1.5 else 100 - 40 * math.log10(f / 1.5)

    if result.status == RunStatus.not_recovered:
        recovery = 0.0
    elif not result.affected_flows and result.peak_error_rate <= cfg.ERROR_BAND_LOW:
        recovery = 100.0
    else:
        recovery = 100 - (result.recovery_s or 0.0) / 1.2       # 30 s -> 75, 60 s -> 50, 120 s -> 0

    affected = set(result.affected_flows)
    reach = set(node.reach_flows) if node else set()
    containment = 100.0
    if reach:
        containment -= 50 * len(affected & reach) / len(reach)  # graceful degradation keeps this low
    containment -= 20 * len(affected - reach)                   # spread to flows that don't depend on it
    if result.status == RunStatus.aborted and (result.abort_reason or "").startswith("guard"):
        containment -= 20
    if any(b.startswith("blast radius") for b in result.guard_breaches):
        containment -= 30
    return {"availability": _clamp(availability), "latency": _clamp(latency), "recovery": _clamp(recovery),
            "containment": _clamp(containment)}


def posture_score(node: ServiceNode) -> float:
    s = 100.0
    if node.replicas < 2:
        s -= 40
    if not node.readiness_probe:
        s -= 20
    if not node.liveness_probe:
        s -= 10
    if not (node.cpu_limit and node.memory_limit):
        s -= 20
    return _clamp(s)


def combine(dims: dict[str, float]) -> float:
    """Weighted service score from rubric dimensions (missing dimensions re-normalise the weights)."""
    present = {k: w for k, w in WEIGHTS.items() if dims.get(k) is not None}
    total = sum(present.values())
    return round(sum(dims[k] * w for k, w in present.items()) / total, 1) if total else 0.0


def default_criticality(node: ServiceNode) -> float:
    """Code's default weight of a service in the overall score (the analyst may choose its own, 0.5-3)."""
    return round(1 + 2 * node.reach + (0.5 if node.on_checkout_path else 0) + (0.5 if node.stateful else 0), 2)


def overall(scores: list[tuple[float, float]]) -> float:
    """Criticality-weighted mean of (service score, weight) pairs."""
    total = sum(w for _, w in scores)
    return round(sum(s * w for s, w in scores) / total, 1) if total else 0.0


def scorable(result: RunResult) -> bool:
    return result.status in (RunStatus.completed, RunStatus.aborted, RunStatus.not_recovered) \
        and result.injected_at is not None


def service_anchors(results: list[RunResult], graph: Graph) -> dict[str, dict]:
    """Per targetable service: worst-case anchor per dimension over the experiments on it, + posture."""
    out: dict[str, dict] = {}
    for name, node in sorted(graph.nodes.items()):
        if not node.targetable:
            continue
        runs = [r for r in results if r.spec.target == name and scorable(r)]
        dims: dict[str, float | None] = {k: None for k in WEIGHTS}
        for r in runs:
            for k, v in run_scores(r, node).items():
                dims[k] = v if dims[k] is None else min(dims[k], v)
        dims["posture"] = posture_score(node)
        score = combine(dims)
        out[name] = {"dimensions": dims, "score": score, "grade": grade(score), "tested": bool(runs),
                     "runs": [r.run_id for r in runs], "criticality": default_criticality(node),
                     "reach": node.reach, "reach_flows": node.reach_flows}
    return out


def anchor_overall(anchors: dict[str, dict]) -> float:
    return overall([(a["score"], a["criticality"]) for a in anchors.values() if a["tested"]])


def coverage(results: list[RunResult]) -> dict:
    """Which failure classes and types were exercised (Day-2 minimum: >= 3 types)."""
    from .templates import TEMPLATES
    done = [r for r in results if scorable(r)]
    types = sorted({r.spec.type.value for r in done})
    classes = sorted({TEMPLATES[ExperimentType(t)].failure_class for t in types})
    return {"experiments": len(done), "types": types, "failure_classes": classes}
