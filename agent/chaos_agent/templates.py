"""Experiment templates: typed, capped parameters -> Chaos Mesh manifests.

This is the only way a fault gets created. CLI input and (later) LLM output both go through `validate()`,
so the hard caps here hold no matter who asks.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import config as cfg
from .models import Baseline, ExperimentSpec, ExperimentType, Graph, Thresholds
from .probe import CHECKOUT_FORM, PRODUCT_IDS


class SpecError(ValueError):
    """The experiment request violates a template rule or a hard cap."""


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PodKillParams(_Params):
    grace_period_s: int = Field(0, ge=0, le=30, description="Seconds the pod gets to shut down")


class PodFailureParams(_Params):
    """No knobs: the pod stays unavailable for the whole duration (Chaos Mesh swaps in a pause image)."""


class NetworkDelayParams(_Params):
    latency_ms: int = Field(300, ge=1, le=2000, description="Added latency on the target's traffic")
    jitter_ms: int = Field(0, ge=0, le=500)


class NetworkLossParams(_Params):
    loss_pct: int = Field(20, ge=1, le=cfg.MAX_LOSS_PCT, description="Percent of the target's packets dropped")
    correlation_pct: int = Field(0, ge=0, le=100, description="How much each drop depends on the previous one")


class CpuStressParams(_Params):
    workers: int = Field(1, ge=1, le=2, description="CPU stress worker threads")
    load: int = Field(100, ge=10, le=100, description="Percent load per worker")


class MemoryStressParams(_Params):
    size_mb: int = Field(64, ge=16, le=cfg.MAX_MEMORY_STRESS_MB,
                         description="Memory each worker allocates inside the target container (limit is 128 Mi for "
                                     "most services, so >~100 MB tests OOM behaviour)")
    workers: int = Field(1, ge=1, le=2, description="Memory stress workers")


class TrafficSpikeParams(_Params):
    users: int = Field(100, ge=1, le=cfg.MAX_LOAD_USERS, description="Peak concurrent virtual shoppers")
    ramp_s: int = Field(30, ge=1, le=120, description="Seconds to ramp from 0 to `users` (then held)")
    think_ms: int = Field(1000, ge=200, le=5000, description="Pause between a shopper's requests")
    checkout_pct: int = Field(20, ge=0, le=100, description="Share of shopping rounds that end in checkout")


@dataclass(frozen=True)
class Template:
    kind: str
    params: type[_Params]
    failure_class: str
    description: str
    instant: bool                      # fault happens at injection (nothing to revert afterwards)
    spec: Callable[[_Params, int], dict] | None   # Chaos Mesh spec body (None for non-Chaos-Mesh engines)
    engine: str = "chaos-mesh"         # "chaos-mesh" or "k6"


TEMPLATES: dict[ExperimentType, Template] = {
    ExperimentType.pod_kill: Template(
        kind="PodChaos", params=PodKillParams, failure_class="recovery", instant=True,
        description="Kill the target's pod; measures how fast Kubernetes replaces it and what users see meanwhile.",
        spec=lambda p, d: {"action": "pod-kill", "gracePeriod": p.grace_period_s}),
    ExperimentType.pod_failure: Template(
        kind="PodChaos", params=PodFailureParams, failure_class="errors", instant=False,
        description="Make the target's pod unavailable for the whole duration (service down, pod not deleted); shows "
                    "what users see while a dependency is gone. Removing the fault restores the pod.",
        spec=lambda p, d: {"action": "pod-failure"}),
    ExperimentType.network_delay: Template(
        kind="NetworkChaos", params=NetworkDelayParams, failure_class="slowdown", instant=False,
        description="Add latency to all traffic of the target's pod; shows how slowness propagates to callers.",
        spec=lambda p, d: {"action": "delay", "direction": "to",
                           "delay": {"latency": f"{p.latency_ms}ms", "jitter": f"{p.jitter_ms}ms",
                                     "correlation": "0"}}),
    ExperimentType.network_loss: Template(
        kind="NetworkChaos", params=NetworkLossParams, failure_class="errors", instant=False,
        description="Drop a share of the target pod's packets; shows how partial network failure turns into retries, "
                    "latency and errors for callers.",
        spec=lambda p, d: {"action": "loss", "direction": "to",
                           "loss": {"loss": str(p.loss_pct), "correlation": str(p.correlation_pct)}}),
    ExperimentType.cpu_stress: Template(
        kind="StressChaos", params=CpuStressParams, failure_class="slowdown", instant=False,
        description="Burn CPU inside the target's pod (within its CPU limit); shows sensitivity to noisy neighbours.",
        spec=lambda p, d: {"stressors": {"cpu": {"workers": p.workers, "load": p.load}}}),
    ExperimentType.memory_stress: Template(
        kind="StressChaos", params=MemoryStressParams, failure_class="resource exhaustion", instant=False,
        description="Allocate memory inside the target's container (counts against its memory limit); shows memory "
                    "pressure and OOM-kill/restart behaviour.",
        spec=lambda p, d: {"stressors": {"memory": {"workers": p.workers, "size": f"{p.size_mb}MB"}}}),
    ExperimentType.traffic_spike: Template(
        kind="K6Load", params=TrafficSpikeParams, failure_class="resource exhaustion", instant=False,
        engine="k6", spec=None,
        description="Flood the shop with many concurrent shoppers (k6) through the frontend; shows where CPU limits "
                    "and single replicas make it slow down or fail. Target must be the frontend (entrypoint)."),
}


def catalog() -> list[dict]:
    """Template catalog with parameter schemas and caps (handed to the LLM planner on Day 2)."""
    return [{"type": t.value, "engine": tpl.engine, "chaos_kind": tpl.kind, "failure_class": tpl.failure_class,
             "description": tpl.description, "params_schema": tpl.params.model_json_schema(),
             "duration_s": {"min": cfg.MIN_DURATION_S, "max": cfg.MAX_DURATION_S}}
            for t, tpl in TEMPLATES.items()]


def validate(spec: ExperimentSpec, graph: Graph) -> _Params:
    """Check the request against the dependency map and every hard cap. Returns the typed params."""
    node = graph.nodes.get(spec.target)
    if node is None or not node.exists:
        raise SpecError(f"unknown target '{spec.target}' (not a Deployment in namespace {graph.namespace})")
    if not node.targetable:
        raise SpecError(f"'{spec.target}' is not a valid chaos target (role={node.role})")
    if not cfg.MIN_DURATION_S <= spec.duration_s <= cfg.MAX_DURATION_S:
        raise SpecError(f"duration_s must be {cfg.MIN_DURATION_S}-{cfg.MAX_DURATION_S}, got {spec.duration_s}")
    if spec.max_error_rate is not None and not 0 < spec.max_error_rate <= cfg.CAP_ERROR_RATE:
        raise SpecError(f"max_error_rate must be in (0, {cfg.CAP_ERROR_RATE}], got {spec.max_error_rate}")
    if spec.max_p95_ms is not None and not 0 < spec.max_p95_ms <= cfg.CAP_P95_MS:
        raise SpecError(f"max_p95_ms must be in (0, {cfg.CAP_P95_MS:.0f}], got {spec.max_p95_ms}")
    try:
        params = TEMPLATES[spec.type].params(**spec.params)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
        raise SpecError(f"invalid params for {spec.type.value}: {problems}") from None
    if isinstance(params, TrafficSpikeParams):
        if spec.target != cfg.ENTRYPOINT:
            raise SpecError(f"traffic_spike sends user traffic through the entrypoint; target must be '{cfg.ENTRYPOINT}'")
        if params.ramp_s >= spec.duration_s:
            raise SpecError(f"ramp_s ({params.ramp_s}) must be shorter than duration_s ({spec.duration_s})")
    return params


def thresholds(spec: ExperimentSpec, baseline: Baseline) -> Thresholds:
    """Guard thresholds: the experiment's own (already capped) values, else defaults derived from baseline."""
    base_p95 = baseline.overall.p95_ms or 0.0
    default_p95 = min(max(cfg.GUARD_P95_BASELINE_FACTOR * base_p95, cfg.GUARD_P95_FLOOR_MS), cfg.CAP_P95_MS)
    return Thresholds(max_error_rate=spec.max_error_rate or cfg.GUARD_ERROR_RATE,
                      max_p95_ms=spec.max_p95_ms or default_p95)


