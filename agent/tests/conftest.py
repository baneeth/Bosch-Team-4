"""Fixtures: a trimmed-down Online Boutique in the Kubernetes API's JSON shape."""
import pytest

from chaos_agent.graph import build_graph
from chaos_agent.models import Baseline, FlowStats


def deployment(name, env=None, replicas=1, probes=True, limits=True):
    c = {"name": "server", "env": [{"name": k, "value": v} for k, v in (env or {}).items()]}
    if probes:
        c["readinessProbe"] = {"grpc": {"port": 8080}}
        c["livenessProbe"] = {"grpc": {"port": 8080}}
    if limits:
        c["resources"] = {"limits": {"cpu": "200m", "memory": "128Mi"}}
    return {"metadata": {"name": name},
            "spec": {"replicas": replicas, "template": {"metadata": {"labels": {"app": name}},
                                                        "spec": {"containers": [c]}}},
            "status": {"readyReplicas": replicas, "availableReplicas": replicas}}


def service(name, app=None):
    return {"metadata": {"name": name}, "spec": {"selector": {"app": app or name}}}


@pytest.fixture
def boutique():
    deps = [
        deployment("frontend", {"PRODUCT_CATALOG_SERVICE_ADDR": "productcatalogservice:3550",
                                "CART_SERVICE_ADDR": "cartservice:7070",
                                "CHECKOUT_SERVICE_ADDR": "checkoutservice:5050",
                                "SHOPPING_ASSISTANT_SERVICE_ADDR": "shoppingassistantservice:80",
                                "ENABLE_PROFILER": "0"}),
        deployment("checkoutservice", {"PRODUCT_CATALOG_SERVICE_ADDR": "productcatalogservice:3550",
                                       "CART_SERVICE_ADDR": "cartservice.boutique.svc.cluster.local:7070",
                                       "PAYMENT_SERVICE_ADDR": "paymentservice:50051"}),
        deployment("cartservice", {"REDIS_ADDR": "redis-cart:6379"}),
        deployment("productcatalogservice"),
        deployment("paymentservice", replicas=2),
        deployment("redis-cart", probes=False),
        deployment("loadgenerator", {"FRONTEND_ADDR": "frontend:80"}, probes=False),
    ]
    svcs = [service(d["metadata"]["name"]) for d in deps if d["metadata"]["name"] != "loadgenerator"]
    svcs.append(service("frontend-external", "frontend"))
    return deps, svcs


@pytest.fixture
def graph(boutique):
    return build_graph(*boutique)


@pytest.fixture
def baseline():
    s = FlowStats(count=100, errors=0, error_rate=0.0, p50_ms=40.0, p95_ms=100.0, p99_ms=150.0)
    return Baseline(generated_at="2026-10-10T00:00:00+00:00", duration_s=120, overall=s,
                    flows={"home": s, "product": s})


def make_result(run_id="20261010-120000-network_delay-productcatalogservice", type="network_delay",
                target="productcatalogservice", status="COMPLETED", abort_reason=None, peak_error_rate=0.0,
                impact_error_rate=0.0, recovery_s=0.0, flows=None, breaches=None, prediction=None, params=None):
    """A RunResult as the runner would save it. flows: {flow: (error_rate, p95_ms)} against a 100 ms baseline."""
    from chaos_agent import scoring
    from chaos_agent.models import (ExperimentSpec, FlowImpact, FlowStats, RunResult, RunStatus, Thresholds)
    base = FlowStats(count=100, error_rate=0.0, p50_ms=40.0, p95_ms=100.0)
    impact = {}
    for f, (err, p95) in (flows or {}).items():
        impact[f] = FlowImpact(baseline=base, impact=FlowStats(count=20, errors=int(err * 20), error_rate=err,
                                                               p95_ms=p95), p95_factor=round(p95 / 100.0, 2))
    r = RunResult(run_id=run_id, spec=ExperimentSpec(type=type, target=target, params=params or {}, duration_s=30,
                                                     prediction=prediction),
                  status=RunStatus(status), abort_reason=abort_reason,
                  thresholds=Thresholds(max_error_rate=0.2, max_p95_ms=1500), started_at="2026-10-10T12:00:00+00:00",
                  injected_at="2026-10-10T12:00:20+00:00", recovery_s=recovery_s, peak_error_rate=peak_error_rate,
                  impact_error_rate=impact_error_rate, guard_breaches=breaches or [], flow_impact=impact)
    r.affected_flows = scoring.affected_flows(r)
    return r
