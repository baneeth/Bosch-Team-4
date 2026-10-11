"""Planner agent: ranks and selects experiments from the dependency map, baseline, catalog and past runs.

The agent only proposes. Code then validates every experiment (hard caps + AI policy + duplicates + budget, and a
Kubernetes server-side dry-run when a cluster is available) and records what it rejected and why.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .. import config as cfg
from .. import policy, templates
from ..models import (Baseline, ExperimentSpec, ExperimentType, Graph, PlannedRun, PlanRecord, Prediction,
                      RunResult)
from . import client, tools
from .schemas import ExperimentPlan, to_spec

INSTRUCTIONS = """\
You are the experiment planner of an AI-driven chaos-engineering agent. The system under test is Google's Online
Boutique (11 gRPC microservices) on a single-node Kubernetes (k3s) cluster. Chaos Mesh injects the faults; a
deterministic safety guard watches real user flows every second and removes the fault when users are hurt too much.

Your job: choose and RANK the experiments that will teach us the most about the shop's resilience, riskiest and
most informative first, within the budget you are given.

How to decide:
- Use the dependency map: services on many user flows (high reach), shared dependencies (fan-in), single replicas,
  the checkout (revenue) path and stateful services are the most likely weak spots.
- Use observed behaviour: past runs show what is already known. Do not repeat an experiment whose outcome is
  already known unless the repeat answers a new question (e.g. a different intensity); prefer gaps.
- Cover at least 3 different failure classes (errors, slowdown, recovery, resource exhaustion) in the plan.
- Respect the safety guard: it aborts when the error rate exceeds 20 % or p95 exceeds 1500 ms for 2 checks in a
  row. An expected abort is acceptable for a short error-class experiment (it measures the blast radius), but a
  slowdown experiment that would just trip the guard teaches little - pick an intensity below it (Day-1 lesson:
  +100 ms on productcatalogservice already makes pages ~700 ms slower because of ~7 sequential calls).
- Respect the AI policy rules and the per-type parameter caps. Use the check_experiment tool to verify each
  experiment before you include it; fix or drop anything it rejects.
- Keep durations short (30-60 s is usually enough; traffic spikes need ramp time).
- For every experiment give: a reason that cites map facts or evidence, a steady-state hypothesis, a checkable
  prediction (affected flows, error band, slowdown band, outcome) and the question it answers.
