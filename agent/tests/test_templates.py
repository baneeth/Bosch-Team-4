import pytest

from chaos_agent import config as cfg
from chaos_agent import templates
from chaos_agent.models import ExperimentSpec, ExperimentType


def spec(**kw):
    base = {"type": ExperimentType.network_delay, "target": "productcatalogservice", "duration_s": 60}
    return ExperimentSpec(**{**base, **kw})


def test_network_delay_manifest(graph):
    s = spec(params={"latency_ms": 300})
    m = templates.render(s, templates.validate(s, graph), "20261010-120000-network_delay-productcatalogservice")
    assert m["kind"] == "NetworkChaos"
    assert m["metadata"]["namespace"] == "boutique"
    assert m["metadata"]["labels"][cfg.MANAGED_BY_LABEL] == "chaos-agent"
    assert m["metadata"]["name"] == "ca-20261010-120000-network-delay-productcatalogservice"
    body = m["spec"]
    assert body["mode"] == "one"
    assert body["selector"] == {"namespaces": ["boutique"], "labelSelectors": {"app": "productcatalogservice"}}
    assert body["duration"] == "60s"
    assert body["action"] == "delay" and body["delay"]["latency"] == "300ms"


def test_pod_kill_and_cpu_stress_manifests(graph):
    s = spec(type=ExperimentType.pod_kill, target="cartservice", duration_s=30)
    m = templates.render(s, templates.validate(s, graph), "r1")
    assert m["kind"] == "PodChaos" and m["spec"]["action"] == "pod-kill" and m["spec"]["duration"] == "30s"
    s = spec(type=ExperimentType.cpu_stress, target="cartservice", params={"workers": 2, "load": 80})
    m = templates.render(s, templates.validate(s, graph), "r2")
    assert m["kind"] == "StressChaos" and m["spec"]["stressors"] == {"cpu": {"workers": 2, "load": 80}}


@pytest.mark.parametrize("kw, msg", [
    ({"target": "loadgenerator"}, "not a valid chaos target"),
    ({"target": "shoppingassistantservice"}, "unknown target"),
    ({"target": "prometheus"}, "unknown target"),
    ({"duration_s": 600}, "duration_s"),
    ({"duration_s": 1}, "duration_s"),
    ({"params": {"latency_ms": 10000}}, "latency_ms"),
    ({"params": {"rm_rf": 1}}, "Extra inputs"),
    ({"max_error_rate": 0.9}, "max_error_rate"),
    ({"max_p95_ms": 60000}, "max_p95_ms"),
])
def test_hard_caps_reject(graph, kw, msg):
    with pytest.raises(templates.SpecError, match=msg):
        templates.validate(spec(**kw), graph)


def test_cpu_workers_capped(graph):
    with pytest.raises(templates.SpecError, match="workers"):
        templates.validate(spec(type=ExperimentType.cpu_stress, params={"workers": 8}), graph)


def test_thresholds(baseline):
    th = templates.thresholds(spec(), baseline)          # baseline p95 100 ms -> floor 1500 ms wins
    assert th.max_error_rate == cfg.GUARD_ERROR_RATE and th.max_p95_ms == cfg.GUARD_P95_FLOOR_MS
    th = templates.thresholds(spec(max_p95_ms=300, max_error_rate=0.05), baseline)
    assert th.max_p95_ms == 300 and th.max_error_rate == 0.05


def test_catalog_lists_every_type():
    types = {t["type"] for t in templates.catalog()}
    assert types == {t.value for t in ExperimentType}


def test_new_fault_manifests(graph):
    s = spec(type=ExperimentType.pod_failure, target="cartservice", duration_s=30)
    m = templates.render(s, templates.validate(s, graph), "r3")
    assert m["kind"] == "PodChaos" and m["spec"]["action"] == "pod-failure" and m["spec"]["duration"] == "30s"
    s = spec(type=ExperimentType.network_loss, params={"loss_pct": 25})
    m = templates.render(s, templates.validate(s, graph), "r4")
    assert m["kind"] == "NetworkChaos" and m["spec"]["action"] == "loss"
    assert m["spec"]["loss"] == {"loss": "25", "correlation": "0"} and m["spec"]["mode"] == "one"
    s = spec(type=ExperimentType.memory_stress, target="cartservice", params={"size_mb": 100})
    m = templates.render(s, templates.validate(s, graph), "r5")
    assert m["kind"] == "StressChaos" and m["spec"]["stressors"] == {"memory": {"workers": 1, "size": "100MB"}}


@pytest.mark.parametrize("t, params, msg", [
    (ExperimentType.network_loss, {"loss_pct": 80}, "loss_pct"),
    (ExperimentType.memory_stress, {"size_mb": 4096}, "size_mb"),
    (ExperimentType.pod_failure, {"anything": 1}, "Extra inputs"),
])
def test_new_fault_caps(graph, t, params, msg):
    with pytest.raises(templates.SpecError, match=msg):
        templates.validate(spec(type=t, target="cartservice", params=params), graph)
