"""Adapter agent: after every campaign run, decide what to do next based on what was just observed.

continue | modify_next (replace the next experiment) | insert_followup (run a new one next) | stop.
Code applies the decision only if the new experiment passes the same validation as the plan.
"""
from __future__ import annotations

import json

from .. import config as cfg
from .. import policy, templates
from ..models import Baseline, Graph, PlannedRun, RunResult
from . import client, tools
from .schemas import AdaptDecision, to_spec

INSTRUCTIONS = """\
You are the adapter of an AI-driven chaos-engineering campaign against Online Boutique (Kubernetes, Chaos Mesh).
A plan of ranked experiments is being executed one by one with a deterministic safety guard. After each run you
see the result and decide what happens next:
- continue: the plan still makes sense.
- modify_next: the next planned experiment should change (e.g. the last run showed the intensity is too high and
  would only trip the guard, or a parameter should be adjusted to answer the question better). Give the full
  replacement experiment.
- insert_followup: the last run revealed something worth probing right away (e.g. a surprising error spread, a
  refuted prediction, a dependency that broke). Give the experiment; it runs next. Use this sparingly.
- stop: the remaining experiments would teach little (the questions are already answered) or are clearly too
  risky given what was just learned.
Facts you can rely on: code only asks you AFTER it has verified the shop is healthy again ("shop_now": all
workloads available and user flows back within baseline tolerance). The last run's timeline may still end in
RECOVERING samples because its 5 s rolling window lags - that is not a reason to stop. If the shop had not
recovered, code would already have stopped the campaign. Guard aborts, errors and restarts during an experiment are
the findings we are looking for, not reasons to stop.
Rules: experiments must respect the per-type caps and AI policy (verify with check_experiment); don't repeat an
experiment that already ran with the same parameters; keep failure-class coverage in mind (the campaign should
cover at least 3 classes). Base the decision on the measured facts and cite them in the reason.
"""


def adapter_input(strategy: str, remaining: list[PlannedRun], history: list[dict], last: RunResult,
                  inserts_left: int, shop_now: str) -> str:
    return json.dumps({
        "plan_strategy": strategy,
        "shop_now": shop_now,
        "last_run": tools.run_detail(last),
        "campaign_so_far": history,
        "remaining_plan": [{"rank": p.rank, "type": p.spec.type.value, "target": p.spec.target,
                            "params": p.spec.params, "duration_s": p.spec.duration_s, "reason": p.spec.reason}
                           for p in remaining],
        "followups_left": inserts_left,
        "ai_policy": policy.RULES,
    }, indent=1)


def build_agent():
    from agents import Agent
    return Agent(name="adapter", instructions=INSTRUCTIONS, model=client.model(fast=True),
                 tools=[tools.get_run, tools.get_timeline, tools.check_experiment],
                 output_type=client.strict_output(AdaptDecision), model_settings=client.model_settings("adapter"))


async def decide(graph: Graph, baseline: Baseline, runs: dict[str, RunResult], strategy: str,
                 remaining: list[PlannedRun], history: list[dict], last: RunResult,
                 inserts_left: int, shop_now: str = "healthy (checked by code)") -> tuple[AdaptDecision, str]:
    ctx = tools.ToolContext(graph=graph, baseline=baseline, runs=runs)
    out, transcript = await client.run_agent(build_agent(), adapter_input(strategy, remaining, history, last,
                                                                          inserts_left, shop_now),
                                             name="adapter", context=ctx, max_turns=8, timeout_s=120)
    return out, transcript


def apply(decision: AdaptDecision, queue: list[PlannedRun], graph: Graph, done_keys: set[tuple],
          inserts_left: int, last_rank: int = 0) -> tuple[bool, str, bool]:
    """Apply a decision to the queue in place. Returns (applied, detail, stop_campaign)."""
    if decision.action == "stop":
        return True, "campaign stopped by the adapter", True
    if decision.action == "continue":
        return True, "plan unchanged", False
    if decision.experiment is None:
        return False, f"{decision.action} without an experiment - ignored", False
    spec = to_spec(decision.experiment, "adapter")
    try:
        policy.check(spec, graph)
    except templates.SpecError as e:
        return False, f"rejected: {e}", False
    if policy.key(spec) in done_keys:
        return False, "rejected: already ran with these parameters", False
    label = f"{spec.type.value} on {spec.target} {spec.params}"
    if decision.action == "modify_next":
        if not queue:
            return False, "nothing left to modify", False
        old = queue[0]
        queue[0] = PlannedRun(rank=old.rank, spec=spec, learning_goal=decision.experiment.learning_goal)
        return True, f"next experiment replaced: {old.spec.type.value} on {old.spec.target} -> {label}", False
    if inserts_left <= 0:
        return False, f"rejected: no follow-ups left (max {cfg.CAMPAIGN_MAX_INSERTS})", False
    rank = queue[0].rank if queue else last_rank + 1
    queue.insert(0, PlannedRun(rank=rank, spec=spec, learning_goal=decision.experiment.learning_goal))
    return True, f"follow-up inserted: {label}", False
