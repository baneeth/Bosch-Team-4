"""Dependency-map extractor.

Online Boutique wires services together with `*_ADDR` env vars (e.g. PRODUCT_CATALOG_SERVICE_ADDR=
productcatalogservice:3550). Each one becomes an edge; each Deployment becomes a node annotated with the
facts the planner needs (replicas, probes, limits, fan-in/out, checkout path, risk).
"""
from __future__ import annotations

import re
from collections import deque
from datetime import datetime, timezone

from . import config as cfg
from .models import Edge, Graph, ServiceNode

_HOST_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")


def parse_addr(value: str) -> tuple[str, int | None] | None:
    """'productcatalogservice.boutique.svc.cluster.local:3550' -> ('productcatalogservice', 3550)."""
    value = (value or "").strip()
    if not value or "://" in value:
        return None
    host, _, port = value.partition(":")
    if not _HOST_RE.match(host):
        return None
    return host.split(".")[0], int(port) if port.isdigit() else None


def _container(dep: dict) -> dict:
    return dep["spec"]["template"]["spec"]["containers"][0]


def _resolve(host: str, services: dict[str, dict], deployments: dict[str, dict]) -> str | None:
    """Map a Service host name to the Deployment behind it (via the Service selector)."""
    svc = services.get(host)
    if svc:
        selector = (svc.get("spec") or {}).get("selector") or {}
        for name, dep in deployments.items():
            labels = dep["spec"]["template"]["metadata"].get("labels") or {}
            if selector and all(labels.get(k) == v for k, v in selector.items()):
                return name
    return host if host in deployments else None


def build_graph(deployments: list[dict], services: list[dict], namespace: str = cfg.APP_NAMESPACE) -> Graph:
    deps = {d["metadata"]["name"]: d for d in deployments}
    svcs = {s["metadata"]["name"]: s for s in services}
    nodes: dict[str, ServiceNode] = {}
    edges: list[Edge] = []
    warnings: list[str] = []

    for name, dep in sorted(deps.items()):
        c = _container(dep)
        limits = (c.get("resources") or {}).get("limits") or {}
        nodes[name] = ServiceNode(
            name=name,
            role="traffic" if name in cfg.TRAFFIC_WORKLOADS else "app",
            replicas=dep["spec"].get("replicas", 1),
            ready_replicas=(dep.get("status") or {}).get("readyReplicas") or 0,
            readiness_probe="readinessProbe" in c,
            liveness_probe="livenessProbe" in c,
            cpu_limit=limits.get("cpu"),
            memory_limit=limits.get("memory"),
            is_entrypoint=name == cfg.ENTRYPOINT,
            stateful=name in cfg.STATEFUL_WORKLOADS,
        )

    for name, dep in sorted(deps.items()):
        for env in _container(dep).get("env") or []:
            if not env.get("name", "").endswith("_ADDR"):
                continue
            parsed = parse_addr(env.get("value", ""))
            if not parsed:
                continue
            host, port = parsed
            target = _resolve(host, svcs, deps)
            if target is None:
                edges.append(Edge(source=name, target=host, port=port, env_var=env["name"], dangling=True))
                nodes.setdefault(host, ServiceNode(name=host, exists=False)).callers.append(name)
                warnings.append(f"{name} references {host}:{port} ({env['name']}) but no such Service/Deployment exists")
                continue
            edges.append(Edge(source=name, target=target, port=port, env_var=env["name"]))
            if target not in nodes[name].depends_on:
                nodes[name].depends_on.append(target)
            if nodes[name].role == "app" and name not in nodes[target].callers:
                nodes[target].callers.append(name)

    # Checkout path: the checkout service and everything it (transitively) calls.
    if cfg.CHECKOUT_ROOT in nodes:
        queue, seen = deque([cfg.CHECKOUT_ROOT]), {cfg.CHECKOUT_ROOT}
        while queue:
            for nxt in nodes[queue.popleft()].depends_on:
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        for n in seen:
            nodes[n].on_checkout_path = True

    for node in nodes.values():
        node.fan_in = len(node.callers)
        node.fan_out = len(node.depends_on)
        node.targetable = node.exists and node.role == "app"
        _assess_risk(node)
    _assess_reach(nodes)

    return Graph(namespace=namespace, generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 nodes=nodes, edges=edges, warnings=warnings)