def resource_name(run_id: str) -> str:
    return f"ca-{run_id}".lower().replace("_", "-")[:63].rstrip("-")


_K6_SCRIPT = """\
// Generated by chaos-agent for run __RUN_ID__ - traffic_spike: __USERS__ users, ramp __RAMP__s, total __DURATION__s
import http from "k6/http";
import { sleep } from "k6";

const BASE = "__BASE__";
const PRODUCTS = __PRODUCTS__;
const FORM = __FORM__;
const THINK = __THINK__;

export const options = {
  discardResponseBodies: true,
  scenarios: {
    spike: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [{ duration: "__RAMP__s", target: __USERS__ }, { duration: "__HOLD__s", target: __USERS__ }],
      gracefulRampDown: "3s",
      gracefulStop: "3s",
    },
  },
};

export default function () {
  const p = PRODUCTS[Math.floor(Math.random() * PRODUCTS.length)];
  http.get(`${BASE}/`);
  sleep(THINK);
  http.get(`${BASE}/product/${p}`);
  sleep(THINK);
  http.post(`${BASE}/cart`, { product_id: p, quantity: "1" }, { redirects: 0 });
  sleep(THINK);
  http.get(`${BASE}/cart`);
  sleep(THINK);
  if (Math.random() * 100 < __CHECKOUT__) {
    http.post(`${BASE}/cart/checkout`, FORM);
    sleep(THINK);
  }
}

export function handleSummary(data) {
  return { "__SUMMARY__": JSON.stringify(data) };
}
"""


