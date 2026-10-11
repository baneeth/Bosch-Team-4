import pytest

from chaos_agent import policy, templates
from chaos_agent.models import ExperimentSpec, ExperimentType


def spec(**kw):
    base = {"type": ExperimentType.network_delay, "target": "productcatalogservice", "duration_s": 60}
    return ExperimentSpec(**{**base, **kw})


@pytest.mark.parametrize("kw, msg", [
    ({"type": ExperimentType.pod_failure, "target": "frontend", "duration_s": 30}, "total outage"),
    ({"type": ExperimentType.pod_kill, "target": "frontend"}, "total outage"),
    ({"type": ExperimentType.pod_failure, "target": "cartservice", "duration_s": 120}, "at most 60s"),
    ({"type": ExperimentType.traffic_spike, "target": "frontend", "params": {"users": 400}, "duration_s": 90},
     "at most 300 users"),
    ({"max_error_rate": 0.4}, "stricter"),
    ({"max_p95_ms": 3000}, "stricter"),
    ({"params": {"latency_ms": 99999}}, "latency_ms"),          # hard caps still apply
])
def test_ai_policy_rejects(graph, kw, msg):
    with pytest.raises(templates.SpecError, match=msg):
        policy.check(spec(**kw), graph)


def test_ai_policy_allows_and_dedups(graph):
    policy.check(spec(params={"latency_ms": 100}, max_p95_ms=800), graph)
    policy.check(spec(type=ExperimentType.pod_failure, target="cartservice", duration_s=30), graph)
    # defaults are filled in, so an explicit default equals an omitted one; duration doesn't matter
    assert policy.key(spec(params={"latency_ms": 300})) == policy.key(spec(params={"latency_ms": 300,
                                                                                    "jitter_ms": 0}, duration_s=30))
    assert policy.key(spec(params={"latency_ms": 100})) != policy.key(spec(params={"latency_ms": 300}))
