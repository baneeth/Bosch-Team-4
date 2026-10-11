"""Data models shared by every component (and later handed to the LLM planner/analyst as JSON)."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# --- Dependency map --------------------------------------------------------------------------------------
class ServiceNode(BaseModel):
    name: str
    exists: bool = True                    # False = referenced by an env var but no Deployment/Service
    role: Literal["app", "traffic"] = "app"
    replicas: int = 0
    ready_replicas: int = 0
    readiness_probe: bool = False
    liveness_probe: bool = False
    cpu_limit: str | None = None
    memory_limit: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    callers: list[str] = Field(default_factory=list)    # app callers only (traffic generators excluded)
    fan_in: int = 0
    fan_out: int = 0
    is_entrypoint: bool = False
    on_checkout_path: bool = False
    stateful: bool = False
    risk: Literal["high", "medium", "low"] = "low"
    risk_reasons: list[str] = Field(default_factory=list)
    targetable: bool = False
    dependents: list[str] = Field(default_factory=list)    # services that call it directly or transitively
    reach_flows: list[str] = Field(default_factory=list)   # user flows whose call chain includes it
    reach: float = 0.0                                      # share of user flows that depend on it (0-1)


class Edge(BaseModel):
    source: str
    target: str
    port: int | None = None
    env_var: str
    dangling: bool = False


class Graph(BaseModel):
    namespace: str
    generated_at: str
    nodes: dict[str, ServiceNode]
    edges: list[Edge]
    warnings: list[str] = Field(default_factory=list)


# --- Measurements ----------------------------------------------------------------------------------------
class FlowStats(BaseModel):
    count: int = 0
    errors: int = 0
    error_rate: float = 0.0
    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None


class Baseline(BaseModel):
    generated_at: str
    duration_s: float
    overall: FlowStats
    flows: dict[str, FlowStats]


# --- Experiments -----------------------------------------------------------------------------------------
class ExperimentType(str, Enum):
    pod_kill = "pod_kill"
    pod_failure = "pod_failure"
    network_delay = "network_delay"
    network_loss = "network_loss"
    cpu_stress = "cpu_stress"
    memory_stress = "memory_stress"
    traffic_spike = "traffic_spike"


ErrorBand = Literal["none", "low", "high"]              # error rate <= 2 % / <= 20 % / above
SlowdownBand = Literal["none", "moderate", "severe"]    # p95 factor < 2x / <= 10x / above
Outcome = Literal["COMPLETED", "ABORTED"]


class Prediction(BaseModel):
    """What the planner expects to observe - checked against the measurements after the run."""
    affected_flows: list[str] = Field(default_factory=list)
    error_band: ErrorBand = "none"
    slowdown_band: SlowdownBand = "none"
    outcome: Outcome = "COMPLETED"


class PredictionCheck(BaseModel):
    verdict: Literal["confirmed", "partly", "refuted"]
    matched: int
    total: int
    observed: Prediction
    details: list[str] = Field(default_factory=list)


class ExperimentSpec(BaseModel):
    type: ExperimentType
    target: str
    params: dict[str, int | float | str] = Field(default_factory=dict)
    duration_s: int = 60
    max_error_rate: float | None = None    # per-experiment guard thresholds (<= hard caps)
    max_p95_ms: float | None = None
    reason: str | None = None              # filled by the planner
    hypothesis: str | None = None
    prediction: Prediction | None = None
    origin: str = "manual"                 # manual | planner | adapter | fallback


class Thresholds(BaseModel):
    max_error_rate: float
    max_p95_ms: float


class FlowImpact(BaseModel):
    baseline: FlowStats
    impact: FlowStats
    p95_factor: float | None = None        # impact p95 / baseline p95


class ResourceUsage(BaseModel):
    """Per-service resource use from Prometheus: before the fault vs the worst moment during/after it."""
    cpu_limit_cores: float | None = None
    cpu_baseline_cores: float | None = None
    cpu_peak_cores: float | None = None
    cpu_peak_pct_of_limit: float | None = None
    throttled_peak_pct: float | None = None      # % of CPU periods in which the container was throttled
    memory_limit_mib: float | None = None
    memory_peak_mib: float | None = None


class LoadSummary(BaseModel):
    """What the k6 load generator itself saw (traffic_spike experiments)."""
    users_target: int
    users_at_stop: int | None = None             # set when the guard/kill switch stopped the load early
    requests: int | None = None
    rps: float | None = None
    failed_rate: float | None = None
    p95_ms: float | None = None
    max_vus: int | None = None


class RunStatus(str, Enum):
    completed = "COMPLETED"
    aborted = "ABORTED"
    not_recovered = "NOT_RECOVERED"
    precheck_failed = "PRECHECK_FAILED"


class RunResult(BaseModel):
    run_id: str
    spec: ExperimentSpec
    status: RunStatus
    abort_reason: str | None = None
    thresholds: Thresholds
    started_at: str
    injected_at: str | None = None
    fault_ended_at: str | None = None
    fault_duration_s: float | None = None        # actual time the fault was active
    recovery_s: float | None = None              # fault end -> metrics back within baseline tolerance
    pod_ready_s: float | None = None             # pod_kill: injection -> replacement pod Ready
    peak_error_rate: float = 0.0
    peak_p95_ms: float | None = None
    impact_error_rate: float | None = None       # all probe requests from injection until recovered
    impact_p95_ms: float | None = None
    availability: float | None = None            # 1 - impact_error_rate
    affected_flows: list[str] = Field(default_factory=list)
    guard_breaches: list[str] = Field(default_factory=list)
    worst_flow: str | None = None
    flow_impact: dict[str, FlowImpact] = Field(default_factory=dict)
    prediction_check: PredictionCheck | None = None
    pod_restarts: dict[str, int] = Field(default_factory=dict)
    resources: dict[str, ResourceUsage] = Field(default_factory=dict)
    node_cpu_baseline_pct: float | None = None
    node_cpu_peak_pct: float | None = None
    load: LoadSummary | None = None
    notes: list[str] = Field(default_factory=list)


# --- AI planning, campaigns and analysis -----------------------------------------------------------------
class PlannedRun(BaseModel):
    rank: int
    spec: ExperimentSpec                   # reason / hypothesis / prediction / origin live on the spec
    learning_goal: str | None = None


class PlanRecord(BaseModel):
    plan_id: str
    created_at: str
    source: Literal["ai", "fallback"]
    model: str | None = None
    strategy: str = ""
    experiments: list[PlannedRun] = Field(default_factory=list)
    rejected: list[dict] = Field(default_factory=list)       # AI suggestions that failed validation / policy
    skipped_ideas: list[dict] = Field(default_factory=list)  # ideas the AI considered and dropped (with why)
    coverage: list[str] = Field(default_factory=list)        # failure classes covered by the accepted plan
    transcript: str | None = None                            # results/ai/... file of the planner call
    note: str | None = None


class CampaignEntry(BaseModel):
    rank: int
    spec: ExperimentSpec
    run_id: str | None = None
    status: str | None = None
    abort_reason: str | None = None
    recovery_s: float | None = None
    peak_error_rate: float | None = None
    peak_p95_ms: float | None = None
    prediction: str | None = None          # confirmed / partly / refuted


class AdaptRecord(BaseModel):
    after_run: str
    action: str
    reason: str
    applied: bool
    detail: str | None = None
    transcript: str | None = None


class CampaignRecord(BaseModel):
    campaign_id: str
    plan_id: str
    started_at: str
    ended_at: str | None = None
    status: str = "running"                # completed | stopped: <reason>
    entries: list[CampaignEntry] = Field(default_factory=list)
    decisions: list[AdaptRecord] = Field(default_factory=list)
    skipped: list[dict] = Field(default_factory=list)


class ServiceVerdict(BaseModel):
    """Final per-service score: the AI's rubric scores, combined by code; anchor shown alongside."""
    service: str
    tested: bool
    dimensions: dict[str, float]           # the AI's rubric scores (clamped to anchor +- tolerance)
    anchors: dict[str, float | None]
    score: float
    anchor_score: float
    grade: str
    weight: float                          # criticality chosen by the AI (or code default)
    justification: str = ""


class AnalysisRecord(BaseModel):
    report_id: str
    created_at: str
    scope: str                             # campaign id or "all"
    run_ids: list[str]
    model: str | None = None
    score_version: str
    overall_score: float                   # AI-given (combined by code)
    overall_anchor: float
    grade: str
    services: list[ServiceVerdict]
    coverage: dict
    predictions: dict[str, int] = Field(default_factory=dict)
    ai: dict | None = None                 # the analyst's FindingsReport (None = AI unavailable)
    guardrail: dict = Field(default_factory=dict)
    transcripts: list[str] = Field(default_factory=list)
