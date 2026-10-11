"""Experiment lifecycle: pre-check -> observe -> inject -> guard -> revert -> recovery -> result files."""
from __future__ import annotations

import asyncio
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from . import config as cfg
from . import loadgen, scoring, templates
from .faults import ACTIVE, GONE, ChaosMeshFault, LoadFault, wait_finished
from .guard import Check, Guard, blast_radius_issues, evaluate, precheck_ok, within_tolerance
from .k8s import Kube, chaos_state, deployment_available, pod_app, pod_ready, pod_restarts
from .models import Baseline, ExperimentSpec, ExperimentType, FlowImpact, Graph, RunResult, RunStatus
from .probe import Prober, per_flow, stats


def iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


def new_run_id(spec: ExperimentSpec) -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{spec.type.value}-{spec.target}"


def cleanup_finished(kube: Kube, log) -> list[str]:
    """Delete our chaos resources whose fault has already ended; return what is still active (incl. k6 load)."""
    active = []
    for obj in kube.list_managed_chaos():
        name, st = obj["metadata"]["name"], chaos_state(obj)
        if st["phase"] == "Stop" and st["recovered"]:
            kube.delete_chaos(obj["kind"], name)
            log(f"removed finished leftover {obj['kind']}/{name}")
        else:
            active.append(f"{obj['kind']}/{name}")
    pid = loadgen.active_pid()
    if pid:
        active.append(f"k6 load (pid {pid})")
    return active


