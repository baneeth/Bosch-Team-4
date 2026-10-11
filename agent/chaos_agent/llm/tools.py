"""Read-only tools for the agents, plus compact JSON views of our data used in their prompts.

Nothing here changes the cluster: the most "active" tool is check_experiment, a dry run of our validator.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field

from agents import RunContextWrapper, function_tool

from .. import config as cfg
from .. import fixes, policy, scoring, templates
from ..models import Baseline, ExperimentSpec, ExperimentType, Graph, RunResult


@dataclass
class ToolContext:
    graph: Graph
    baseline: Baseline
    runs: dict[str, RunResult] = field(default_factory=dict)
    checks: list[str] = field(default_factory=list)          # check_experiment calls (for the transcript)


def load_runs(run_ids: list[str] | None = None) -> dict[str, RunResult]:
    """Saved results, oldest first (optionally only the given run ids)."""
    out: dict[str, RunResult] = {}
    if not cfg.RUNS_DIR.exists():
        return out
    for d in sorted(cfg.RUNS_DIR.iterdir()):
        f = d / "result.json"
        if f.exists() and (run_ids is None or d.name in run_ids):
            try:
                out[d.name] = RunResult.model_validate_json(f.read_text())
            except ValueError:
                continue                                       # result from an older format - skip
    return out


# --- Compact views (prompt material) ---------------------------------------------------------------------
def graph_view(g: Graph) -> list[dict]:
    rows = []
    for n in sorted(g.nodes.values(), key=lambda n: n.name):
        if not n.exists:
            rows.append({"service": n.name, "missing": True, "referenced_by": n.callers})
            continue
        if n.role != "app":
            rows.append({"service": n.name, "role": n.role, "targetable": False})
            continue
        rows.append({"service": n.name, "replicas": n.replicas, "risk": n.risk, "risk_reasons": n.risk_reasons,
                     "depends_on": n.depends_on, "callers": n.callers, "dependents": n.dependents,
                     "reach": n.reach, "reach_flows": n.reach_flows, "on_checkout_path": n.on_checkout_path,
                     "stateful": n.stateful, "cpu_limit": n.cpu_limit, "memory_limit": n.memory_limit,
                     "readiness_probe": n.readiness_probe, "liveness_probe": n.liveness_probe})
    return rows


def baseline_view(b: Baseline) -> dict:
    def s(x):
        return {"requests": x.count, "error_rate": x.error_rate, "p50_ms": x.p50_ms, "p95_ms": x.p95_ms}
    return {"measured_at": b.generated_at, "overall": s(b.overall), "flows": {k: s(v) for k, v in b.flows.items()}}


def run_view(r: RunResult) -> dict:
    """One line of facts per run (what the planner/adapter see for past runs)."""
    view = {"run_id": r.run_id, "type": r.spec.type.value, "target": r.spec.target, "params": r.spec.params,
            "duration_s": r.spec.duration_s, "origin": r.spec.origin, "status": r.status.value,
            "abort_reason": r.abort_reason, "peak_error_rate": round(r.peak_error_rate, 3),
            "impact_error_rate": r.impact_error_rate, "peak_p95_ms": r.peak_p95_ms,
            "slowdown_factor": scoring.slowdown_factor(r), "affected_flows": r.affected_flows or scoring.affected_flows(r),
            "worst_flow": r.worst_flow, "recovery_s": r.recovery_s, "pod_ready_s": r.pod_ready_s,
            "pod_restarts": r.pod_restarts, "fault_active_s": r.fault_duration_s}
    if r.load:
        view["load"] = r.load.model_dump(exclude_none=True)
    if r.prediction_check:
        view["prediction"] = {"verdict": r.prediction_check.verdict, "details": r.prediction_check.details}
    return view


def run_detail(r: RunResult) -> dict:
    view = run_view(r)
    view["thresholds"] = r.thresholds.model_dump()
    view["reason"], view["hypothesis"] = r.spec.reason, r.spec.hypothesis
    view["flow_impact"] = {f: {"baseline_p95_ms": fi.baseline.p95_ms, "p95_ms": fi.impact.p95_ms,
                               "p95_factor": fi.p95_factor, "error_rate": round(fi.impact.error_rate, 3),
                               "requests": fi.impact.count} for f, fi in r.flow_impact.items()}
    top = sorted(r.resources.items(), key=lambda kv: -(kv[1].cpu_peak_pct_of_limit or 0))[:4]
    view["resources_top"] = {k: v.model_dump(exclude_none=True) for k, v in top}
    view["vm_cpu_pct"] = {"before": r.node_cpu_baseline_pct, "peak": r.node_cpu_peak_pct}
    view["guard_breaches"], view["notes"] = r.guard_breaches, r.notes
    return view


def _find_run(ctx: ToolContext, run_id: str) -> RunResult | None:
    if run_id in ctx.runs:
        return ctx.runs[run_id]
    matches = [k for k in ctx.runs if run_id and run_id in k]
    return ctx.runs[matches[-1]] if matches else None


def check_spec(spec: ExperimentSpec, graph: Graph) -> str:
    try:
        policy.check(spec, graph)
    except templates.SpecError as e:
        return f"REJECTED: {e}"
    return "OK: within hard caps and AI policy"


# --- Tools -----------------------------------------------------------------------------------------------
@function_tool
def get_service(ctx: RunContextWrapper[ToolContext], name: str) -> str:
    """Full dependency-map entry of one service (replicas, probes, limits, callers, dependents, reach, risk).

    Args:
        name: Deployment name, e.g. cartservice.
    """
    node = ctx.context.graph.nodes.get(name)
    return node.model_dump_json() if node else f"unknown service '{name}'"


@function_tool
def list_runs(ctx: RunContextWrapper[ToolContext]) -> str:
    """Compact facts of every saved experiment run (oldest first)."""
    return json.dumps([run_view(r) for r in ctx.context.runs.values()])


@function_tool
def get_run(ctx: RunContextWrapper[ToolContext], run_id: str) -> str:
    """Detailed result of one run: per-flow impact, resources, guard breaches, prediction check.

    Args:
        run_id: Run id (or a unique part of it).
    """
    r = _find_run(ctx.context, run_id)
    return json.dumps(run_detail(r)) if r else f"no run matching '{run_id}'"


@function_tool
def get_timeline(ctx: RunContextWrapper[ToolContext], run_id: str) -> str:
    """Per-second guard checks of a run, downsampled to every 5 s: phase, health, error rate, p95.

    Args:
        run_id: Run id (or a unique part of it).
    """
    r = _find_run(ctx.context, run_id)
    if not r:
        return f"no run matching '{run_id}'"
    path = cfg.RUNS_DIR / r.run_id / "timeline.csv"
    if not path.exists():
        return "no timeline saved"
    with open(path) as f:
        rows = list(csv.DictReader(f))
    keep = ("t_since_inject_s", "phase", "health", "n", "error_rate", "p95_ms", "breaches")
    return json.dumps([{k: row.get(k) for k in keep if row.get(k) not in (None, "")}
                       for i, row in enumerate(rows) if i % 5 == 0 or row.get("breaches")])


@function_tool
def check_experiment(ctx: RunContextWrapper[ToolContext], type: str, target: str, duration_s: int,
                     params_json: str, max_error_rate: float | None, max_p95_ms: float | None) -> str:
    """Dry-run our validator (hard caps + AI policy) on an experiment before you propose it. Injects nothing.

    Args:
        type: Experiment type, e.g. network_delay.
        target: Deployment name.
        duration_s: Fault duration in seconds.
        params_json: The params object as JSON, e.g. {"latency_ms": 100, "jitter_ms": 0}.
        max_error_rate: Optional stricter guard error-rate limit, or null.
        max_p95_ms: Optional stricter guard p95 limit in ms, or null.
    """
    try:
        spec = ExperimentSpec(type=ExperimentType(type), target=target, duration_s=duration_s,
                              params=json.loads(params_json or "{}"), max_error_rate=max_error_rate,
                              max_p95_ms=max_p95_ms)
    except (ValueError, TypeError) as e:
        verdict = f"REJECTED: malformed experiment ({e})"
    else:
        verdict = check_spec(spec, ctx.context.graph)
    ctx.context.checks.append(f"{type} {target} {params_json} {duration_s}s -> {verdict}")
    return verdict


@function_tool
def get_fix_catalog(ctx: RunContextWrapper[ToolContext]) -> str:
    """The fix actions you may recommend, what each addresses, and caveats."""
    return json.dumps(fixes.CATALOG)
