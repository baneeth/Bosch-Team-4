"""Grafana integration: a "Chaos Lab" dashboard and a shaded annotation for every experiment.

Credentials: GRAFANA_PASSWORD env, else the file vm-bootstrap.sh writes (~/.chaos-lab/grafana-admin-password).
Everything here is best effort - a Grafana problem never affects an experiment.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx

from . import config as cfg

DASHBOARD_UID = "chaos-lab"
NS = cfg.APP_NAMESPACE
SEL = f'namespace="{NS}",container!="",container!="POD"'


def _svc(expr: str) -> str:
    """Turn pod names (frontend-7d9f8c6b5-x2x4k) into service names (frontend) for readable legends."""
    return f'label_replace({expr}, "service", "$1", "pod", "(.*)-[a-z0-9]+-[a-z0-9]+")'


QUERIES = {
    "cpu_pct_limit": _svc(f"100 * sum by (pod) (rate(container_cpu_usage_seconds_total{{{SEL}}}[30s])) / "
                          f'sum by (pod) (kube_pod_container_resource_limits{{namespace="{NS}",resource="cpu"}})'),
    "throttled": _svc(f"100 * sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{{SEL}}}[30s])) / "
                      f"sum by (pod) (rate(container_cpu_cfs_periods_total{{{SEL}}}[30s]))"),
    "cpu_cores": _svc(f"sum by (pod) (rate(container_cpu_usage_seconds_total{{{SEL}}}[30s]))"),
    "memory": _svc(f"sum by (pod) (container_memory_working_set_bytes{{{SEL}}}) / 1024 / 1024"),
    "net_tx": _svc(f'sum by (pod) (rate(container_network_transmit_bytes_total{{namespace="{NS}"}}[30s])) / 1024'),
    "restarts": _svc(f'sum by (pod) (kube_pod_container_status_restarts_total{{namespace="{NS}"}})'),
    "ready": f'sum(kube_pod_status_ready{{namespace="{NS}",condition="true"}})',
    "vm_cpu": '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[30s])))',
    "frontend_pct": (f'100 * sum(rate(container_cpu_usage_seconds_total{{{SEL},pod=~"frontend-.*"}}[30s])) / '
                     f'sum(kube_pod_container_resource_limits{{namespace="{NS}",pod=~"frontend-.*",resource="cpu"}})'),
}


def _password() -> str | None:
    if os.environ.get("GRAFANA_PASSWORD"):
        return os.environ["GRAFANA_PASSWORD"]
    f = Path.home() / ".chaos-lab" / "grafana-admin-password"
    return f.read_text().strip() if f.exists() else None


class Grafana:
    def __init__(self, url: str, password: str) -> None:
        self.url = url.rstrip("/")
        self.http = httpx.Client(base_url=self.url, auth=("admin", password), timeout=5)

    @classmethod
    def from_kube(cls, kube) -> "Grafana | None":
        pw = _password()
        if not pw:
            return None
        url = os.environ.get("GRAFANA_URL") or kube.prometheus_url().rsplit(":", 1)[0] + f":{cfg.GRAFANA_NODEPORT}"
        return cls(url, pw)

    def annotate(self, start: float, text: str, tags: list[str]) -> int | None:
        r = self.http.post("/api/annotations", json={"time": int(start * 1000), "tags": tags, "text": text})
        r.raise_for_status()
        return r.json().get("id")

    def close_annotation(self, ann_id: int, end: float, text: str) -> None:
        self.http.patch(f"/api/annotations/{ann_id}", json={"timeEnd": int(end * 1000), "text": text}).raise_for_status()

    def upsert_dashboard(self) -> str:
        r = self.http.post("/api/dashboards/db", json={"dashboard": dashboard(), "overwrite": True})
        r.raise_for_status()
        return f"{self.url}{r.json()['url']}"


def dashboard() -> dict:
    ds = {"type": "prometheus", "uid": "prometheus"}
    panels: list[dict] = []

    def stat(title, expr, x, unit, thresholds):
        panels.append({
            "type": "stat", "title": title, "datasource": ds, "gridPos": {"x": x, "y": 0, "w": 6, "h": 4},
            "targets": [{"refId": "A", "expr": expr, "datasource": ds}],
            "fieldConfig": {"defaults": {"unit": unit, "decimals": 0, "thresholds": {"mode": "absolute", "steps": thresholds}},
                            "overrides": []},
            "options": {"colorMode": "background", "graphMode": "area", "reduceOptions": {"calcs": ["lastNotNull"]}},
        })

    def series(title, expr, x, y, unit, desc, max_=None):
        defaults = {"unit": unit, "custom": {"lineWidth": 2, "fillOpacity": 10}}
        if max_ is not None:
            defaults["max"] = max_
        panels.append({
            "type": "timeseries", "title": title, "description": desc, "datasource": ds,
            "gridPos": {"x": x, "y": y, "w": 12, "h": 8},
            "targets": [{"refId": "A", "expr": expr, "legendFormat": "{{service}}", "datasource": ds}],
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["max", "lastNotNull"]},
                        "tooltip": {"mode": "multi", "sort": "desc"}},
        })

    red, amber, green = "red", "orange", "green"
    stat("Ready pods (boutique)", QUERIES["ready"], 0, "none",
         [{"color": red, "value": None}, {"color": amber, "value": 11}, {"color": green, "value": 12}])
    stat("Frontend CPU (% of its limit)", QUERIES["frontend_pct"], 6, "percent",
         [{"color": green, "value": None}, {"color": amber, "value": 70}, {"color": red, "value": 90}])
    stat("VM CPU", QUERIES["vm_cpu"], 12, "percent",
         [{"color": green, "value": None}, {"color": amber, "value": 70}, {"color": red, "value": 90}])
    stat("Pod restarts (total)", f'sum(kube_pod_container_status_restarts_total{{namespace="{NS}"}})', 18, "none",
         [{"color": green, "value": None}, {"color": amber, "value": 1}])
    series("CPU used, % of each service's limit", QUERIES["cpu_pct_limit"], 0, 4, "percent",
           "100 % = the service uses all the CPU it is allowed; beyond that it is throttled.")
    series("CPU throttling % (time spent waiting for CPU)", QUERIES["throttled"], 12, 4, "percent",
           "Share of 100 ms CPU periods in which the container hit its limit and had to wait.", 100)
    series("CPU cores per service", QUERIES["cpu_cores"], 0, 12, "none", "Absolute CPU use (1 = one full core).")
    series("Memory per service (MiB)", QUERIES["memory"], 12, 12, "decmbytes", "Working-set memory.")
    series("Network sent per service (KiB/s)", QUERIES["net_tx"], 0, 20, "KiBs", "Outgoing traffic per pod.")
    series("Container restarts per service", QUERIES["restarts"], 12, 20, "none", "Counter; a step = a restart.")

    return {
        "uid": DASHBOARD_UID, "title": "Chaos Lab - Online Boutique", "tags": ["chaos"], "timezone": "browser",
        "refresh": "5s", "time": {"from": "now-15m", "to": "now"}, "schemaVersion": 39,
        "description": "Shaded regions = chaos experiments run by chaos-agent (hover for type, target, outcome).",
        "annotations": {"list": [{
            "name": "Chaos experiments", "enable": True, "iconColor": "red",
            "datasource": {"type": "grafana", "uid": "-- Grafana --"},
            "target": {"type": "tags", "tags": ["chaos"], "matchAny": True, "limit": 100},
        }]},
        "panels": panels,
    }
