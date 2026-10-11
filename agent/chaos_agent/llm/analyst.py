"""Analyst agent: findings ranked by severity, structured fixes, and the resilience score.

Code computes the facts and an anchor score per rubric dimension (scoring.py); the AI scores every service against
the same rubric, explains, ranks and recommends. A code guardrail checks the answer (known run ids, services and
fix targets, valid verification experiments, scores within +-SCORE_TOLERANCE of the anchors) and asks for one
correction; remaining deviations are clamped and recorded. Final service/overall scores are combined by code.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .. import config as cfg
from .. import fixes, policy, scoring, templates
from ..models import AnalysisRecord, Baseline, Graph, RunResult, ServiceVerdict
from . import client, tools
from .schemas import SEVERITY_ORDER, FindingsReport, experiment_to_spec

SEVERITY = {
    "critical": "users cannot shop or check out (outage or most requests failing) and nothing contains it",
    "high": "a core flow (product, cart, checkout) breaks or becomes unusably slow (seconds), or recovery is slow",
    "medium": "noticeable degradation with a workaround or limited scope (one flow, moderate slowdown)",
    "low": "minor impact, or a configuration weakness without measured user impact yet",
}

INSTRUCTIONS = f"""\
You are the resilience analyst of an AI-driven chaos-engineering agent. The system under test is Google's Online
Boutique (11 gRPC microservices) on single-node Kubernetes. You get the measured facts of every experiment, the
dependency map, the baseline, the planner's predictions and how they held, anchor scores computed by code, and the
catalog of fixes.

Produce:
1. Findings = weaknesses, ranked by severity (most severe first). Severity scale: {json.dumps(SEVERITY)}.
   Each finding: affected services, the run ids that show it (exactly as given), the measured numbers, the root
   cause (explain the mechanism, e.g. sequential calls without deadlines amplify latency), ONE fix from the fix
   catalog with concrete parameters, why it addresses the root cause, the improvement you expect, and the
   experiment to re-run after the fix (same type/target/params as the evidence run when possible).
   Positive findings (e.g. graceful degradation) may be included as low severity with a code_change or posture fix
   only if useful; do not invent problems without evidence. Merge findings that share one root cause.
2. Resilience scores: for EVERY service in the anchors table, score each rubric dimension 0-100:
   {json.dumps(scoring.RUBRIC)}
   Start from the anchor and stay within +-{cfg.SCORE_TOLERANCE} of it; deviate only with a reason (e.g. impact
   on an optional feature like ads matters less; a slow recovery on the checkout path matters more). Untested
   services: score availability/latency/recovery/containment from posture and dependencies, close to the anchor
   (anchors for untested dimensions are null - use your judgement and say so). Choose a criticality weight per
   service (0.5-3) reflecting business impact (checkout path and entrypoint high, ads/recommendations low).
3. Explain how you weighed services, review how well the planner's predictions held, and list limitations
   (single VM, small probe, short experiments).
Use only facts you are given or fetch with the tools. Answer only with the structured report.
"""


def analyst_input(results: list[RunResult], graph: Graph, baseline: Baseline, anchors: dict[str, dict]) -> str:
    return json.dumps({
        "baseline": tools.baseline_view(baseline),
        "dependency_map": tools.graph_view(graph),
        "experiments": [tools.run_detail(r) for r in results],
        "anchors": {s: {"dimensions": a["dimensions"], "anchor_score": a["score"], "tested": a["tested"],
                        "runs": a["runs"], "default_criticality": a["criticality"], "reach": a["reach"]}
                    for s, a in anchors.items()},
        "coverage": scoring.coverage(results),
        "fix_catalog": fixes.CATALOG,
    }, indent=1)


def build_agent():
    from agents import Agent
    return Agent(name="analyst", instructions=INSTRUCTIONS, model=client.model(),
                 tools=[tools.get_run, tools.get_timeline, tools.get_service, tools.get_fix_catalog],
                 output_type=client.strict_output(FindingsReport), model_settings=client.model_settings("analyst"))


def problems(report: FindingsReport, graph: Graph, run_ids: set[str], anchors: dict[str, dict]) -> list[str]:
    """Guardrail: everything the report references must exist; scores must stay near the anchors."""
    out: list[str] = []
    last = -1
    for i, f in enumerate(report.findings, 1):
        sev = SEVERITY_ORDER[f.severity]
        if sev < last:
            out.append(f"finding {i} ('{f.title}') is more severe than the one before it - order by severity")
        last = max(last, sev)
        for rid in f.evidence_runs:
            if rid not in run_ids:
                out.append(f"finding {i}: unknown run id '{rid}' (use run ids exactly as given)")
        for s in f.services:
            if s not in graph.nodes:
                out.append(f"finding {i}: unknown service '{s}'")
        out += [f"finding {i}: {p}" for p in fixes.problems(f.fix, graph)]
        if f.verify_with is not None:
            try:
                policy.check(experiment_to_spec(f.verify_with, "analyst"), graph)
            except templates.SpecError as e:
                out.append(f"finding {i}: verify_with is not a valid experiment: {e}")
    scored = {s.service for s in report.service_scores}
    for svc in anchors:
        if svc not in scored:
            out.append(f"service_scores: missing service '{svc}'")
    for s in report.service_scores:
        a = anchors.get(s.service)
        if a is None:
            out.append(f"service_scores: '{s.service}' is not in the anchors table")
            continue
        for dim, anchor in a["dimensions"].items():
            if anchor is not None and abs(getattr(s, dim) - anchor) > cfg.SCORE_TOLERANCE:
                out.append(f"{s.service}.{dim}: {getattr(s, dim)} is more than {cfg.SCORE_TOLERANCE} from the "
                           f"anchor {anchor}")
    return out


def verdicts(report: FindingsReport | None, anchors: dict[str, dict]) -> list[ServiceVerdict]:
    """Final per-service scores: the AI's dimensions (clamped to anchor +- tolerance), combined by code."""
    by_name = {s.service: s for s in report.service_scores} if report else {}
    out = []
    for svc, a in anchors.items():
        ai = by_name.get(svc)
        dims: dict[str, float] = {}
        for dim, anchor in a["dimensions"].items():
            v = float(getattr(ai, dim)) if ai else anchor
            if v is None:
                continue
            if anchor is not None:
                v = min(max(v, anchor - cfg.SCORE_TOLERANCE), anchor + cfg.SCORE_TOLERANCE)
            dims[dim] = round(v, 1)
        score = scoring.combine(dims)
        out.append(ServiceVerdict(service=svc, tested=a["tested"], dimensions=dims, anchors=a["dimensions"],
                                  score=score, anchor_score=a["score"], grade=scoring.grade(score),
                                  weight=ai.criticality_weight if ai else a["criticality"],
                                  justification=ai.justification if ai else ""))
    return sorted(out, key=lambda v: v.score)


