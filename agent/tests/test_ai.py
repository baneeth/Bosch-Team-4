"""Planner / adapter / analyst / report logic with the AI calls stubbed (no network, no API key)."""
import asyncio
import json

import pytest
from conftest import make_result

from chaos_agent import config as cfg
from chaos_agent import report as report_mod
from chaos_agent import scoring
from chaos_agent.llm import adapter, analyst, client, planner, tools
from chaos_agent.llm.schemas import (EXPERIMENT_MODELS, AdaptDecision, ExperimentPlan, FindingsReport,
                                     PlannedExperiment, to_spec)
from chaos_agent.models import ExperimentType, PlannedRun


@pytest.fixture(autouse=True)
def tmp_results(tmp_path, monkeypatch):
    for name, sub in [("RESULTS_DIR", ""), ("RUNS_DIR", "runs"), ("PLANS_DIR", "plans"), ("AI_DIR", "ai"),
                      ("REPORTS_DIR", "reports"), ("CAMPAIGNS_DIR", "campaigns")]:
        monkeypatch.setattr(cfg, name, tmp_path / sub if sub else tmp_path)


def exp(t, target, params=None, duration=45):
    return EXPERIMENT_MODELS[ExperimentType(t)](type=t, target=target, duration_s=duration, params=params or {})


def planned(t, target, params=None, duration=45, flows=("product",)):
    return PlannedExperiment(experiment=exp(t, target, params, duration), max_error_rate=None, max_p95_ms=None,
                             reason=f"{t} on {target}", hypothesis="h", learning_goal="q",
                             prediction={"affected_flows": list(flows), "error_band": "none",
                                         "slowdown_band": "moderate", "outcome": "COMPLETED"})


def fake_agent(outputs):
    """Replacement for client.run_agent returning the given outputs in order."""
    calls = []

    async def run_agent(agent, user_input, *, name, **kw):
        calls.append((name, user_input))
        out = outputs[len(calls) - 1]
        if isinstance(out, Exception):
            raise out
        return out, f"ai/fake-{name}-{len(calls)}.json"
    run_agent.calls = calls
    return run_agent


def _walk(node, key):
    if isinstance(node, dict):
        return key in node or any(_walk(v, key) for v in node.values())
    if isinstance(node, list):
        return any(_walk(v, key) for v in node)
    return False


@pytest.mark.parametrize("model", [ExperimentPlan, AdaptDecision, FindingsReport])
def test_output_schemas_are_strict_without_defaults(model):
    schema = client.strict_output(model)
    assert schema.is_strict_json_schema()
    js = schema.json_schema()
    assert not _walk(js, "default") and not _walk(js, "discriminator")
    assert json.dumps(js)                                   # serialisable


def test_experiment_union_carries_caps_and_converts():
    js = json.dumps(client.strict_output(ExperimentPlan).json_schema())
    assert '"loss_pct"' in js and '"maximum": 50' in js      # network_loss cap is in the schema the model fills
    spec = to_spec(planned("network_delay", "productcatalogservice", {"latency_ms": 100}), "planner")
    assert spec.type == ExperimentType.network_delay and spec.params == {"latency_ms": 100, "jitter_ms": 0}
    assert spec.origin == "planner" and spec.prediction.slowdown_band == "moderate"


def test_ai_plan_is_validated(graph, baseline, monkeypatch):
    plan = ExperimentPlan(strategy="s", skipped_ideas=[{"idea": "kill redis", "why_not": "data loss"}], experiments=[
        planned("network_delay", "productcatalogservice", {"latency_ms": 100}),
        planned("network_delay", "productcatalogservice", {"latency_ms": 100, "jitter_ms": 0}, 30),  # duplicate
        planned("pod_failure", "frontend", duration=30),                                             # policy
        planned("pod_failure", "cartservice", duration=30),
        planned("network_loss", "cartservice", {"loss_pct": 20}),                                     # over budget
    ])
    monkeypatch.setattr(client, "run_agent", fake_agent([plan]))
    rec = asyncio.run(planner.make_plan(graph, baseline, {}, max_experiments=2))
    assert rec.source == "ai" and rec.transcript == "ai/fake-planner-1.json"
    assert [(r.rank, r.spec.type.value, r.spec.target) for r in rec.experiments] == [
        (1, "network_delay", "productcatalogservice"), (2, "pod_failure", "cartservice")]
    errors = [r["error"] for r in rec.rejected]
    assert len(errors) == 3 and "duplicate" in errors[0] and "total outage" in errors[1] and "budget" in errors[2]
    assert rec.coverage == ["errors", "slowdown"] and rec.skipped_ideas[0]["idea"] == "kill redis"
    planner.save(rec)
    assert planner.load(None).plan_id == rec.plan_id


