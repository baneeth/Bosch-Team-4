"""Structured fixes: the analyst recommends one of these actions (with parameters); code renders the Kubernetes change.

Day 2 only recommends. The rendered manifests/commands are ready for the Day-3 fix-and-rerun
(server-side dry-run -> human approval -> apply -> re-run the verification experiment).
"""
from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from . import config as cfg
from .models import Graph


class _Fix(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(description="Deployment the fix applies to (namespace boutique)")


class ScaleReplicas(_Fix):
    action: Literal["scale_replicas"]
    replicas: int = Field(ge=2, le=5, description="Number of replicas (stateless services only)")


class AddPdb(_Fix):
    action: Literal["add_pdb"]
    min_available: int = Field(ge=1, le=4, description="Pods that must stay up during voluntary disruptions")


class SetResources(_Fix):
    action: Literal["set_resources"]
    cpu_limit_millicores: int = Field(ge=100, le=2000)
    memory_limit_mib: int = Field(ge=64, le=1024)


class AddHpa(_Fix):
    action: Literal["add_hpa"]
    min_replicas: int = Field(ge=1, le=5)
    max_replicas: int = Field(ge=2, le=10)
    cpu_target_pct: int = Field(ge=30, le=90, description="Average CPU utilisation (of requests) to scale at")


class TuneProbes(_Fix):
    action: Literal["tune_probes"]
    initial_delay_s: int = Field(ge=0, le=60)
    period_s: int = Field(ge=1, le=30)
    failure_threshold: int = Field(ge=1, le=10)


class CodeChange(_Fix):
    action: Literal["code_change"]
    change: str = Field(description="Concrete code/config change, e.g. 'set a 300 ms gRPC deadline on frontend -> "
                                    "productcatalogservice calls and fetch products in parallel'")


Fix = Union[ScaleReplicas, AddPdb, SetResources, AddHpa, TuneProbes, CodeChange]

CATALOG = [
    {"action": "scale_replicas", "addresses": "single point of failure: outages during pod kill/failure, slow "
     "recovery (new pod start + readiness), capacity", "notes": "stateless services only (not redis-cart)"},
    {"action": "add_pdb", "addresses": "voluntary disruptions (node drain, rollout) taking every replica down",
     "notes": "only useful together with >= 2 replicas"},
    {"action": "set_resources", "addresses": "CPU throttling / queueing under load, OOM kills under memory pressure",
     "notes": "most services have 200m CPU / 128Mi; the frontend is the capacity bottleneck at 200m"},
    {"action": "add_hpa", "addresses": "traffic spikes: add replicas automatically when CPU is high",
     "notes": "k3s ships metrics-server; scales on CPU % of requests"},
    {"action": "tune_probes", "addresses": "slow or flapping recovery: readiness detects a broken pod too late/early",
     "notes": "faster readiness shortens time-to-traffic of a replacement pod"},
    {"action": "code_change", "addresses": "latency amplification (sequential calls, no deadlines), missing "
     "timeouts/retries/circuit breakers, missing graceful degradation", "notes": "advisory: not applied automatically"},
]


def problems(fix: Fix, graph: Graph) -> list[str]:
    """Why this fix can't be applied as given (empty = fine)."""
    node = graph.nodes.get(fix.target)
    if node is None or not node.targetable:
        return [f"fix target '{fix.target}' is not an application deployment"]
    out = []
    if isinstance(fix, ScaleReplicas) and node.stateful:
        out.append(f"scale_replicas on stateful {fix.target} would split its data")
    if isinstance(fix, AddHpa) and fix.min_replicas > fix.max_replicas:
        out.append("add_hpa: min_replicas > max_replicas")
    return out


def render(fix: Fix) -> str:
    """The Kubernetes change as a shell command / manifest (for the report and the Day-3 apply step)."""
    ns, t = cfg.APP_NAMESPACE, fix.target
    if isinstance(fix, ScaleReplicas):
        return f"kubectl -n {ns} scale deployment/{t} --replicas={fix.replicas}"
    if isinstance(fix, SetResources):
        return (f"kubectl -n {ns} set resources deployment/{t} -c server "
                f"--limits=cpu={fix.cpu_limit_millicores}m,memory={fix.memory_limit_mib}Mi")
    if isinstance(fix, AddPdb):
        return "\n".join([
            "apiVersion: policy/v1", "kind: PodDisruptionBudget",
            f"metadata: {{name: {t}-pdb, namespace: {ns}}}",
            f"spec: {{minAvailable: {fix.min_available}, selector: {{matchLabels: {{{cfg.APP_LABEL}: {t}}}}}}}"])
    if isinstance(fix, AddHpa):
        return (f"kubectl -n {ns} autoscale deployment/{t} --min={fix.min_replicas} --max={fix.max_replicas} "
                f"--cpu-percent={fix.cpu_target_pct}")
    if isinstance(fix, TuneProbes):
        patch = ('{"spec":{"template":{"spec":{"containers":[{"name":"server","readinessProbe":{'
                 f'"initialDelaySeconds":{fix.initial_delay_s},"periodSeconds":{fix.period_s},'
                 f'"failureThreshold":{fix.failure_threshold}}}}}]}}}}}}}}')
        return f"kubectl -n {ns} patch deployment/{t} --type=strategic -p '{patch}'"
    return f"# code change in {t}: {fix.change}"
