"""Resource metrics from Prometheus (cAdvisor + kube-state-metrics + node-exporter via kube-prometheus-stack).

Answers "what did the fault do to CPU / memory?": per service, the level before injection vs the peak during the
fault and recovery, CPU as a share of the container's limit, and how often the container was CPU-throttled.
"""
from __future__ import annotations

import httpx

from . import config as cfg
from .models import ResourceUsage

NS = cfg.APP_NAMESPACE
SEL = f'namespace="{NS}",container!="",container!="POD"'
Q_CPU = f"sum by (pod) (rate(container_cpu_usage_seconds_total{{{SEL}}}[20s]))"
Q_THROTTLE = (f"sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{{SEL}}}[20s])) / "
              f"sum by (pod) (rate(container_cpu_cfs_periods_total{{{SEL}}}[20s]))")
Q_MEM = f"sum by (pod) (container_memory_working_set_bytes{{{SEL}}})"
Q_CPU_LIMIT = f'sum by (pod) (kube_pod_container_resource_limits{{namespace="{NS}",resource="cpu"}})'
Q_MEM_LIMIT = f'sum by (pod) (kube_pod_container_resource_limits{{namespace="{NS}",resource="memory"}})'
Q_NODE_CPU = '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[20s]))'


class Prom:
    def __init__(self, url: str) -> None:
        self.http = httpx.Client(base_url=url.rstrip("/"), timeout=10)

    def range(self, query: str, t0: float, t1: float, step: float = 5) -> list[dict]:
        r = self.http.get("/api/v1/query_range", params={"query": query, "start": t0, "end": t1, "step": step})
        r.raise_for_status()
        return r.json()["data"]["result"]

    def instant(self, query: str, t: float) -> list[dict]:
        r = self.http.get("/api/v1/query", params={"query": query, "time": t})
        r.raise_for_status()
        return r.json()["data"]["result"]


def app_of(pod: str, apps: list[str]) -> str | None:
    """'frontend-7d9f8c6b5-x2x4k' -> 'frontend' (longest Deployment name that prefixes the pod name)."""
    best = None
    for a in apps:
        if pod.startswith(a + "-") and (best is None or len(a) > len(best)):
            best = a
    return best


def split_series(series: list[dict], apps: list[str], t_inject: float) -> dict[str, tuple[list[float], list[float]]]:
    """Per app: (values before t_inject, values from t_inject on). Pods of the same app are pooled."""
    out: dict[str, tuple[list[float], list[float]]] = {}
    for s in series:
        app = app_of(s["metric"].get("pod", ""), apps)
        if app is None:
            continue
        before, after = out.setdefault(app, ([], []))
        for ts, val in s["values"]:
            v = float(val)
            if v != v:          # NaN (e.g. 0/0 throttling ratio)
                continue
            (before if float(ts) < t_inject else after).append(v)
    return out


def _by_app(series: list[dict], apps: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for s in series:
        app = app_of(s["metric"].get("pod", ""), apps)
        if app is not None:
            out[app] = max(out.get(app, 0.0), float(s["value"][1]))
    return out


def resource_usage(prom: Prom, apps: list[str], t_start: float, t_inject: float, t_end: float
                   ) -> tuple[dict[str, ResourceUsage], float | None, float | None]:
    """Returns (per-app usage, node CPU % baseline, node CPU % peak)."""
    cpu = split_series(prom.range(Q_CPU, t_start, t_end), apps, t_inject)
    thr = split_series(prom.range(Q_THROTTLE, t_start, t_end), apps, t_inject)
    mem = split_series(prom.range(Q_MEM, t_start, t_end), apps, t_inject)
    cpu_lim = _by_app(prom.instant(Q_CPU_LIMIT, t_inject), apps)
    mem_lim = _by_app(prom.instant(Q_MEM_LIMIT, t_inject), apps)
    avg = lambda xs: sum(xs) / len(xs) if xs else None
    r3 = lambda v: None if v is None else round(v, 3)
    usage: dict[str, ResourceUsage] = {}
    for app in apps:
        if app not in cpu:
            continue
        before, after = cpu[app]
        peak = max(after) if after else None
        lim = cpu_lim.get(app)
        t_after = thr.get(app, ([], []))[1]
        m_after = mem.get(app, ([], []))[1]
        usage[app] = ResourceUsage(
            cpu_limit_cores=lim, cpu_baseline_cores=r3(avg(before)), cpu_peak_cores=r3(peak),
            cpu_peak_pct_of_limit=round(100 * peak / lim, 1) if peak is not None and lim else None,
            throttled_peak_pct=round(100 * max(t_after), 1) if t_after else None,
            memory_limit_mib=round(mem_lim[app] / 2**20, 1) if app in mem_lim else None,
            memory_peak_mib=round(max(m_after) / 2**20, 1) if m_after else None,
        )
    node = prom.range(Q_NODE_CPU, t_start, t_end)
    node_before, node_after = [], []
    for s in node:
        for ts, val in s["values"]:
            (node_before if float(ts) < t_inject else node_after).append(float(val))
    nb = round(100 * avg(node_before), 1) if node_before else None
    na = round(100 * max(node_after), 1) if node_after else None
    return usage, nb, na
