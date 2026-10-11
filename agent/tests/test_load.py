import json

import pytest

from chaos_agent import templates
from chaos_agent.loadgen import read_summary
from chaos_agent.models import ExperimentSpec, ExperimentType
from chaos_agent.prom import app_of, split_series


def spike(**kw):
    base = {"type": ExperimentType.traffic_spike, "target": "frontend", "duration_s": 90,
            "params": {"users": 200, "ramp_s": 30}}
    return ExperimentSpec(**{**base, **kw})


def test_traffic_spike_validates(graph):
    p = templates.validate(spike(), graph)
    assert p.users == 200 and p.ramp_s == 30 and p.think_ms == 1000 and p.checkout_pct == 20


@pytest.mark.parametrize("kw, msg", [
    ({"target": "cartservice"}, "target must be 'frontend'"),
    ({"params": {"users": 5000}}, "users"),
    ({"params": {"users": 50, "ramp_s": 90}}, "ramp_s"),
    ({"params": {"users": 50, "think_ms": 10}}, "think_ms"),
])
def test_traffic_spike_caps(graph, kw, msg):
    with pytest.raises(templates.SpecError, match=msg):
        templates.validate(spike(**kw), graph)


def test_k6_script(graph):
    s = spike()
    p = templates.validate(s, graph)
    js = templates.render_k6(s, p, "run1", "http://10.0.0.1:80/", "/tmp/sum.json")
    assert 'const BASE = "http://10.0.0.1:80";' in js
    assert '{ duration: "30s", target: 200 }, { duration: "60s", target: 200 }' in js
    assert "const THINK = 1;" in js
    assert '"/tmp/sum.json"' in js and "__" not in js.replace("__VU", "")
    with pytest.raises(ValueError):
        templates.render(s, p, "run1")          # not a Chaos Mesh experiment


def test_users_at_ramp():
    p = templates.TrafficSpikeParams(users=200, ramp_s=40)
    assert templates.users_at(p, 0) == 0
    assert templates.users_at(p, 10) == 50
    assert templates.users_at(p, 40) == 200
    assert templates.users_at(p, 75) == 200


def test_app_of():
    apps = ["frontend", "cartservice", "redis-cart", "productcatalogservice"]
    assert app_of("frontend-7d9f8c6b5-x2x4k", apps) == "frontend"
    assert app_of("redis-cart-6899b65948-bjk6v", apps) == "redis-cart"
    assert app_of("prometheus-0", apps) is None


def test_split_series():
    series = [{"metric": {"pod": "frontend-abc-1"}, "values": [[100, "0.01"], [105, "0.02"], [110, "0.19"], [115, "NaN"]]}]
    out = split_series(series, ["frontend"], t_inject=108)
    assert out["frontend"] == ([0.01, 0.02], [0.19])


def test_read_summary(tmp_path):
    f = tmp_path / "sum.json"
    f.write_text(json.dumps({"metrics": {
        "http_reqs": {"values": {"count": 1340, "rate": 43.59}},
        "http_req_failed": {"values": {"rate": 0.0123}},
        "http_req_duration": {"values": {"p(95)": 91.404}},
        "vus_max": {"values": {"max": 200}}}}))
    s = read_summary(f, 200)
    assert (s.requests, s.rps, s.failed_rate, s.p95_ms, s.max_vus) == (1340, 43.6, 0.0123, 91.4, 200)
    assert read_summary(tmp_path / "missing.json", 10).requests is None
