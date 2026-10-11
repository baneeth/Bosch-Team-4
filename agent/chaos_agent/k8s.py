"""Thin Kubernetes wrapper. Returns plain dicts (API JSON shape) so the rest of the code stays testable."""
from __future__ import annotations

import time
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from . import config as cfg

CHAOS_GROUP = "chaos-mesh.org"
CHAOS_VERSION = "v1alpha1"
CHAOS_PLURALS = {"PodChaos": "podchaos", "NetworkChaos": "networkchaos", "StressChaos": "stresschaos"}
MANAGED_SELECTOR = f"{cfg.MANAGED_BY_LABEL}={cfg.MANAGED_BY_VALUE}"


class Kube:
    def __init__(self) -> None:
        config.load_kube_config()
        self._api = client.ApiClient()
        self.apps = client.AppsV1Api(self._api)
        self.core = client.CoreV1Api(self._api)
        self.custom = client.CustomObjectsApi(self._api)

    def _plain(self, obj: Any) -> Any:
        return self._api.sanitize_for_serialization(obj)

    # --- Workloads ---------------------------------------------------------------------------------------
    def deployments(self, ns: str = cfg.APP_NAMESPACE) -> list[dict]:
        return self._plain(self.apps.list_namespaced_deployment(ns).items)

    def services(self, ns: str = cfg.APP_NAMESPACE) -> list[dict]:
        return self._plain(self.core.list_namespaced_service(ns).items)

    def pods(self, ns: str = cfg.APP_NAMESPACE, label_selector: str | None = None) -> list[dict]:
        return self._plain(self.core.list_namespaced_pod(ns, label_selector=label_selector).items)

    def service_url(self, name: str, ns: str = cfg.APP_NAMESPACE) -> str:
        svc = self.core.read_namespaced_service(name, ns)
        return f"http://{svc.spec.cluster_ip}:{svc.spec.ports[0].port}"

    def prometheus_url(self) -> str:
        if cfg.PROMETHEUS_URL:
            return cfg.PROMETHEUS_URL
        node = self.core.list_node().items[0]
        ip = next(a.address for a in node.status.addresses if a.type == "InternalIP")
        return f"http://{ip}:{cfg.PROMETHEUS_NODEPORT}"

    # --- Chaos Mesh resources ----------------------------------------------------------------------------
    def create_chaos(self, manifest: dict, dry_run: bool = False) -> dict:
        """Create a chaos resource. dry_run=True: the API server (incl. Chaos Mesh's admission webhook) validates
        it fully but persists nothing - used as a last check before every injection."""
        ns = manifest["metadata"]["namespace"]
        plural = CHAOS_PLURALS[manifest["kind"]]
        kw = {"dry_run": "All"} if dry_run else {}
        return self.custom.create_namespaced_custom_object(CHAOS_GROUP, CHAOS_VERSION, ns, plural, manifest, **kw)

    def get_chaos(self, kind: str, name: str, ns: str = cfg.APP_NAMESPACE) -> dict | None:
        try:
            return self.custom.get_namespaced_custom_object(
                CHAOS_GROUP, CHAOS_VERSION, ns, CHAOS_PLURALS[kind], name)
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def delete_chaos(self, kind: str, name: str, ns: str = cfg.APP_NAMESPACE) -> bool:
        """Delete a chaos resource; Chaos Mesh reverts the fault before the object disappears."""
        try:
            self.custom.delete_namespaced_custom_object(
                CHAOS_GROUP, CHAOS_VERSION, ns, CHAOS_PLURALS[kind], name)
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    def wait_chaos_gone(self, kind: str, name: str, ns: str = cfg.APP_NAMESPACE,
                        timeout_s: float = cfg.DELETE_TIMEOUT_S) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.get_chaos(kind, name, ns) is None:
                return True
            time.sleep(0.5)
        return False

    def list_managed_chaos(self, ns: str = cfg.APP_NAMESPACE) -> list[dict]:
        items = []
        for kind, plural in CHAOS_PLURALS.items():
            res = self.custom.list_namespaced_custom_object(
                CHAOS_GROUP, CHAOS_VERSION, ns, plural, label_selector=MANAGED_SELECTOR)
            for item in res.get("items", []):
                item["kind"] = kind
                items.append(item)
        return items


# --- Pure helpers ----------------------------------------------------------------------------------------
def chaos_state(obj: dict) -> dict:
    """Summarise a chaos resource's status: desired phase plus AllInjected / AllRecovered flags."""
    status = obj.get("status") or {}
    conds = {c.get("type"): c.get("status") == "True" for c in status.get("conditions") or []}
    return {
        "phase": (status.get("experiment") or {}).get("desiredPhase"),
        "injected": conds.get("AllInjected", False),
        "recovered": conds.get("AllRecovered", False),
        "selected": conds.get("Selected", False),
    }


def pod_ready(pod: dict) -> bool:
    if (pod.get("metadata") or {}).get("deletionTimestamp"):
        return False
    for c in (pod.get("status") or {}).get("conditions") or []:
        if c.get("type") == "Ready":
            return c.get("status") == "True"
    return False


def pod_app(pod: dict) -> str | None:
    return ((pod.get("metadata") or {}).get("labels") or {}).get(cfg.APP_LABEL)


def pod_restarts(pod: dict) -> int:
    return sum(cs.get("restartCount", 0) for cs in (pod.get("status") or {}).get("containerStatuses") or [])


def deployment_available(dep: dict) -> bool:
    want = (dep.get("spec") or {}).get("replicas", 1)
    have = (dep.get("status") or {}).get("availableReplicas") or 0
    return have >= want
