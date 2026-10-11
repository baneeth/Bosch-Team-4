"""Campaign: run a plan's experiments one by one with the guard armed, let the adapter react after each run.

The loop is plain code. Stop rules: NOT_RECOVERED, 2 guard aborts in a row on the same target, a manual abort
(Ctrl-C / `chaos-agent abort`), another experiment active (teammate), budget used, or the adapter says stop.
A guard ABORT of a single experiment is a valid result, not a campaign failure.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

from rich.console import Console
from rich.markup import escape

from . import config as cfg
from . import policy, templates
from .guard import within_tolerance
from .k8s import Kube, deployment_available
from .models import (AdaptRecord, Baseline, CampaignEntry, CampaignRecord, Graph, PlannedRun, PlanRecord,
                     RunResult, RunStatus)
from .probe import Prober, stats

LOCK_FILE = cfg.STATE_DIR / "campaign.lock"
STOP_FILE = cfg.STATE_DIR / "campaign.stop"


def request_stop_file() -> bool:
    """Used by `chaos-agent abort`: tell a running campaign (other process) to stop after the current run."""
    if not LOCK_FILE.exists():
        return False
    STOP_FILE.write_text(datetime.now(timezone.utc).isoformat())
    return True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def acquire_lock() -> None:
    cfg.STATE_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip() or 0)
        except ValueError:
            pid = 0
        if pid and pid != os.getpid() and _pid_alive(pid):
            raise RuntimeError(f"another campaign is running (pid {pid})")
    LOCK_FILE.write_text(str(os.getpid()))
    STOP_FILE.unlink(missing_ok=True)


def release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)
    STOP_FILE.unlink(missing_ok=True)


class Campaign:
    def __init__(self, graph: Graph, baseline: Baseline, plan: PlanRecord, *, console: Console | None = None,
                 kube: Kube | None = None, adapt: bool = True, max_experiments: int | None = None,
                 runner_factory: Callable[[], object] | None = None,
                 adapter_fn: Callable[..., Awaitable] | None = None,
                 settle_fn: Callable[[], Awaitable[tuple[float, bool]]] | None = None) -> None:
        self.graph, self.baseline, self.plan, self.kube = graph, baseline, plan, kube
        self.console = console or Console()
        self.adapt = adapt
        self.budget = max_experiments or len(plan.experiments) + cfg.CAMPAIGN_MAX_INSERTS
        self.runner_factory = runner_factory or self._default_runner
        self.adapter_fn = adapter_fn or self._default_adapter
        self.settle_fn = settle_fn or self._settle
        self.current = None
        self.stop_reason: str | None = None
        self.record = CampaignRecord(campaign_id=f"{datetime.now():%Y%m%d-%H%M%S}-campaign", plan_id=plan.plan_id,
                                     started_at=_now())
        self.runs: dict[str, RunResult] = {}

    # --- control ---------------------------------------------------------------------------------------
    def request_stop(self, reason: str) -> None:
        """Ctrl-C / SIGTERM: abort the running experiment (fault removed) and stop the campaign."""
        self.stop_reason = self.stop_reason or reason
        if self.current is not None:
            self.current.request_abort(reason)

    def _default_runner(self):
        from .runner import Runner
        return Runner(self.kube, self.graph, self.baseline, self.console)

    async def _default_adapter(self, **kw):
        from .llm import adapter
        return await adapter.decide(**kw)

    # --- main loop -------------------------------------------------------------------------------------
    async def run(self) -> CampaignRecord:
        from .llm import adapter as adapter_mod
        from .llm.client import AIError

        queue = [p.model_copy(deep=True) for p in self.plan.experiments]
        done_keys: set[tuple] = set()
        inserts_left = cfg.CAMPAIGN_MAX_INSERTS
        last_abort_target: str | None = None
        precheck_failures = 0
        executed = 0
        self.log(f"[bold]campaign {self.record.campaign_id}[/]: plan {self.plan.plan_id} ({self.plan.source}), "
                 f"{len(queue)} experiments, budget {self.budget}, adapter {'on' if self.adapt else 'off'}")
        while queue:
            if self.stop_reason:
                break
            if STOP_FILE.exists():
                self.stop_reason = "stopped by `chaos-agent abort`"
                break
            if executed >= self.budget:
                self.stop_reason = f"budget used ({self.budget} experiments)"
                break
            item = queue.pop(0)
            spec = item.spec
            try:
                (templates.validate if spec.origin == "manual" else policy.check)(spec, self.graph)
            except templates.SpecError as e:
                self.record.skipped.append({"rank": item.rank, "type": spec.type.value, "target": spec.target,
                                            "error": str(e)})
                self.log(f"[yellow]skipped[/] {spec.type.value} on {spec.target}: {escape(str(e))}")
                continue
            executed += 1
            self.log(f"[bold cyan]experiment {executed}[/] (plan rank {item.rank}, {spec.origin}): "
                     f"{spec.type.value} on {spec.target} {spec.params} {spec.duration_s}s")
            if spec.reason:
                self.log(f"[dim]reason: {escape(spec.reason)}[/]")
            self.current = self.runner_factory()
            if self.stop_reason:                              # Ctrl-C arrived while setting up
                break
            result: RunResult = await self.current.run(spec)
            self.current = None
            self.runs[result.run_id] = result
            done_keys.add(policy.key(spec))
            self.record.entries.append(_entry(item, result))
            self.save()

            if result.status == RunStatus.precheck_failed:
                reason = result.abort_reason or ""
                if "another experiment is active" in reason:
                    self.stop_reason = f"another experiment is active (teammate?): {reason}"
                    break
                precheck_failures += 1
                if precheck_failures >= 2:
                    self.stop_reason = f"pre-check failed twice: {reason}"
                    break
            elif result.status == RunStatus.not_recovered:
                self.stop_reason = f"{result.run_id} did not recover - stopping to protect the shop"
                break
            elif result.status == RunStatus.aborted:
                reason = result.abort_reason or ""
                if not reason.startswith("guard"):
                    self.stop_reason = self.stop_reason or f"manual abort ({reason})"
                    break
                if last_abort_target == spec.target:
                    self.stop_reason = f"guard aborted 2 runs in a row on {spec.target}"
                    break
                last_abort_target = spec.target
            else:
                last_abort_target = None
            if self.stop_reason or STOP_FILE.exists():
                continue

            settled_s, healthy = await self.settle_fn()
            if not healthy:
                self.stop_reason = (f"shop not back within baseline tolerance {settled_s:.0f}s after "
                                    f"{result.run_id} - stopping to protect it")
                break
            self.log(f"shop settled after {settled_s:.0f}s")
            if not self.adapt or (not queue and inserts_left <= 0):
                continue
            history = [{"run_id": e.run_id, "type": e.spec.type.value, "target": e.spec.target,
                        "status": e.status, "prediction": e.prediction} for e in self.record.entries]
            shop_now = (f"healthy: checked by code after the run - all workloads available and the probe within "
                        f"baseline tolerance for {cfg.RECOVERY_STREAK} checks in a row ({settled_s:.0f}s after "
                        f"the run ended)")
            try:
                decision, transcript = await self.adapter_fn(
                    graph=self.graph, baseline=self.baseline, runs=self.runs, strategy=self.plan.strategy,
                    remaining=queue, history=history, last=result, inserts_left=inserts_left, shop_now=shop_now)
            except AIError as e:
                self.record.decisions.append(AdaptRecord(after_run=result.run_id, action="continue", applied=False,
                                                         reason=f"adapter unavailable: {e}"))
                self.log(f"[yellow]adapter unavailable[/] - continuing with the plan ({escape(str(e))})")
                continue
            applied, detail, stop = adapter_mod.apply(decision, queue, self.graph, done_keys, inserts_left,
                                                      item.rank)
            if applied and decision.action == "insert_followup":
                inserts_left -= 1
            self.record.decisions.append(AdaptRecord(after_run=result.run_id, action=decision.action,
                                                     reason=decision.reason, applied=applied, detail=detail,
                                                     transcript=transcript))
            self.log(f"[bold magenta]adapter[/]: {decision.action} - {escape(decision.reason)} "
                     f"[dim]({escape(detail)})[/]")
            self.save()
            if stop:
                self.stop_reason = f"adapter: {decision.reason}"
                break

        self.record.ended_at = _now()
        self.record.status = f"stopped: {self.stop_reason}" if self.stop_reason else "completed"
        self.save()
        self.log(f"[bold]campaign {self.record.status}[/] - {len(self.record.entries)} runs")
        return self.record

    # --- helpers ---------------------------------------------------------------------------------------
    async def _settle(self) -> tuple[float, bool]:
        """Wait until every workload is available and the probe is back within baseline tolerance.

        Returns (seconds waited, healthy). Not healthy after CAMPAIGN_SETTLE_TIMEOUT_S -> the campaign stops."""
        t0 = time.time()
        url = cfg.FRONTEND_URL or await asyncio.to_thread(self.kube.service_url, cfg.ENTRYPOINT)
        prober, stop = Prober(url), asyncio.Event()
        task = asyncio.create_task(prober.run(stop))
        streak = 0
        try:
            while time.time() - t0 < cfg.CAMPAIGN_SETTLE_TIMEOUT_S and not self.stop_reason:
                await asyncio.sleep(1)
                deps = await asyncio.to_thread(self.kube.deployments)
                healthy = all(deployment_available(d) for d in deps)
                streak = streak + 1 if healthy and within_tolerance(stats(prober.window()), self.baseline) else 0
                if streak >= cfg.RECOVERY_STREAK and time.time() - t0 >= cfg.WINDOW_S:
                    return time.time() - t0, True
        finally:
            stop.set()
            await task
        return time.time() - t0, bool(self.stop_reason)       # Ctrl-C/abort: the stop reason ends the loop

    def save(self) -> str:
        d = cfg.CAMPAIGNS_DIR / self.record.campaign_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "campaign.json").write_text(self.record.model_dump_json(indent=2))
        (d / "plan.json").write_text(self.plan.model_dump_json(indent=2))
        return str(d / "campaign.json")

    def log(self, msg: str) -> None:
        self.console.print(f"[dim]{datetime.now():%H:%M:%S}[/] {msg}")


def _entry(item: PlannedRun, r: RunResult) -> CampaignEntry:
    return CampaignEntry(rank=item.rank, spec=item.spec, run_id=r.run_id, status=r.status.value,
                         abort_reason=r.abort_reason, recovery_s=r.recovery_s, peak_error_rate=r.peak_error_rate,
                         peak_p95_ms=r.peak_p95_ms,
                         prediction=r.prediction_check.verdict if r.prediction_check else None)


def load(campaign_id: str | None) -> CampaignRecord:
    dirs = sorted(p for p in cfg.CAMPAIGNS_DIR.glob("*") if (p / "campaign.json").exists()) \
        if cfg.CAMPAIGNS_DIR.exists() else []
    match = dirs[-1:] if not campaign_id else [d for d in dirs if campaign_id in d.name]
    if not match:
        raise FileNotFoundError(f"no campaign matching '{campaign_id or 'latest'}' in {cfg.CAMPAIGNS_DIR}")
    return CampaignRecord.model_validate_json((match[-1] / "campaign.json").read_text())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