def test_planner_falls_back_when_ai_fails(graph, baseline, monkeypatch):
    monkeypatch.setattr(client, "run_agent", fake_agent([client.AIError("no key")]))
    rec = asyncio.run(planner.make_plan(graph, baseline, {}, max_experiments=6))
    assert rec.source == "fallback" and "no key" in rec.note
    assert len(rec.experiments) >= 4 and len(rec.coverage) >= 3
    assert all(r.spec.origin == "fallback" and r.spec.prediction for r in rec.experiments)
    assert all(r.spec.target != "frontend" or r.spec.type == ExperimentType.traffic_spike for r in rec.experiments)


def test_planner_input_has_what_the_ai_needs(graph, baseline):
    data = json.loads(planner.planner_input(graph, baseline, {"r": make_result(run_id="r")}, 5, None))
    assert {"dependency_map", "baseline", "experiment_catalog", "safety_guard", "ai_policy", "past_runs"} <= set(data)
    assert any(s["service"] == "productcatalogservice" and s["reach"] == 1.0 for s in data["dependency_map"])
    assert {c["type"] for c in data["experiment_catalog"]} == {t.value for t in ExperimentType}


def test_check_spec_tool_logic(graph):
    from chaos_agent.models import ExperimentSpec
    ok = ExperimentSpec(type="network_delay", target="cartservice", params={"latency_ms": 50}, duration_s=30)
    assert tools.check_spec(ok, graph).startswith("OK")
    bad = ExperimentSpec(type="pod_kill", target="frontend", duration_s=30)
    assert "total outage" in tools.check_spec(bad, graph)


def test_adapter_apply(graph):
    queue = [PlannedRun(rank=2, spec=to_spec(planned("network_delay", "productcatalogservice",
                                                     {"latency_ms": 300}), "planner"))]
    stop = AdaptDecision(action="stop", reason="enough", experiment=None)
    assert adapter.apply(stop, queue, graph, set(), 2) == (True, "campaign stopped by the adapter", True)
    modify = AdaptDecision(action="modify_next", reason="300 ms would trip the guard",
                           experiment=planned("network_delay", "productcatalogservice", {"latency_ms": 80}))
    applied, detail, _ = adapter.apply(modify, queue, graph, set(), 2)
    assert applied and queue[0].spec.params["latency_ms"] == 80 and queue[0].spec.origin == "adapter"
    follow = AdaptDecision(action="insert_followup", reason="cart broke",
                           experiment=planned("pod_failure", "redis-cart", duration=30))
    assert adapter.apply(follow, queue, graph, set(), 0)[0] is False          # no follow-ups left
    assert adapter.apply(follow, queue, graph, set(), 1)[0] is True and queue[0].spec.target == "redis-cart"
    unsafe = AdaptDecision(action="insert_followup", reason="x", experiment=planned("pod_kill", "frontend"))
    assert "total outage" in adapter.apply(unsafe, queue, graph, set(), 1)[1]
    from chaos_agent import policy
    done = {policy.key(queue[0].spec)}
    assert "already ran" in adapter.apply(follow, queue, graph, done, 1)[1]


def _report(score_shift=0, run_id="r1", fix_target="productcatalogservice"):
    anchors_like = {"availability": 100, "latency": 70, "recovery": 100, "containment": 60, "posture": 30}
    services = [{"service": s, **{k: max(0, min(100, v + score_shift)) for k, v in anchors_like.items()},
                 "criticality_weight": 2.0, "justification": "j"}
                for s in ("frontend", "checkoutservice", "cartservice", "productcatalogservice", "paymentservice",
                          "redis-cart")]
    return FindingsReport(
        summary="sum", score_explanation="weights", planner_review="ok", limitations=["single VM"],
        service_scores=services,
        findings=[{"title": "Latency amplification", "severity": "high", "services": ["productcatalogservice"],
                   "evidence_runs": [run_id], "evidence": "p95 100 -> 700 ms", "root_cause": "sequential calls",
                   "fix": {"action": "code_change", "target": fix_target, "change": "deadline + parallel calls"},
                   "fix_rationale": "r", "expected_improvement": "e",
                   "verify_with": exp("network_delay", "productcatalogservice", {"latency_ms": 100}).model_dump()},
                  {"title": "Single replica", "severity": "medium", "services": ["cartservice"],
                   "evidence_runs": [run_id], "evidence": "x", "root_cause": "1 replica",
                   "fix": {"action": "scale_replicas", "target": "cartservice", "replicas": 2},
                   "fix_rationale": "r", "expected_improvement": "e", "verify_with": None}])