- List ideas you considered but dropped, with why (e.g. too destructive, already known, blocked by policy).
Answer only with the structured plan.
"""


def planner_input(graph: Graph, baseline: Baseline, runs: dict[str, RunResult], max_experiments: int,
                  focus: str | None) -> str:
    return json.dumps({
        "budget": {"max_experiments": max_experiments,
                   "note": "each experiment takes ~1.5-4 min incl. observation and recovery"},
        "focus": focus or "overall resilience of the shop",
        "user_flows": list(cfg.FLOW_CALLS),
        "dependency_map": tools.graph_view(graph),
        "baseline": tools.baseline_view(baseline),
        "experiment_catalog": templates.catalog(),
        "safety_guard": {"max_error_rate": cfg.GUARD_ERROR_RATE, "max_p95_ms": cfg.GUARD_P95_FLOOR_MS,
                         "consecutive_checks": cfg.GUARD_CONSECUTIVE,
                         "frontend_silent_s": cfg.GUARD_UNRESPONSIVE_S,
                         "blast_radius": "abort if any non-target pod becomes NotReady"},
        "ai_policy": policy.RULES,
        "past_runs": [tools.run_view(r) for r in runs.values()],
    }, indent=1)


def build_agent():
    from agents import Agent
    return Agent(name="planner", instructions=INSTRUCTIONS, model=client.model(),
                 tools=[tools.get_service, tools.list_runs, tools.get_run, tools.check_experiment],
                 output_type=client.strict_output(ExperimentPlan), model_settings=client.model_settings("planner"))


def new_plan_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-plan"


def finalize(candidates: list[tuple[ExperimentSpec, str | None]], graph: Graph, max_experiments: int,
             kube=None) -> tuple[list[PlannedRun], list[dict]]:
    """Validate candidates in rank order: caps + AI policy, duplicates, budget, server-side dry-run."""
    accepted: list[PlannedRun] = []
    rejected: list[dict] = []
    seen: set[tuple] = set()
    for spec, goal in candidates:
        item = {"type": spec.type.value, "target": spec.target, "params": spec.params, "duration_s": spec.duration_s}
        try:
            policy.check(spec, graph)
        except templates.SpecError as e:
            rejected.append({**item, "error": str(e)})
            continue
        if policy.key(spec) in seen:
            rejected.append({**item, "error": "duplicate of a higher-ranked experiment"})
            continue
        if len(accepted) >= max_experiments:
            rejected.append({**item, "error": f"over budget ({max_experiments} experiments)"})
            continue
        err = dry_run(spec, graph, kube)
        if err:
            rejected.append({**item, "error": f"Kubernetes dry-run: {err}"})
            continue
        seen.add(policy.key(spec))
        accepted.append(PlannedRun(rank=len(accepted) + 1, spec=spec, learning_goal=goal))
    return accepted, rejected


def dry_run(spec: ExperimentSpec, graph: Graph, kube) -> str | None:
    if kube is None or templates.TEMPLATES[spec.type].engine != "chaos-mesh":
        return None
    try:
        manifest = templates.render(spec, templates.validate(spec, graph), "plan-dry-run")
        kube.create_chaos(manifest, dry_run=True)
    except Exception as e:                                   # ApiException carries the webhook's message
        from ..runner import _api_error
        return _api_error(e)
    return None


def coverage_of(runs: list[PlannedRun]) -> list[str]:
    return sorted({templates.TEMPLATES[r.spec.type].failure_class for r in runs})


async def ai_plan(graph: Graph, baseline: Baseline, runs: dict[str, RunResult], max_experiments: int,
                  focus: str | None = None, kube=None) -> PlanRecord:
    ctx = tools.ToolContext(graph=graph, baseline=baseline, runs=runs)
    agent = build_agent()
    out, transcript = await client.run_agent(agent, planner_input(graph, baseline, runs, max_experiments, focus),
                                             name="planner", context=ctx)
    plan: ExperimentPlan = out
    accepted, rejected = finalize([(to_spec(pe, "planner"), pe.learning_goal) for pe in plan.experiments],
                                  graph, max_experiments, kube)
    return PlanRecord(plan_id=new_plan_id(), created_at=_now(), source="ai", model=agent.model,
                      strategy=plan.strategy, experiments=accepted, rejected=rejected,
                      skipped_ideas=[s.model_dump() for s in plan.skipped_ideas], coverage=coverage_of(accepted),
                      transcript=transcript)


# --- Deterministic fallback (no API / API failure) -------------------------------------------------------
def fallback_plan(graph: Graph, max_experiments: int, kube=None, note: str | None = None) -> PlanRecord:
    """Rank by risk and reach, then walk a fixed recipe that covers every failure class. Labelled 'fallback'."""
    order = {"high": 0, "medium": 1, "low": 2}
    apps = sorted((n for n in graph.nodes.values() if n.targetable and not n.is_entrypoint),
                  key=lambda n: (order[n.risk], -n.reach, -n.fan_in, n.name))
    by_fan_in = sorted(apps, key=lambda n: (-n.fan_in, -n.reach, n.name))
    if not apps:
        return PlanRecord(plan_id=new_plan_id(), created_at=_now(), source="fallback", note="no targetable services")

    def pick(i: int, pool=apps):
        return pool[min(i, len(pool) - 1)]

    def mk(t, node_name, params, dur, why, flows, err, slow, outcome="COMPLETED"):
        return (ExperimentSpec(type=t, target=node_name, params=params, duration_s=dur, origin="fallback",
                               reason=why, hypothesis=f"Users on {', '.join(flows) or 'no flow'} are affected.",
                               prediction=Prediction(affected_flows=flows, error_band=err, slowdown_band=slow,
                                                     outcome=outcome)), None)

    top, shared, second = pick(0), pick(0, by_fan_in), pick(1)
    fe = graph.nodes.get(cfg.ENTRYPOINT)
    candidates = [
        mk(ExperimentType.pod_failure, top.name, {}, 30, f"highest risk + reach ({top.reach:.0%} of flows)",
           top.reach_flows, "high", "none", "ABORTED"),
        mk(ExperimentType.network_delay, shared.name, {"latency_ms": 100}, 45,
           f"shared dependency of {shared.fan_in} services", shared.reach_flows, "none", "severe"),
        mk(ExperimentType.traffic_spike, cfg.ENTRYPOINT, {"users": 150, "ramp_s": 30}, 90,
           "every request passes through the entrypoint", list(cfg.FLOW_CALLS), "none", "severe"),
        mk(ExperimentType.pod_kill, second.name, {}, 30, f"single replica, risk {second.risk}",
           second.reach_flows, "low", "none"),
        mk(ExperimentType.network_loss, second.name, {"loss_pct": 20}, 45, "partial network failure on a "
           "single-replica dependency", second.reach_flows, "low", "moderate"),
        mk(ExperimentType.cpu_stress, pick(2).name, {"workers": 1, "load": 80}, 45, "CPU contention inside the "
           "container limit", pick(2).reach_flows, "none", "moderate"),
    ]
    if fe is None:
        candidates = [c for c in candidates if c[0].type != ExperimentType.traffic_spike]
    accepted, rejected = finalize(candidates, graph, max_experiments, kube)
    return PlanRecord(plan_id=new_plan_id(), created_at=_now(), source="fallback",
                      strategy="Deterministic fallback (no AI): highest risk/reach services first, one experiment "
                               "per failure class.", experiments=accepted, rejected=rejected,
                      coverage=coverage_of(accepted), note=note)


async def make_plan(graph: Graph, baseline: Baseline, runs: dict[str, RunResult], max_experiments: int,
                    focus: str | None = None, kube=None, use_ai: bool = True) -> PlanRecord:
    """AI plan; on any AI failure the deterministic fallback (clearly labelled)."""
    if use_ai:
        try:
            return await ai_plan(graph, baseline, runs, max_experiments, focus, kube)
        except client.AIError as e:
            return fallback_plan(graph, max_experiments, kube, note=f"AI planner unavailable: {e}")
    return fallback_plan(graph, max_experiments, kube, note="fallback requested")


def save(plan: PlanRecord) -> str:
    cfg.PLANS_DIR.mkdir(parents=True, exist_ok=True)
    path = cfg.PLANS_DIR / f"{plan.plan_id}.json"
    path.write_text(plan.model_dump_json(indent=2))
    return str(path)


def load(plan_id: str | None) -> PlanRecord:
    """Plan by id (or part of it); latest when None."""
    files = sorted(cfg.PLANS_DIR.glob("*.json")) if cfg.PLANS_DIR.exists() else []
    match = files[-1:] if not plan_id else [f for f in files if plan_id in f.stem]
    if not match:
        raise FileNotFoundError(f"no plan matching '{plan_id or 'latest'}' in {cfg.PLANS_DIR}")
    return PlanRecord.model_validate_json(match[-1].read_text())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