def _assess_risk(node: ServiceNode) -> None:
    if not node.targetable:
        node.risk, node.risk_reasons = "low", []
        return
    reasons = []
    single = node.replicas < 2
    if single:
        reasons.append("single replica (no redundancy)")
    if node.is_entrypoint:
        reasons.append("entrypoint: every user request passes through it")
    if node.fan_in >= 2:
        reasons.append(f"shared dependency of {node.fan_in} services ({', '.join(sorted(node.callers))})")
    if node.stateful:
        reasons.append("stateful: holds cart data")
    if node.on_checkout_path:
        reasons.append("on the checkout (revenue) path")
    if not node.readiness_probe:
        reasons.append("no readiness probe")
    if not (node.cpu_limit and node.memory_limit):
        reasons.append("no CPU/memory limits")
    critical = node.is_entrypoint or node.fan_in >= 2 or node.stateful
    node.risk = "high" if single and critical else "medium" if single or not node.readiness_probe else "low"
    node.risk_reasons = reasons


def _closure(nodes: dict[str, ServiceNode], start: list[str], step) -> set[str]:
    """All nodes reachable from `start` following `step(node) -> names` (start included)."""
    seen = {s for s in start if s in nodes}
    queue = deque(seen)
    while queue:
        for nxt in step(nodes[queue.popleft()]):
            if nxt in nodes and nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def _assess_reach(nodes: dict[str, ServiceNode]) -> None:
    """Reach = share of user flows whose call chain includes the service; dependents = transitive callers."""
    flow_chains = {flow: _closure(nodes, calls, lambda n: n.depends_on) | {cfg.ENTRYPOINT}
                   for flow, calls in cfg.FLOW_CALLS.items()}
    for node in nodes.values():
        if not node.targetable:
            continue
        node.dependents = sorted(_closure(nodes, [node.name], lambda n: n.callers) - {node.name})
        node.reach_flows = [f for f, chain in flow_chains.items() if node.name in chain]
        node.reach = round(len(node.reach_flows) / len(flow_chains), 2) if flow_chains else 0.0


def to_mermaid(graph: Graph) -> str:
    """Mermaid flowchart coloured by risk (paste into README / GitHub renders it)."""
    lines = ["flowchart LR"]
    for n in graph.nodes.values():
        if not n.exists:
            label, cls = f"{n.name}<br/>(missing)", "missing"
        elif n.role == "traffic":
            label, cls = f"{n.name}<br/>(traffic)", "traffic"
        else:
            label, cls = f"{n.name}<br/>{n.replicas} replica{'s' if n.replicas != 1 else ''}", n.risk
        lines.append(f'  {_mid(n.name)}["{label}"]:::{cls}')
    for e in graph.edges:
        arrow = "-.->" if e.dangling or graph.nodes[e.source].role == "traffic" else "-->"
        lines.append(f"  {_mid(e.source)} {arrow} {_mid(e.target)}")
    lines += [
        "  classDef high fill:#f8d7da,stroke:#b02a37,color:#000",
        "  classDef medium fill:#fff3cd,stroke:#997404,color:#000",
        "  classDef low fill:#d1e7dd,stroke:#146c43,color:#000",
        "  classDef traffic fill:#e2e3e5,stroke:#6c757d,color:#000",
        "  classDef missing fill:#fff,stroke:#6c757d,stroke-dasharray:4 4,color:#6c757d",
    ]
    return "\n".join(lines) + "\n"


def _mid(name: str) -> str:
    return name.replace("-", "_")
