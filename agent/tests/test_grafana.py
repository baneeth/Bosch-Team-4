from chaos_agent.grafana import QUERIES, dashboard


def test_dashboard_structure():
    d = dashboard()
    assert d["uid"] == "chaos-lab" and d["refresh"] == "5s"
    assert d["annotations"]["list"][0]["target"]["tags"] == ["chaos"]
    titles = [p["title"] for p in d["panels"]]
    assert "CPU throttling % (time spent waiting for CPU)" in titles
    assert len(titles) == 10
    for p in d["panels"]:
        assert p["targets"][0]["expr"]


def test_queries_scoped_to_boutique():
    for name, q in QUERIES.items():
        if name != "vm_cpu":
            assert 'namespace="boutique"' in q, name
    assert 'label_replace(' in QUERIES["cpu_pct_limit"]
