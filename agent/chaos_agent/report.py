"""Findings report (markdown). Every number comes from code (results, scoring); AI-written text is labelled."""
from __future__ import annotations

from pydantic import TypeAdapter

from . import config as cfg
from . import fixes, scoring
from .graph import to_mermaid
from .models import AnalysisRecord, Baseline, CampaignRecord, Graph, PlanRecord, RunResult, RunStatus

AI = "*(AI analyst)*"
_fix_adapter = TypeAdapter(fixes.Fix)


def _ms(v) -> str:
    return "-" if v is None else f"{v:.0f} ms"


def _pct(v) -> str:
    return "-" if v is None else f"{v:.1%}"


def _cell(text) -> str:
    return str(text if text is not None else "-").replace("|", "\\|").replace("\n", " ")


def render(a: AnalysisRecord, runs: list[RunResult], graph: Graph, baseline: Baseline,
           plan: PlanRecord | None = None, campaign: CampaignRecord | None = None) -> str:
    ai = a.ai or {}
    L: list[str] = []
    add = L.append
    add("# Chaos Engineering Findings Report — Online Boutique\n")
    add(f"Generated {a.created_at} · scope: `{a.scope}` · {len(runs)} experiments · analyst model: "
        f"{a.model or 'none (AI unavailable — code anchors only)'} · score version {a.score_version}\n")

    # 1. Summary ------------------------------------------------------------------------------------------
    add("## 1. Summary\n")
    who = ("given by the AI analyst against a fixed rubric, combined by code" if a.ai
           else "code anchors only (AI analyst unavailable)")
    add(f"- **Resilience score: {a.overall_score:.0f} / 100 (grade {a.grade})** — {who}; code anchor: "
        f"{a.overall_anchor:.0f}.")
    cov = a.coverage
    add(f"- Experiments: {cov.get('experiments', 0)} · types: {', '.join(cov.get('types', []))} · failure classes: "
        f"{', '.join(cov.get('failure_classes', []))}.")
    if any(a.predictions.values()):
        add(f"- Planner predictions: {a.predictions.get('confirmed', 0)} confirmed, {a.predictions.get('partly', 0)} "
            f"partly, {a.predictions.get('refuted', 0)} refuted.")
    aborted = [r for r in runs if r.status == RunStatus.aborted]
    add(f"- Safety: {len(aborted)} experiment(s) stopped automatically or manually; baseline p95 "
        f"{_ms(baseline.overall.p95_ms)}, {baseline.overall.error_rate:.1%} errors.")
    if ai.get("summary"):
        add(f"\n{AI} {ai['summary']}")
    add("")

    # 2. Findings -----------------------------------------------------------------------------------------
    add("## 2. Findings, ranked by severity\n")
    findings = ai.get("findings") or []
    if not findings:
        add("_No AI findings (analyst unavailable). See the scores and experiments below._\n")
    for i, f in enumerate(findings, 1):
        add(f"### F{i}. [{f['severity'].upper()}] {f['title']}\n")
        add(f"- **Services:** {', '.join(f['services'])}")
        add(f"- **Evidence:** {', '.join(f'`{r}`' for r in f['evidence_runs'])} — {f['evidence']}")
        add(f"- **Root cause:** {f['root_cause']}")
        fix = _fix_adapter.validate_python(f["fix"])
        add(f"- **Fix (`{fix.action}` on {fix.target}):** {f['fix_rationale']}")
        add(f"\n```\n{fixes.render(fix)}\n```")
        add(f"- **Expected improvement:** {f['expected_improvement']}")
        v = f.get("verify_with")
        if v:
            add(f"- **Verify by re-running:** `chaos-agent run --type {v['type']} --target {v['target']}"
                + "".join(f" -p {k}={val}" for k, val in v["params"].items())
                + f" --duration {v['duration_s']}`")
        add("")
    if findings:
        add(f"_Findings text: {AI}. Numbers: measured by the probe / Prometheus._\n")

    # 3. Scores -------------------------------------------------------------------------------------------
    add("## 3. Resilience score per service (worst first)\n")
    src = "AI" if a.ai else "anchors"
    add(f"| Service | Score ({src}) | Code anchor | Grade | Availability | Latency | Recovery | Containment | Posture | "
        f"Weight | Tested | Justification ({src}) |")
    add("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for s in a.services:
        d = s.dimensions
        add(f"| {s.service} | **{s.score:.0f}** | {s.anchor_score:.0f} | {s.grade} | "
            + " | ".join(f"{d[k]:.0f}" if k in d else "-" for k in scoring.WEIGHTS)
            + f" | {s.weight:g} | {'yes' if s.tested else 'no'} | {_cell(s.justification)} |")
    add(f"\nRubric weights: " + ", ".join(f"{k} {w:.0%}" for k, w in scoring.WEIGHTS.items())
        + f". AI dimension scores are limited to ±{cfg.SCORE_TOLERANCE} of the code anchor; the overall score is the "
          "criticality-weighted mean of the tested services.\n")
    if ai.get("score_explanation"):
        add(f"{AI} {ai['score_explanation']}\n")

    # 4. Experiments --------------------------------------------------------------------------------------
    add("## 4. Experiments\n")
    add("| Run | Type | Target | Params | Origin | Status | Peak errors | Peak p95 | Worst slowdown | Affected flows | "
        "Recovery | Prediction |")
    add("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in runs:
        status = r.status.value + (f" ({_cell(r.abort_reason)[:60]})" if r.abort_reason else "")
        rec = f"{r.recovery_s:.0f} s" if r.recovery_s is not None else "-"
        if r.pod_ready_s is not None:
            rec += f" (pod Ready {r.pod_ready_s:.0f} s)"
        add(f"| `{r.run_id}` | {r.spec.type.value} | {r.spec.target} | {_cell(r.spec.params or '-')} | "
            f"{r.spec.origin} | {status} | {_pct(r.peak_error_rate)} | {_ms(r.peak_p95_ms)} | "
            f"{scoring.slowdown_factor(r):.1f}x | {', '.join(r.affected_flows) or 'none'} | {rec} | "
            f"{r.prediction_check.verdict if r.prediction_check else '-'} |")
    add("")

    # 5. How the AI decided -------------------------------------------------------------------------------
    add("## 5. How the AI decided\n")
    if plan:
        add(f"**Plan `{plan.plan_id}`** ({plan.source}{', model ' + plan.model if plan.model else ''}). "
            f"Strategy: {plan.strategy}\n")
        add("| Rank | Experiment | Reason | Prediction |")
        add("|---|---|---|---|")
        for p in plan.experiments:
            s, pr = p.spec, p.spec.prediction
            pred = (f"flows {', '.join(pr.affected_flows) or 'none'}; errors {pr.error_band}; slowdown "
                    f"{pr.slowdown_band}; {pr.outcome}") if pr else "-"
            params = f" {_cell(s.params)}" if s.params else ""
            add(f"| {p.rank} | {s.type.value} on {s.target}{params} {s.duration_s}s | "
                f"{_cell(s.reason)} | {pred} |")
        if plan.rejected:
            label = "AI suggestions" if plan.source == "ai" else "plan candidates"
            add(f"\nRejected {label} (validation / AI policy / budget):\n")
            for rj in plan.rejected:
                add(f"- {rj['type']} on {rj['target']} {rj.get('params') or ''}: {rj['error']}")
        if plan.skipped_ideas:
            add("\nIdeas the planner considered and dropped:\n")
            for sk in plan.skipped_ideas:
                add(f"- {sk['idea']} — {sk['why_not']}")
        add("")
    if campaign and campaign.decisions:
        add(f"**Adapter decisions** (campaign `{campaign.campaign_id}`, {campaign.status}):\n")
        for d in campaign.decisions:
            add(f"- after `{d.after_run}`: **{d.action}** — {d.reason} _({d.detail or ''})_")
        add("")
    if ai.get("planner_review"):
        add(f"{AI} {ai['planner_review']}\n")

    # 6. Safety -------------------------------------------------------------------------------------------
    add("## 6. Safety record\n")
    add(f"Guard (deterministic code, checked every {cfg.CHECK_INTERVAL_S:.0f} s): abort when the error rate > "
        f"{cfg.GUARD_ERROR_RATE:.0%} or p95 > max({cfg.GUARD_P95_BASELINE_FACTOR:g}× baseline, "
        f"{cfg.GUARD_P95_FLOOR_MS:.0f} ms) for {cfg.GUARD_CONSECUTIVE} checks in a row, the frontend is silent for "
        f"{cfg.GUARD_UNRESPONSIVE_S:.0f} s, or a non-target pod becomes NotReady. Every fault is validated against "
        "hard caps (and the AI policy for AI-proposed ones), dry-run by the Kubernetes API server, and carries a "
        "Chaos Mesh `duration` (auto-revert).\n")
    if aborted:
        add("| Run | Reason | Fault active | Kill switch |")
        add("|---|---|---|---|")
        for r in aborted:
            ks = next((n for n in r.notes if n.startswith("kill switch took")), "-")
            add(f"| `{r.run_id}` | {_cell(r.abort_reason)} | {r.fault_duration_s} s | {ks} |")
        add("")

    # 7. Map ----------------------------------------------------------------------------------------------
    add("## 7. Dependency map\n")
    add("Red = high risk (single replica and critical), yellow = medium, green = low.\n")
    add(f"```mermaid\n{to_mermaid(graph)}```\n")
    add("| Service | Replicas | Risk | Reach (share of user flows) | Dependents |")
    add("|---|---|---|---|---|")
    for n in sorted((n for n in graph.nodes.values() if n.targetable), key=lambda n: (-n.reach, n.name)):
        add(f"| {n.name} | {n.replicas} | {n.risk} | {n.reach:.0%} ({', '.join(n.reach_flows) or '-'}) | "
            f"{', '.join(n.dependents) or '-'} |")
    add("")

    # 8. Method -------------------------------------------------------------------------------------------
    add("## 8. Method and limitations\n")
    add("- Users are simulated by a 4-user probe (home, product, add to cart, view cart, checkout) measured in "
        "rolling 5 s windows; resources come from Prometheus.")
    add("- Rubric: " + "; ".join(f"**{k}** — {v}" for k, v in scoring.RUBRIC.items()))
    for lim in ai.get("limitations") or []:
        add(f"- {AI} {lim}")
    if a.guardrail.get("problems"):
        add(f"- Analyst guardrail: {len(a.guardrail['problems'])} issue(s) left after "
            f"{a.guardrail.get('attempts')} attempt(s); out-of-range scores were clamped to the anchor ±"
            f"{cfg.SCORE_TOLERANCE}.")
    if a.guardrail.get("error"):
        add(f"- AI analyst error: {a.guardrail['error']}")
    return "\n".join(L) + "\n"
