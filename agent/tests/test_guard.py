from chaos_agent.guard import Guard, blast_radius_issues, evaluate, precheck_ok, within_tolerance
from chaos_agent.models import FlowStats, Thresholds
from chaos_agent.probe import Sample, percentile, stats

TH = Thresholds(max_error_rate=0.2, max_p95_ms=1500)
NOW = 1000.0


def samples(n, latency_ms=50, errors=0, end=NOW):
    out = []
    for i in range(n):
        e = end - i * 0.2
        out.append(Sample(e - latency_ms / 1000, e, "home", 200 if i >= errors else 500, i >= errors))
    return out


def pod(name, app, ready=True, terminating=False):
    md = {"name": name, "labels": {"app": app}}
    if terminating:
        md["deletionTimestamp"] = "2026-10-10T00:00:00Z"
    return {"metadata": md, "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def test_percentile():
    assert percentile([], 95) is None
    assert percentile([10], 95) == 10
    assert percentile(list(range(1, 101)), 50) == 50.5
    assert stats(samples(10, errors=2)).error_rate == 0.2


def test_healthy_window_has_no_breach():
    assert evaluate(samples(10), NOW, NOW, TH).breaches == []


def test_error_rate_and_latency_breaches():
    c = evaluate(samples(10, errors=5), NOW, NOW, TH)
    assert any(b.startswith("error rate 50%") for b in c.breaches)
    c = evaluate(samples(10, latency_ms=2500), NOW, NOW, TH)
    assert any(b.startswith("p95 2500ms") for b in c.breaches)


def test_too_few_samples_not_judged_but_silence_is():
    assert evaluate(samples(2, errors=2), NOW, NOW, TH).breaches == []
    c = evaluate([], NOW, NOW - 4, TH)
    assert c.breaches and c.breaches[0].startswith("frontend unresponsive")


def test_guard_needs_consecutive_breaches():
    g = Guard(TH)
    bad = evaluate(samples(10, errors=5), NOW, NOW, TH)
    good = evaluate(samples(10), NOW, NOW, TH)
    assert g.check(bad) is None          # single spike
    assert g.check(good) is None         # streak reset
    assert g.check(bad) is None
    reason = g.check(bad)
    assert reason and "error rate" in reason
    assert len(g.seen) == 1              # same rule with different numbers is recorded once


def test_blast_radius():
    pods = [pod("cart-1", "cartservice", ready=False), pod("front-1", "frontend")]
    assert blast_radius_issues(pods, "cartservice") == []                    # target down is expected
    pods.append(pod("pay-1", "paymentservice", ready=False))
    assert blast_radius_issues(pods, "cartservice") == ["blast radius: non-target pod pay-1 is NotReady"]
    pods = [pod("cart-1", "cartservice", ready=False), pod("cart-2", "cartservice", ready=False)]
    assert "2 cartservice pods NotReady" in blast_radius_issues(pods, "cartservice")[0]
    pods = [pod("old", "cartservice", ready=False, terminating=True), pod("new", "cartservice", ready=False)]
    assert blast_radius_issues(pods, "cartservice") == []                    # killed pod still terminating


def test_recovery_tolerance(baseline):
    assert within_tolerance(FlowStats(count=10, error_rate=0.0, p95_ms=190), baseline)
    assert not within_tolerance(FlowStats(count=10, error_rate=0.0, p95_ms=400), baseline)
    assert not within_tolerance(FlowStats(count=10, error_rate=0.1, p95_ms=100), baseline)
    assert not within_tolerance(FlowStats(count=1, error_rate=0.0, p95_ms=100), baseline)


def test_precheck(baseline):
    assert precheck_ok(FlowStats(count=20, error_rate=0.0, p95_ms=120), baseline)[0]
    assert not precheck_ok(FlowStats(count=20, error_rate=0.3, p95_ms=120), baseline)[0]
    assert not precheck_ok(FlowStats(count=20, error_rate=0.0, p95_ms=900), baseline)[0]
