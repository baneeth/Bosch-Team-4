"""AI-facing output models (OpenAI structured outputs) and their conversion to our internal models.

Experiments are a union with one model per experiment type, generated from templates.TEMPLATES, so the schema the
model fills already carries each type's parameter names and hard caps. Code still re-validates everything.
"""
from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field, create_model

from .. import config as cfg
from ..fixes import Fix
from ..models import ExperimentSpec, ExperimentType, Prediction
from ..templates import TEMPLATES

FlowName = Literal[tuple(cfg.FLOW_CALLS)]  # type: ignore[valid-type]


def _experiment_model(t: ExperimentType) -> type[BaseModel]:
    name = "".join(p.capitalize() for p in t.value.split("_")) + "Experiment"
    return create_model(
        name, __config__=ConfigDict(extra="forbid"),
        type=(Literal[t.value], ...),
        target=(str, Field(description="Deployment name in namespace boutique (from the dependency map)")),
        duration_s=(int, Field(ge=cfg.MIN_DURATION_S, le=cfg.MAX_DURATION_S, description="Fault duration")),
        params=(TEMPLATES[t].params, ...),
    )


EXPERIMENT_MODELS = {t: _experiment_model(t) for t in ExperimentType}
AnyExperiment = Union[tuple(EXPERIMENT_MODELS.values())]  # type: ignore[valid-type]


class PredictionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    affected_flows: list[FlowName] = Field(description="User flows you expect to get errors or >3x slower")
    error_band: Literal["none", "low", "high"] = Field(description="Peak error rate: none <=2%, low <=20%, high >20%")
    slowdown_band: Literal["none", "moderate", "severe"] = Field(
        description="Worst flow p95 vs baseline: none <2x, moderate 2-10x, severe >10x")
    outcome: Literal["COMPLETED", "ABORTED"] = Field(description="ABORTED if you expect the safety guard to stop it")


class PlannedExperiment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    experiment: AnyExperiment  # type: ignore[valid-type]
    max_error_rate: float | None = Field(description="Optional stricter guard error-rate limit (0-0.2); null = default")
    max_p95_ms: float | None = Field(description="Optional stricter guard p95 limit in ms (<=1500); null = default")
    reason: str = Field(description="Why this experiment, and why at this rank - cite dependency-map facts/evidence")
    hypothesis: str = Field(description="What you expect to happen and why (steady-state hypothesis)")
    prediction: PredictionOut
    learning_goal: str = Field(description="The question this experiment answers")


class SkippedIdea(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idea: str
    why_not: str


class ExperimentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: str = Field(description="2-4 sentences: what you test first and why")
    experiments: list[PlannedExperiment] = Field(description="Ranked: most informative / riskiest first")
    skipped_ideas: list[SkippedIdea]


class AdaptDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["continue", "modify_next", "insert_followup", "stop"]
    reason: str = Field(description="One or two sentences citing what the last run showed")
    experiment: PlannedExperiment | None = Field(
        description="Required for modify_next (replaces the next experiment) and insert_followup (runs next); "
                    "null otherwise")


class ServiceScoreOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: str
    availability: int = Field(ge=0, le=100)
    latency: int = Field(ge=0, le=100)
    recovery: int = Field(ge=0, le=100)
    containment: int = Field(ge=0, le=100)
    posture: int = Field(ge=0, le=100)
    criticality_weight: float = Field(ge=0.5, le=3.0, description="Weight in the overall score (business impact)")
    justification: str = Field(description="One or two sentences citing run ids / facts")


class FindingOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    severity: Literal["critical", "high", "medium", "low"]
    services: list[str]
    evidence_runs: list[str] = Field(description="Run ids that show it (exactly as given)")
    evidence: str = Field(description="The measured numbers that show it")
    root_cause: str
    fix: Fix
    fix_rationale: str = Field(description="Why this fix addresses the root cause")
    expected_improvement: str = Field(description="What should change when the experiment is re-run after the fix")
    verify_with: AnyExperiment | None = Field(  # type: ignore[valid-type]
        description="The experiment to re-run after applying the fix (null if none fits)")


class FindingsReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(description="Executive summary, 3-5 sentences")
    findings: list[FindingOut] = Field(description="Most severe first")
    service_scores: list[ServiceScoreOut] = Field(description="One entry per service in the anchors table")
    score_explanation: str = Field(description="How you weighed the services and where you deviated from anchors")
    planner_review: str = Field(description="How well the planner's predictions held and what that tells us")
    limitations: list[str]


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def to_spec(pe: PlannedExperiment, origin: str) -> ExperimentSpec:
    e = pe.experiment
    return ExperimentSpec(
        type=ExperimentType(e.type), target=e.target, params=e.params.model_dump(), duration_s=e.duration_s,
        max_error_rate=pe.max_error_rate, max_p95_ms=pe.max_p95_ms, reason=pe.reason, hypothesis=pe.hypothesis,
        prediction=Prediction(**pe.prediction.model_dump()), origin=origin)


def experiment_to_spec(e: BaseModel, origin: str, reason: str | None = None) -> ExperimentSpec:
    return ExperimentSpec(type=ExperimentType(e.type), target=e.target, params=e.params.model_dump(),
                          duration_s=e.duration_s, reason=reason, origin=origin)
