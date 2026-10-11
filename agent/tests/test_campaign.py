"""Campaign loop with a fake runner / adapter / settle step (no cluster, no AI)."""
import asyncio

import pytest
from conftest import make_result

from chaos_agent import campaign as campaign_mod
from chaos_agent import config as cfg
from chaos_agent.llm.client import AIError
from chaos_agent.llm.schemas import AdaptDecision
from chaos_agent.models import ExperimentSpec, PlannedRun, PlanRecord
from test_ai import planned


@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CAMPAIGNS_DIR", tmp_path / "campaigns")
    monkeypatch.setattr(campaign_mod, "STOP_FILE", tmp_path / "campaign.stop")
    monkeypatch.setattr(campaign_mod, "LOCK_FILE", tmp_path / "campaign.lock")


def plan_of(*specs):
    return PlanRecord(plan_id="p1", created_at="t", source="ai", strategy="s",
                      experiments=[PlannedRun(rank=i, spec=s) for i, s in enumerate(specs, 1)])


def spec(t="network_delay", target="productcatalogservice", params=None, duration=30, origin="planner"):
    return ExperimentSpec(type=t, target=target, params={"latency_ms": 100} if params is None else params,
                          duration_s=duration, origin=origin)


class FakeRunner:
    def __init__(self, outcomes, ran):
        self.outcomes, self.ran = outcomes, ran

    async def run(self, s):
        self.ran.append((s.type.value, s.target, s.params))
        status, reason = self.outcomes.pop(0) if self.outcomes else ("COMPLETED", None)
        return make_result(run_id=f"run{len(self.ran)}", type=s.type.value, target=s.target, status=status,
                           abort_reason=reason, params=s.params)

    def request_abort(self, reason):
        pass


def make_campaign(graph, baseline, plan, outcomes=(), decisions=(), adapt=True, healthy=True):
    ran, outcomes, decisions = [], list(outcomes), list(decisions)
    calls = []

    async def adapter_fn(**kw):
        calls.append(kw["last"].run_id)
        d = decisions.pop(0) if decisions else AdaptDecision(action="continue", reason="fine", experiment=None)
        if isinstance(d, Exception):
            raise d
        return d, "ai/fake-adapter.json"

    async def settle():
        return 0.0, healthy

    c = campaign_mod.Campaign(graph, baseline, plan, adapt=adapt, runner_factory=lambda: FakeRunner(outcomes, ran),
                              adapter_fn=adapter_fn, settle_fn=settle)
    return c, ran, calls


def run(c):
    return asyncio.run(c.run())


def test_runs_the_plan_and_consults_the_adapter(graph, baseline):
    c, ran, calls = make_campaign(graph, baseline, plan_of(spec(), spec("pod_failure", "cartservice", {})))
    rec = run(c)
    assert rec.status == "completed" and len(ran) == 2 and calls == ["run1", "run2"]
    assert [e.status for e in rec.entries] == ["COMPLETED", "COMPLETED"]
    assert (cfg.CAMPAIGNS_DIR / rec.campaign_id / "campaign.json").exists()


def test_not_recovered_stops(graph, baseline):
    c, ran, _ = make_campaign(graph, baseline, plan_of(spec(), spec(params={"latency_ms": 50})),
                              outcomes=[("NOT_RECOVERED", None)])
    rec = run(c)
    assert len(ran) == 1 and "did not recover" in rec.status


def test_two_guard_aborts_on_same_target_stop(graph, baseline):
    abort = ("ABORTED", "guard: error rate 60% > 20%")
    c, ran, _ = make_campaign(graph, baseline, plan_of(spec(), spec(params={"latency_ms": 50}),
                                                       spec(params={"latency_ms": 20})),
                              outcomes=[abort, abort])
    rec = run(c)
    assert len(ran) == 2 and "2 runs in a row on productcatalogservice" in rec.status


def test_single_guard_abort_is_a_result_not_a_stop(graph, baseline):
    c, ran, _ = make_campaign(graph, baseline, plan_of(spec("pod_failure", "cartservice", {}), spec()),
                              outcomes=[("ABORTED", "guard: error rate 60% > 20%")])
    assert run(c).status == "completed" and len(ran) == 2


def test_manual_abort_stops(graph, baseline):
    c, ran, _ = make_campaign(graph, baseline, plan_of(spec(), spec(params={"latency_ms": 50})),
                              outcomes=[("ABORTED", "kill switch: chaos resource was deleted externally")])
    assert "manual abort" in run(c).status and len(ran) == 1


def test_adapter_followup_and_stop(graph, baseline):
    follow = AdaptDecision(action="insert_followup", reason="cart errors spread",
                           experiment=planned("pod_failure", "redis-cart", duration=30))
    stop = AdaptDecision(action="stop", reason="enough evidence", experiment=None)
    c, ran, _ = make_campaign(graph, baseline, plan_of(spec(), spec(params={"latency_ms": 50})),
                              decisions=[follow, stop])
    rec = run(c)
    assert [r[1] for r in ran] == ["productcatalogservice", "redis-cart"]
    assert rec.status.startswith("stopped: adapter: enough evidence")
    assert [d.action for d in rec.decisions] == ["insert_followup", "stop"] and rec.decisions[0].applied
    assert rec.entries[1].spec.origin == "adapter"


def test_adapter_failure_keeps_the_plan_going(graph, baseline):
    c, ran, _ = make_campaign(graph, baseline, plan_of(spec(), spec(params={"latency_ms": 50})),
                              decisions=[AIError("timeout")])
    rec = run(c)
    assert len(ran) == 2 and rec.decisions[0].reason.startswith("adapter unavailable")


def test_stop_file_and_policy_skip(graph, baseline):
    c, ran, _ = make_campaign(graph, baseline, plan_of(spec("pod_kill", "frontend", {}), spec()), adapt=False)
    rec = run(c)
    assert rec.skipped and "total outage" in rec.skipped[0]["error"] and len(ran) == 1
    campaign_mod.STOP_FILE.write_text("x")
    c, ran, _ = make_campaign(graph, baseline, plan_of(spec()), adapt=False)
    assert "chaos-agent abort" in run(c).status and ran == []


def test_teammate_experiment_stops(graph, baseline):
    c, ran, _ = make_campaign(graph, baseline, plan_of(spec(), spec(params={"latency_ms": 50})),
                              outcomes=[("PRECHECK_FAILED", "another experiment is active: PodChaos/x")])
    assert "another experiment is active" in run(c).status and len(ran) == 1


def test_lock(tmp_path):
    campaign_mod.acquire_lock()
    assert campaign_mod.request_stop_file() and campaign_mod.STOP_FILE.exists()
    campaign_mod.release_lock()
    assert not campaign_mod.LOCK_FILE.exists() and not campaign_mod.request_stop_file()


def test_unsettled_shop_stops_the_campaign(graph, baseline):
    c, ran, calls = make_campaign(graph, baseline, plan_of(spec(), spec(params={"latency_ms": 50})), healthy=False)
    rec = run(c)
    assert len(ran) == 1 and calls == [] and "not back within baseline tolerance" in rec.status


def test_adapter_gets_the_code_checked_health(graph, baseline):
    seen = {}

    async def adapter_fn(**kw):
        seen.update(kw)
        return AdaptDecision(action="continue", reason="ok", experiment=None), "t"
    c, _, _ = make_campaign(graph, baseline, plan_of(spec()))
    c.adapter_fn = adapter_fn
    run(c)
    assert seen["shop_now"].startswith("healthy: checked by code")