class _Abort(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Runner:
    def __init__(self, kube: Kube, graph: Graph, baseline: Baseline, console: Console | None = None) -> None:
        self.kube = kube
        self.graph = graph
        self.baseline = baseline
        self.console = console or Console()
        self.abort_event = asyncio.Event()
        self.abort_reason = "manual abort"
        self.events: list[str] = []
        self.timeline: list[dict] = []
        self.t0 = time.time()

    # --- public ------------------------------------------------------------------------------------------
    def request_abort(self, reason: str = "manual abort") -> None:
        """Kill switch from inside the process (Ctrl-C / SIGTERM handler)."""
        if not self.abort_event.is_set():
            self.abort_reason = reason
            self.abort_event.set()

    async def run(self, spec: ExperimentSpec) -> RunResult:
        params = templates.validate(spec, self.graph)          # raises SpecError before touching anything
        tpl = templates.TEMPLATES[spec.type]
        th = templates.thresholds(spec, self.baseline)
        run_id = new_run_id(spec)
        run_dir = cfg.RUNS_DIR / run_id
        url = cfg.FRONTEND_URL or await asyncio.to_thread(self.kube.service_url, cfg.ENTRYPOINT)
        if tpl.engine == "k6":
            script = templates.render_k6(spec, params, run_id, url, str(run_dir / "k6-summary.json"))
            fault = LoadFault(loadgen.K6Process(script, run_dir), params)
            definition = {"engine": "k6", "script": "load.js"}
        else:
            manifest = templates.render(spec, params, run_id)
            fault = ChaosMeshFault(self.kube, manifest, tpl.instant)
            definition = {"engine": "chaos-mesh", "manifest": manifest}
        self.t0 = time.time()
        result = RunResult(run_id=run_id, spec=spec, status=RunStatus.completed, thresholds=th,
                           started_at=iso(self.t0))
        self.log(f"run [bold]{run_id}[/]: {spec.type.value} on {spec.target} for {spec.duration_s}s "
                 f"{params.model_dump()}")
        self.log(f"guard: abort if error rate > {th.max_error_rate:.0%} or p95 > {th.max_p95_ms:.0f}ms "
                 f"({cfg.GUARD_CONSECUTIVE} checks in a row), frontend silent > {cfg.GUARD_UNRESPONSIVE_S:.0f}s, "
                 f"or blast radius exceeded")

        # 1. Cluster pre-check (nothing has been started yet).
        bad = [d["metadata"]["name"] for d in await asyncio.to_thread(self.kube.deployments)
               if not deployment_available(d)]
        if bad:
            return self._finish(result, run_dir, definition, None, RunStatus.precheck_failed,
                                f"deployments not available: {', '.join(bad)}")
        active = await asyncio.to_thread(cleanup_finished, self.kube, self.log)
        if active:
            return self._finish(result, run_dir, definition, None, RunStatus.precheck_failed,
                                f"another experiment is active: {', '.join(active)} (use `chaos-agent abort`)")
        try:                                                  # API server + Chaos Mesh webhook validate it first
            await fault.dry_run()
        except Exception as e:
            return self._finish(result, run_dir, definition, None, RunStatus.precheck_failed,
                                f"Kubernetes dry-run rejected the fault definition: {_api_error(e)}")
        if tpl.engine == "chaos-mesh":
            self.log("Kubernetes server-side dry-run accepted the fault definition")
        pods_before = await asyncio.to_thread(self.kube.pods)
        target_pods_before = {p["metadata"]["name"] for p in pods_before if pod_app(p) == spec.target}
        restarts_before = _restarts_by_app(pods_before)

        # 2. Probe + healthy pre-window.
        prober = Prober(url)
        stop = asyncio.Event()
        probe_task = asyncio.create_task(prober.run(stop))
        guard = Guard(th)
        created = False
        t_inject = t_end = ann = None
        try:
            self.log(f"observing steady state for {cfg.PRE_WINDOW_S}s (probe -> {url})")
            await self._observe("pre", cfg.PRE_WINDOW_S, prober, th)
            if self.abort_event.is_set():
                return self._finish(result, run_dir, definition, prober, RunStatus.aborted,
                                    f"{self.abort_reason} (before injection)")
            pre = stats(prober.between(self.t0 + 5, time.time()))
            ok, why = precheck_ok(pre, self.baseline)
            if not ok:
                return self._finish(result, run_dir, definition, prober, RunStatus.precheck_failed, why)
            self.log(f"pre-check ok: {pre.count} requests, {pre.error_rate:.0%} errors, p95 {pre.p95_ms:.0f}ms")

            # 3. Inject.
            await fault.inject()
            created, t_inject = True, time.time()
            result.injected_at = iso(t_inject)
            self.log(f"[bold red]INJECTED[/] {fault.label}")
            ann = await self._annotate(spec, t_inject)

            # 4. Fault phase with the guard armed.
            try:
                if fault.instant:
                    await self._wait_injected(fault, t_inject + spec.duration_s, result)
                    t_end = t_inject
                    result.notes.append("Instant fault: aborting cannot undo a pod kill, so during recovery "
                                        "the guard reports breaches instead of aborting.")
                else:
                    t_end = await self._fault_phase(spec, fault, prober, guard, t_inject, result)
            except _Abort as a:
                t_end = await self._revert(fault, a.reason, result)
                created = False
            result.fault_ended_at = iso(t_end)
            result.fault_duration_s = round(t_end - t_inject, 1)

            # 5. Recovery.
            await self._recovery(spec, prober, guard, t_inject, t_end, target_pods_before, result)
        finally:
            if created:                                       # normal end, crash or Ctrl-C: always clean up
                await fault.remove()
                self.log(f"cleaned up {fault.label}")
            stop.set()
            await probe_task

        pods_after = await asyncio.to_thread(self.kube.pods)
        restarts_after = _restarts_by_app(pods_after)
        result.pod_restarts = {a: n - restarts_before.get(a, 0) for a, n in restarts_after.items()
                               if n - restarts_before.get(a, 0) > 0}
        result.guard_breaches = guard.seen
        if isinstance(fault, LoadFault):
            result.load = fault.k6.summary(params.users)
            result.load.users_at_stop = fault.stopped_at_users
        self._summarise(result, prober, t_inject, t_end)
        await self._collect_resources(result, t_inject)
        await self._close_annotation(ann, spec, result, t_end)
        return self._finish(result, run_dir, definition, prober, result.status, result.abort_reason)

    # --- phases ------------------------------------------------------------------------------------------
    async def _observe(self, phase: str, seconds: float, prober: Prober, th) -> None:
        end = time.time() + seconds
        while time.time() < end and not self.abort_event.is_set():
            tick = time.time()
            c = evaluate(prober.window(tick), tick, prober.last_response, th)
            self._record(phase, c, None)
            await asyncio.sleep(max(0.0, cfg.CHECK_INTERVAL_S - (time.time() - tick)))

    async def _wait_injected(self, fault, deadline: float, result: RunResult) -> None:
        limit = time.time() + cfg.INJECT_CONFIRM_TIMEOUT_S
        while time.time() < limit:
            state, _ = await fault.poll(time.time(), deadline)
            if state != "pending":
                self.log("Chaos Mesh confirms the fault is injected")
                return
            await asyncio.sleep(0.5)
        result.notes.append("Chaos Mesh did not confirm injection in time")
        self.log("[yellow]warning: Chaos Mesh did not confirm injection in time[/]")

    async def _fault_phase(self, spec, fault, prober, guard, t_inject, result) -> float:
        deadline = t_inject + spec.duration_s
        injected = warned = False
        while True:
            tick = time.time()
            if self.abort_event.is_set():
                raise _Abort(self.abort_reason)
            pods = await asyncio.to_thread(self.kube.pods)
            c = evaluate(prober.window(tick), tick, prober.last_response, guard.thresholds,
                         blast_radius_issues(pods, spec.target))
            reason = guard.check(c)
            self._record("fault", c, guard, fault.describe(tick - t_inject))
            if reason:
                raise _Abort(f"guard: {reason}")
            state, detail = await fault.poll(tick, deadline)
            if state == GONE:
                raise _Abort(f"kill switch: {detail}")
            if state == ACTIVE and not injected:
                injected = True
                if isinstance(fault, ChaosMeshFault):
                    self.log("Chaos Mesh confirms the fault is injected")
            if not injected and not warned and tick - t_inject > cfg.INJECT_CONFIRM_TIMEOUT_S:
                warned = True
                result.notes.append("fault injection was not confirmed in time")
                self.log("[yellow]warning: fault injection not confirmed[/]")
            if tick >= deadline:
                return await wait_finished(fault, deadline, self.log)
            await asyncio.sleep(max(0.0, cfg.CHECK_INTERVAL_S - (time.time() - tick)))

    async def _revert(self, fault, reason: str, result: RunResult) -> float:
        """Kill switch: remove the fault (delete the Chaos Mesh resource / stop k6)."""
        t = time.time()
        result.status, result.abort_reason = RunStatus.aborted, reason
        extra = ""
        if isinstance(fault, LoadFault):
            fault.stopped_at_users = fault.users_now()
            extra = f" at ~{fault.stopped_at_users} users"
        self.log(f"[bold yellow]ABORT[/] {escape(reason)} -> removing {fault.label}{extra}")
        gone = await fault.remove()
        if gone:
            self.log(f"fault removed in {time.time() - t:.1f}s")
        else:
            result.notes.append("fault still present after the removal timeout")
            self.log("[red]fault still present after timeout - check `chaos-agent status`[/]")
        result.notes.append(f"kill switch took {time.time() - t:.1f}s from decision to fault removed")
        return time.time()

    async def _recovery(self, spec, prober, guard, t_inject, t_end, target_pods_before, result) -> None:
        self.log(f"measuring recovery (steady = within baseline tolerance for {cfg.RECOVERY_STREAK} checks)")
        streak, first_ok = 0, None
        need_pod = spec.type == ExperimentType.pod_kill
        while time.time() - t_end < cfg.RECOVERY_TIMEOUT_S:
            tick = time.time()
            if self.abort_event.is_set():
                if result.status != RunStatus.aborted:
                    result.status, result.abort_reason = RunStatus.aborted, f"{self.abort_reason} (during recovery)"
                return
            pods = await asyncio.to_thread(self.kube.pods)
            c = evaluate(prober.window(tick), tick, prober.last_response, guard.thresholds,
                         blast_radius_issues(pods, spec.target))
            guard.check(c)                                   # records breaches; nothing left to revert
            self._record("recovery", c, guard)
            if need_pod and result.pod_ready_s is None:
                fresh = [p for p in pods if pod_app(p) == spec.target
                         and p["metadata"]["name"] not in target_pods_before and pod_ready(p)]
                if fresh:
                    result.pod_ready_s = round(tick - t_inject, 1)
                    self.log(f"replacement pod {fresh[0]['metadata']['name']} Ready after {result.pod_ready_s}s")
            if within_tolerance(c.window, self.baseline):
                streak += 1
                first_ok = first_ok or tick
            else:
                streak, first_ok = 0, None
            if streak >= cfg.RECOVERY_STREAK and result.recovery_s is None:
                # The first good window already covered the previous WINDOW_S seconds.
                result.recovery_s = round(max(0.0, first_ok - cfg.WINDOW_S - t_end), 1)
                self.log(f"[green]steady state restored[/]: recovery {result.recovery_s}s after the fault ended")
            if result.recovery_s is not None and (not need_pod or result.pod_ready_s is not None):
                return
            await asyncio.sleep(max(0.0, cfg.CHECK_INTERVAL_S - (time.time() - tick)))
        if result.status != RunStatus.aborted:
            result.status = RunStatus.not_recovered
        self.log(f"[red]not recovered within {cfg.RECOVERY_TIMEOUT_S}s[/]")

    async def _annotate(self, spec: ExperimentSpec, t_inject: float):
        """Start a shaded region on the Grafana "Chaos Lab" dashboard (best effort)."""
        from .grafana import Grafana
        try:
            g = await asyncio.to_thread(Grafana.from_kube, self.kube)
            if g is None:
                return None
            ann_id = await asyncio.to_thread(g.annotate, t_inject, f"{spec.type.value} on {spec.target} (running)",
                                             ["chaos", spec.type.value, spec.target])
            return g, ann_id
        except Exception as e:
            self.log(f"[dim]grafana annotation skipped: {escape(str(e))}[/]")
            return None

    async def _close_annotation(self, ann, spec: ExperimentSpec, result: RunResult, t_end: float | None) -> None:
        if not ann or not ann[1] or t_end is None:
            return
        g, ann_id = ann
        text = f"{spec.type.value} on {spec.target}: {result.status.value}"
        if result.abort_reason:
            text += f" ({result.abort_reason})"
        if result.recovery_s is not None:
            text += f", recovery {result.recovery_s}s"
        try:
            await asyncio.to_thread(g.close_annotation, ann_id, t_end, text)
        except Exception as e:
            self.log(f"[dim]grafana annotation update skipped: {escape(str(e))}[/]")

    async def _collect_resources(self, result: RunResult, t_inject: float | None) -> None:
        """CPU / memory per service from Prometheus: pre-window vs peak during fault + recovery."""
        if t_inject is None:
            return
        from .prom import Prom, resource_usage
        try:
            await asyncio.sleep(6)                           # let Prometheus scrape the last seconds (5 s interval)
            url = await asyncio.to_thread(self.kube.prometheus_url)
            apps = [n.name for n in self.graph.nodes.values() if n.exists]
            usage, nb, na = await asyncio.to_thread(resource_usage, Prom(url), apps, self.t0, t_inject, time.time())
            result.resources, result.node_cpu_baseline_pct, result.node_cpu_peak_pct = usage, nb, na
        except Exception as e:                               # metrics are nice-to-have, never fail a run on them
            result.notes.append(f"resource metrics unavailable: {e}")
            self.log(f"[yellow]resource metrics unavailable: {escape(str(e))}[/]")

    # --- results -----------------------------------------------------------------------------------------
    def _summarise(self, result: RunResult, prober: Prober, t_inject: float | None, t_end: float | None) -> None:
        if t_inject is None or t_end is None:
            return
        impact_end = max(t_end + (result.recovery_s or 0.0), t_inject + (result.pod_ready_s or 0.0),
                         t_inject + cfg.WINDOW_S)
        if result.recovery_s is None:
            impact_end = time.time()
        rows = [r for r in self.timeline if r["phase"] in ("fault", "recovery") and r["n"] >= cfg.GUARD_MIN_SAMPLES]
        result.peak_error_rate = max((r["error_rate"] for r in rows), default=0.0)
        result.peak_p95_ms = max((r["p95_ms"] for r in rows if r["p95_ms"] is not None), default=None)
        window = prober.between(t_inject, impact_end)
        overall = stats(window)
        result.impact_error_rate = round(overall.error_rate, 4)
        result.impact_p95_ms = overall.p95_ms
        result.availability = round(1 - overall.error_rate, 4)
        impact = per_flow(window)
        worst, worst_score = None, 0.0
        for flow, base in self.baseline.flows.items():
            imp = impact.get(flow)
            if imp is None:
                continue
            factor = round(imp.p95_ms / base.p95_ms, 2) if imp.p95_ms and base.p95_ms else None
            result.flow_impact[flow] = FlowImpact(baseline=base, impact=imp, p95_factor=factor)
            score = imp.error_rate * 100 + (factor or 0)
            if score > worst_score:
                worst, worst_score = flow, score
        result.worst_flow = worst
        result.affected_flows = scoring.affected_flows(result)
        if result.spec.prediction is not None:
            result.prediction_check = scoring.check_prediction(result.spec.prediction, result)
            self.log(f"prediction {result.prediction_check.verdict} "
                     f"({result.prediction_check.matched}/{result.prediction_check.total} aspects matched)")

    def _finish(self, result: RunResult, run_dir: Path, definition: dict, prober: Prober | None,
                status: RunStatus, reason: str | None) -> RunResult:
        result.status, result.abort_reason = status, reason
        colour = {"COMPLETED": "green", "ABORTED": "yellow"}.get(status.value, "red")
        self.log(f"[bold {colour}]{status.value}[/]" + (f": {escape(reason)}" if reason else ""))
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "spec.json").write_text(json.dumps(
            {"spec": result.spec.model_dump(mode="json"), **definition}, indent=2))
        (run_dir / "result.json").write_text(result.model_dump_json(indent=2))
        (run_dir / "events.log").write_text("\n".join(self.events) + "\n")
        t_inject = datetime.fromisoformat(result.injected_at).timestamp() if result.injected_at else None
        with open(run_dir / "timeline.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts", "t_since_start_s", "t_since_inject_s", "phase", "health", "n", "error_rate", "p50_ms",
                        "p95_ms", "guard_streak", "fault_state", "breaches"])
            for r in self.timeline:
                w.writerow([iso(r["ts"]), round(r["ts"] - self.t0, 1),
                            round(r["ts"] - t_inject, 1) if t_inject else "", r["phase"], r["health"], r["n"],
                            r["error_rate"], r["p50_ms"], r["p95_ms"], r["streak"], r["extra"],
                            " | ".join(r["breaches"])])
        if prober:
            with open(run_dir / "samples.csv", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["start", "end", "flow", "status", "ok", "latency_ms", "error"])
                for s in prober.samples:
                    w.writerow([iso(s.start), iso(s.end), s.flow, s.status, s.ok, round(s.latency_ms, 1), s.error])
        self.console.print(f"results: {run_dir}")
        return result

    # --- logging -----------------------------------------------------------------------------------------
    def log(self, msg: str) -> None:
        now = time.time()
        stamp = datetime.fromtimestamp(now).strftime("%H:%M:%S")
        self.events.append(f"{iso(now)} {Console().render_str(msg).plain}")
        self.console.print(f"[dim]{stamp}[/] {msg}")

    def _record(self, phase: str, c: Check, guard: Guard | None, extra: str = "") -> None:
        w = c.window
        streak = guard.streak if guard else 0
        self.timeline.append({"ts": c.ts, "phase": phase, "health": health_label(phase, c, self.baseline),
                              "n": w.count, "error_rate": w.error_rate, "p50_ms": w.p50_ms, "p95_ms": w.p95_ms,
                              "streak": streak, "breaches": c.breaches, "extra": extra})
        p95 = f"{w.p95_ms:6.0f}ms" if w.p95_ms is not None else "     - "
        flag = ""
        if c.breaches:
            flag = f" [yellow]breach {streak}/{cfg.GUARD_CONSECUTIVE}: {escape('; '.join(c.breaches))}[/]" \
                if phase == "fault" else f" [dim]{escape('; '.join(c.breaches))}[/]"
        extra_txt = f" {extra}" if extra else ""
        self.console.print(f"[dim]  {phase:<8} t+{c.ts - self.t0:5.1f}s[/] n={w.count:>2} "
                           f"err={w.error_rate:6.1%} p95={p95}{extra_txt}{flag}")


def health_label(phase: str, c: Check, baseline: Baseline) -> str:
    """Per-second label for the timeline: OK / IMPACTED / BREACH / RECOVERING (UNKNOWN = too few samples)."""
    if c.breaches:
        return "BREACH"
    if c.window.count < cfg.GUARD_MIN_SAMPLES:
        return "UNKNOWN"
    if within_tolerance(c.window, baseline):
        return "OK"
    return "RECOVERING" if phase == "recovery" else "IMPACTED"


def _api_error(e: Exception) -> str:
    """Short message from a Kubernetes ApiException (its body is JSON with a 'message')."""
    body = getattr(e, "body", None)
    if body:
        try:
            return json.loads(body).get("message", str(body))[:300]
        except (ValueError, AttributeError):
            return str(body)[:300]
    return str(e)[:300]


def _restarts_by_app(pods: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in pods:
        app = pod_app(p)
        if app:
            out[app] = out.get(app, 0) + pod_restarts(p)
    return out