def test_analyst_guardrail(graph):
    r = make_result(run_id="r1", flows={"product": (0.0, 700)})
    anchors = scoring.service_anchors([r], graph)
    good = _report()
    # make the AI scores match the anchors exactly -> no problems
    for s in good.service_scores:
        for dim, v in anchors[s.service]["dimensions"].items():
            if v is not None:
                setattr(s, dim, int(v))
    assert analyst.problems(good, graph, {"r1"}, anchors) == []
    bad = _report(run_id="nope", fix_target="redis-cart")
    bad.findings[1].fix.target = "redis-cart"
    bad.findings.reverse()                                          # medium before high
    found = analyst.problems(bad, graph, {"r1"}, anchors)
    text = "\n".join(found)
    assert "more severe than the one before" in text and "unknown run id 'nope'" in text
    assert "scale_replicas on stateful redis-cart" in text
    assert "from the anchor" in text                                # productcatalogservice latency 70 vs anchor


def test_analyze_retries_then_clamps(graph, baseline, monkeypatch):
    r = make_result(run_id="r1", flows={"product": (0.0, 700)})
    bad = _report(score_shift=-40)                                  # every tested score far below its anchor
    monkeypatch.setattr(client, "run_agent", fake_agent([bad, bad]))
    rec = asyncio.run(analyst.analyze([r], graph, baseline, "all"))
    assert rec.guardrail["attempts"] == 2 and rec.guardrail["problems"]
    pc = next(s for s in rec.services if s.service == "productcatalogservice")
    anchor = scoring.service_anchors([r], graph)["productcatalogservice"]["dimensions"]
    assert pc.dimensions["availability"] == anchor["availability"] - cfg.SCORE_TOLERANCE   # clamped
    assert rec.overall_score == pc.score                            # only the tested service counts
    assert rec.ai["findings"][0]["title"] == "Latency amplification"


def test_analyze_without_ai_uses_anchors(graph, baseline):
    r = make_result(run_id="r1", flows={"product": (0.0, 700)})
    rec = asyncio.run(analyst.analyze([r], graph, baseline, "all", use_ai=False))
    assert rec.ai is None and rec.overall_score == rec.overall_anchor


def test_report_renders_every_section(graph, baseline, monkeypatch):
    from chaos_agent.models import Prediction
    r = make_result(run_id="r1", flows={"product": (0.0, 700)},
                    prediction=Prediction(affected_flows=["product"], slowdown_band="moderate"))
    r.prediction_check = scoring.check_prediction(r.spec.prediction, r)
    monkeypatch.setattr(client, "run_agent", fake_agent([_report(), _report()]))
    rec = asyncio.run(analyst.analyze([r], graph, baseline, "all"))
    plan = planner.fallback_plan(graph, 4)
    md = report_mod.render(rec, [r], graph, baseline, plan, None)
    for heading in ("## 1. Summary", "## 2. Findings", "## 3. Resilience score", "## 4. Experiments",
                    "## 5. How the AI decided", "## 6. Safety record", "## 7. Dependency map", "## 8. Method"):
        assert heading in md
    assert "[HIGH] Latency amplification" in md and "kubectl -n boutique scale deployment/cartservice --replicas=2" in md
    assert "chaos-agent run --type network_delay --target productcatalogservice -p latency_ms=100" in md
    assert "Planner predictions: 1 confirmed" in md


def test_report_without_ai_says_so(graph, baseline):
    r = make_result(run_id="r1", flows={"product": (0.0, 700)})
    rec = asyncio.run(analyst.analyze([r], graph, baseline, "all", use_ai=False))
    md = report_mod.render(rec, [r], graph, baseline, planner.fallback_plan(graph, 2), None)
    assert "code anchors only (AI analyst unavailable)" in md and "Score (anchors)" in md
    assert "given by the AI analyst" not in md and "Rejected plan candidates" in md


def test_reasoning_effort_per_agent(monkeypatch):
    monkeypatch.setenv("OPENAI_EFFORT_ADAPTER", "low")
    assert client.effort("adapter") == "low"
    monkeypatch.delenv("OPENAI_EFFORT_PLANNER", raising=False)
    assert client.effort("planner") == cfg.DEFAULT_EFFORT["planner"]
    assert planner.build_agent().model_settings.reasoning.effort == cfg.DEFAULT_EFFORT["planner"]


def test_adapter_input_tells_the_shop_is_healthy():
    data = json.loads(adapter.adapter_input("s", [], [], make_result(run_id="r"), 2, "healthy: checked by code"))
    assert data["shop_now"] == "healthy: checked by code" and data["last_run"]["run_id"] == "r"
    assert "not a reason to stop" in adapter.INSTRUCTIONS