def render_k6(spec: ExperimentSpec, params: TrafficSpikeParams, run_id: str, base_url: str, summary_path: str) -> str:
    """k6 script for a traffic spike. k6 stops by itself after duration_s (the load's auto-revert)."""
    values = {
        "__RUN_ID__": run_id, "__BASE__": base_url.rstrip("/"), "__USERS__": str(params.users),
        "__RAMP__": str(params.ramp_s), "__HOLD__": str(spec.duration_s - params.ramp_s),
        "__DURATION__": str(spec.duration_s), "__THINK__": f"{params.think_ms / 1000:g}",
        "__CHECKOUT__": str(params.checkout_pct), "__PRODUCTS__": json.dumps(PRODUCT_IDS),
        "__FORM__": json.dumps(CHECKOUT_FORM), "__SUMMARY__": summary_path,
    }
    script = _K6_SCRIPT
    for k, v in values.items():
        script = script.replace(k, v)
    return script


def users_at(params: TrafficSpikeParams, elapsed_s: float) -> int:
    """Virtual users k6 is running `elapsed_s` seconds after start (linear ramp, then hold)."""
    return max(0, min(params.users, round(params.users * elapsed_s / params.ramp_s)))


def render(spec: ExperimentSpec, params: _Params, run_id: str) -> dict:
    """Chaos Mesh manifest. Always: namespace pinned, mode one, our labels, and a duration (auto-revert)."""
    tpl = TEMPLATES[spec.type]
    if tpl.engine != "chaos-mesh":
        raise ValueError(f"{spec.type.value} is not a Chaos Mesh experiment")
    body = {
        "mode": "one",
        "selector": {"namespaces": [cfg.APP_NAMESPACE], "labelSelectors": {cfg.APP_LABEL: spec.target}},
        "duration": f"{spec.duration_s}s",
        **tpl.spec(params, spec.duration_s),
    }
    return {
        "apiVersion": "chaos-mesh.org/v1alpha1",
        "kind": tpl.kind,
        "metadata": {
            "name": resource_name(run_id),
            "namespace": cfg.APP_NAMESPACE,
            "labels": {cfg.MANAGED_BY_LABEL: cfg.MANAGED_BY_VALUE, cfg.RUN_ID_LABEL: run_id},
            "annotations": {"chaos-agent/reason": (spec.reason or "manual run")[:250]},
        },
        "spec": body,
    }
