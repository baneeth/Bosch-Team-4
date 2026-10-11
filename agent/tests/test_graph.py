from chaos_agent.graph import parse_addr, to_mermaid


def test_parse_addr():
    assert parse_addr("productcatalogservice:3550") == ("productcatalogservice", 3550)
    assert parse_addr("cartservice.boutique.svc.cluster.local:7070") == ("cartservice", 7070)
    assert parse_addr("redis-cart") == ("redis-cart", None)
    assert parse_addr("") is None
    assert parse_addr("http://collector:4317") is None


def test_edges_from_env_vars(graph):
    assert set(graph.nodes["frontend"].depends_on) == {"productcatalogservice", "cartservice", "checkoutservice"}
    assert set(graph.nodes["checkoutservice"].depends_on) == {"productcatalogservice", "cartservice", "paymentservice"}
    assert graph.nodes["cartservice"].depends_on == ["redis-cart"]
    # non-_ADDR env vars are ignored
    assert not any(e.env_var == "ENABLE_PROFILER" for e in graph.edges)


def test_dangling_edge_is_flagged_not_fatal(graph):
    dangling = [e for e in graph.edges if e.dangling]
    assert [(e.source, e.target) for e in dangling] == [("frontend", "shoppingassistantservice")]
    node = graph.nodes["shoppingassistantservice"]
    assert not node.exists and not node.targetable
    assert any("shoppingassistantservice" in w for w in graph.warnings)


def test_callers_exclude_traffic_generator(graph):
    assert graph.nodes["frontend"].callers == []
    assert graph.nodes["loadgenerator"].role == "traffic"
    assert not graph.nodes["loadgenerator"].targetable
    assert sorted(graph.nodes["productcatalogservice"].callers) == ["checkoutservice", "frontend"]
    assert graph.nodes["productcatalogservice"].fan_in == 2


def test_checkout_path_is_transitive(graph):
    on_path = {n for n, node in graph.nodes.items() if node.on_checkout_path}
    assert on_path == {"checkoutservice", "productcatalogservice", "cartservice", "paymentservice", "redis-cart"}


def test_risk(graph):
    n = graph.nodes
    assert n["frontend"].risk == "high"                 # entrypoint, single replica
    assert n["productcatalogservice"].risk == "high"    # shared by 2 services, single replica
    assert n["redis-cart"].risk == "high"               # stateful, single replica
    assert n["paymentservice"].risk == "low"            # 2 replicas, probes, limits
    assert n["checkoutservice"].risk == "medium"        # single replica, fan-in 1


def test_mermaid(graph):
    m = to_mermaid(graph)
    assert m.startswith("flowchart LR")
    assert "redis_cart" in m and "redis-cart<br/>1 replica" in m
    assert "frontend -.-> shoppingassistantservice" in m
    assert "loadgenerator -.-> frontend" in m
    assert "frontend --> productcatalogservice" in m


def test_reach_and_dependents(graph):
    n = graph.nodes
    assert n["frontend"].reach == 1.0                       # every flow enters through it
    assert n["redis-cart"].reach == 1.0                     # cart is on every flow (incl. checkout, transitively)
    assert n["checkoutservice"].reach_flows == ["checkout"] and n["checkoutservice"].reach == 0.2
    assert n["paymentservice"].reach_flows == ["checkout"]  # only via checkoutservice
    assert n["redis-cart"].dependents == ["cartservice", "checkoutservice", "frontend"]
    assert n["loadgenerator"].reach == 0.0 and n["loadgenerator"].dependents == []
