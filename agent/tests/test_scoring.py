from conftest import make_result

from chaos_agent import scoring
from chaos_agent.models import Prediction


def test_affected_flows_need_errors_or_real_slowdown():
    r = make_result(flows={"home": (0.0, 110), "product": (0.0, 700), "add_to_cart": (0.10, 100),
                           "view_cart": (0.0, 140)})
    # home: noise; product: 7x and +600 ms; add_to_cart: 10 % errors; view_cart: 1.4x only
    assert r.affected_flows == ["product", "add_to_cart"]
    assert scoring.slowdown_factor(r) == 7.0


def test_bands():
    assert scoring.error_band(0.01) == "none" and scoring.error_band(0.1) == "low" and scoring.error_band(0.6) == "high"
    assert scoring.slowdown_band(1.5) == "none" and scoring.slowdown_band(7) == "moderate"
    assert scoring.slowdown_band(70) == "severe"


def test_prediction_verdicts():
    r = make_result(flows={"product": (0.0, 700), "view_cart": (0.0, 700)})
    good = Prediction(affected_flows=["product", "view_cart"], error_band="none", slowdown_band="moderate",
                      outcome="COMPLETED")
    assert scoring.check_prediction(good, r).verdict == "confirmed"
    half = Prediction(affected_flows=["product"], error_band="high", slowdown_band="severe", outcome="COMPLETED")
    pc = scoring.check_prediction(half, r)
    assert pc.verdict == "partly" and pc.matched == 2          # flows (Jaccard 0.5) + outcome
    wrong = Prediction(affected_flows=["checkout"], error_band="high", slowdown_band="none", outcome="ABORTED")
    assert scoring.check_prediction(wrong, r).verdict == "refuted"


def test_run_scores(graph):
    node = graph.nodes["productcatalogservice"]
    calm = make_result()
    assert scoring.run_scores(calm, node) == {"availability": 100, "latency": 100, "recovery": 100,
                                              "containment": 100}
    bad = make_result(status="ABORTED", abort_reason="guard: error rate 60% > 20%", peak_error_rate=0.6,
                      impact_error_rate=0.3, recovery_s=30, flows={f: (0.6, 300) for f in node.reach_flows},
                      breaches=["blast radius: non-target pod x is NotReady"])
    s = scoring.run_scores(bad, node)
    assert s["availability"] == 25.0                          # 100 - (150*0.3 + 50*0.6)
    assert s["recovery"] == 75.0                              # 30 s
    assert s["containment"] == 0.0                            # every reachable flow hit, guard abort, blast radius
    assert scoring.run_scores(make_result(status="NOT_RECOVERED", flows={"home": (0.5, 100)}), node)["recovery"] == 0


def test_posture_and_combine(graph):
    assert scoring.posture_score(graph.nodes["paymentservice"]) == 100   # 2 replicas, probes, limits
    assert scoring.posture_score(graph.nodes["redis-cart"]) == 30        # 1 replica, no probes
    assert scoring.combine({"availability": 100, "latency": 100, "recovery": 100, "containment": 100,
                            "posture": 0}) == 90.0
    assert scoring.combine({"posture": 60, "availability": None}) == 60.0
    assert scoring.grade(90) == "A" and scoring.grade(59.9) == "F"


def test_service_anchors_take_the_worst_run(graph):
    runs = [make_result(run_id="a", flows={"product": (0.0, 300)}),
            make_result(run_id="b", flows={"product": (0.0, 3000)})]
    anchors = scoring.service_anchors(runs, graph)
    pc = anchors["productcatalogservice"]
    assert pc["tested"] and pc["runs"] == ["a", "b"]
    assert pc["dimensions"]["latency"] == scoring.run_scores(runs[1], graph.nodes["productcatalogservice"])["latency"]
    assert not anchors["cartservice"]["tested"] and anchors["cartservice"]["dimensions"]["availability"] is None
    assert "loadgenerator" not in anchors
    assert scoring.anchor_overall(anchors) == pc["score"]               # only tested services count
    assert scoring.coverage(runs) == {"experiments": 2, "types": ["network_delay"], "failure_classes": ["slowdown"]}