async def analyze(results: list[RunResult], graph: Graph, baseline: Baseline, scope: str,
                  use_ai: bool = True) -> AnalysisRecord:
    anchors = scoring.service_anchors(results, graph)
    run_ids = {r.run_id for r in results}
    report: FindingsReport | None = None
    transcripts: list[str] = []
    guard: dict = {"attempts": 0, "problems": [], "clamped": []}
    error = None
    if use_ai:
        ctx = tools.ToolContext(graph=graph, baseline=baseline, runs={r.run_id: r for r in results})
        agent = build_agent()
        prompt = analyst_input(results, graph, baseline, anchors)
        try:
            for attempt in (1, 2):
                guard["attempts"] = attempt
                out, path = await client.run_agent(agent, prompt, name="analyst", context=ctx)
                transcripts.append(path)
                report = out
                found = problems(report, graph, run_ids, anchors)
                guard["problems"] = found
                if not found:
                    break
                prompt = (analyst_input(results, graph, baseline, anchors)
                          + "\n\nYour previous report failed these checks - fix them and answer again:\n- "
                          + "\n- ".join(found))
        except client.AIError as e:
            error = str(e)
    services = verdicts(report, anchors)
    if report:
        guard["clamped"] = [p for p in guard["problems"] if "from the anchor" in p]
    tested = [v for v in services if v.tested]
    checks = [r.prediction_check.verdict for r in results if r.prediction_check]
    overall = scoring.overall([(v.score, v.weight) for v in tested])
    record = AnalysisRecord(
        report_id=f"{datetime.now():%Y%m%d-%H%M%S}-report", created_at=datetime.now(timezone.utc).isoformat(
            timespec="seconds"), scope=scope, run_ids=sorted(run_ids), model=client.model() if report else None,
        score_version=scoring.SCORE_VERSION, overall_score=overall, overall_anchor=scoring.anchor_overall(anchors),
        grade=scoring.grade(overall), services=services, coverage=scoring.coverage(results),
        predictions={v: checks.count(v) for v in ("confirmed", "partly", "refuted")},
        ai=report.model_dump(mode="json") if report else None, guardrail=guard, transcripts=transcripts)
    if error:
        record.guardrail["error"] = error
    return record


def save(record: AnalysisRecord) -> str:
    d = cfg.REPORTS_DIR / record.report_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "analysis.json").write_text(record.model_dump_json(indent=2))
    return str(d / "analysis.json")


def load(report_id: str | None) -> AnalysisRecord:
    dirs = sorted(p for p in cfg.REPORTS_DIR.glob("*") if (p / "analysis.json").exists()) \
        if cfg.REPORTS_DIR.exists() else []
    match = dirs[-1:] if not report_id else [d for d in dirs if report_id in d.name]
    if not match:
        raise FileNotFoundError(f"no analysis matching '{report_id or 'latest'}' in {cfg.REPORTS_DIR}")
    return AnalysisRecord.model_validate_json((match[-1] / "analysis.json").read_text())
