"""chaos-agent command line."""
from __future__ import annotations

import asyncio
import json
import signal
import time
from datetime import datetime, timezone
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__, config as cfg, loadgen, templates
from .graph import build_graph, to_mermaid
from .k8s import Kube, chaos_state, deployment_available
from .models import Baseline, ExperimentSpec, ExperimentType, Graph, RunResult, RunStatus
from .probe import Prober, per_flow, stats

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="AI-driven chaos engineering agent for Online Boutique (Chaos Mesh on Kubernetes).")
console = Console()


def _version(value: bool) -> None:
    if value:
        console.print(f"chaos-agent {__version__}")
        raise typer.Exit()


@app.callback()
def main(version: Optional[bool] = typer.Option(None, "--version", callback=_version, is_eager=True)) -> None:
    pass


# --- graph -----------------------------------------------------------------------------------------------
def refresh_graph(kube: Kube) -> Graph:
    graph = build_graph(kube.deployments(), kube.services())
    cfg.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    cfg.GRAPH_FILE.write_text(graph.model_dump_json(indent=2))
    (cfg.RESULTS_DIR / "graph.mmd").write_text(to_mermaid(graph))
    return graph


@app.command()
def graph() -> None:
    """Extract the service dependency map -> results/graph.json + graph.mmd (Mermaid)."""
    g = refresh_graph(Kube())
    t = Table(title=f"Dependency map ({g.namespace})")
    for col in ("service", "replicas", "depends on", "callers", "checkout path", "reach", "risk"):
        t.add_column(col)
    colour = {"high": "red", "medium": "yellow", "low": "green"}
    for n in sorted(g.nodes.values(), key=lambda n: ({"high": 0, "medium": 1, "low": 2}[n.risk], n.name)):
        if not n.exists:
            t.add_row(f"[dim]{n.name}[/]", "-", "-", ", ".join(n.callers) or "-", "-", "-", "[dim]missing[/]")
            continue
        t.add_row(n.name, str(n.replicas), ", ".join(n.depends_on) or "-", ", ".join(n.callers) or "-",
                  "yes" if n.on_checkout_path else "", f"{n.reach:.0%}" if n.targetable else "-",
                  f"[{colour[n.risk]}]{n.risk if n.role == 'app' else 'traffic'}[/]")
    console.print(t)
    console.print(f"{len([n for n in g.nodes.values() if n.exists])} workloads, {len(g.edges)} edges "
                  f"({sum(e.dangling for e in g.edges)} dangling)")
    for w in g.warnings:
        console.print(f"[yellow]warning:[/] {w}")
    console.print(f"saved {cfg.GRAPH_FILE} and {cfg.RESULTS_DIR / 'graph.mmd'}")


# --- baseline --------------------------------------------------------------------------------------------
@app.command()
def baseline(duration: int = typer.Option(120, help="Measured seconds (after a 30 s warm-up)"),
             warmup: int = typer.Option(30, help="Warm-up seconds, not counted")) -> None:
    """Measure steady-state error rate and latency per user flow -> results/baseline.json."""
    kube = Kube()
    url = cfg.FRONTEND_URL or kube.service_url(cfg.ENTRYPOINT)
    console.print(f"probing {url} with {cfg.PROBE_USERS} virtual users: {warmup}s warm-up + {duration}s measured")

    async def go() -> tuple[Prober, float]:
        prober, stop = Prober(url), asyncio.Event()
        task = asyncio.create_task(prober.run(stop))
        t_start = time.time()
        end = t_start + warmup + duration
        while time.time() < end:
            await asyncio.sleep(5)
            w = stats(prober.window())
            phase = "warm-up " if time.time() < t_start + warmup else "measured"
            console.print(f"[dim]  {phase} t+{time.time() - t_start:4.0f}s[/] n={w.count:>2} "
                          f"err={w.error_rate:6.1%} p95={w.p95_ms or 0:6.0f}ms")
        stop.set()
        await task
        return prober, t_start + warmup

    prober, measured_from = asyncio.run(go())
    samples = [s for s in prober.samples if s.start >= measured_from]
    b = Baseline(generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), duration_s=duration,
                 overall=stats(samples), flows=per_flow(samples))
    cfg.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    cfg.BASELINE_FILE.write_text(b.model_dump_json(indent=2))
    _print_baseline(b, "Baseline")
    console.print(f"saved {cfg.BASELINE_FILE}")


# --- run -------------------------------------------------------------------------------------------------
def _parse_param(raw: str) -> tuple[str, int | float | str]:
    key, sep, val = raw.partition("=")
    if not sep:
        raise typer.BadParameter(f"expected key=value, got '{raw}'")
    for conv in (int, float):
        try:
            return key.strip(), conv(val)
        except ValueError:
            pass
    return key.strip(), val


@app.command()
def run(type: ExperimentType = typer.Option(..., "--type", "-t", help="Experiment type"),
        target: str = typer.Option(..., "--target", help="Deployment to target (namespace boutique)"),
        param: list[str] = typer.Option([], "--param", "-p", help="Template parameter key=value (repeatable)"),
        duration: int = typer.Option(60, help=f"Fault duration in seconds ({cfg.MIN_DURATION_S}-{cfg.MAX_DURATION_S})"),
        max_error_rate: Optional[float] = typer.Option(None, help=f"Guard: abort above this error rate (<= {cfg.CAP_ERROR_RATE})"),
        max_p95_ms: Optional[float] = typer.Option(None, help=f"Guard: abort above this p95 in ms (<= {cfg.CAP_P95_MS:.0f})"),
        reason: Optional[str] = typer.Option(None, help="Why this experiment (recorded in the result)")) -> None:
    """Run one experiment with the safety guard armed."""
    from .runner import Runner

    if not cfg.BASELINE_FILE.exists():
        console.print("[red]no baseline yet - run `chaos-agent baseline` first[/]")
        raise typer.Exit(1)
    spec = ExperimentSpec(type=type, target=target, params=dict(_parse_param(p) for p in param),
                          duration_s=duration, max_error_rate=max_error_rate, max_p95_ms=max_p95_ms, reason=reason)
    kube = Kube()
    g = refresh_graph(kube)
    base = Baseline.model_validate_json(cfg.BASELINE_FILE.read_text())
    try:
        templates.validate(spec, g)
    except templates.SpecError as e:
        console.print(f"[red]rejected:[/] {e}")
        raise typer.Exit(1)

    async def go() -> RunResult:
        runner = Runner(kube, g, base, console)
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, runner.request_abort, "manual abort (Ctrl-C)")
        loop.add_signal_handler(signal.SIGTERM, runner.request_abort, "manual abort (SIGTERM)")
        return await runner.run(spec)

    result = asyncio.run(go())
    _print_result(result)
    raise typer.Exit({RunStatus.completed: 0, RunStatus.aborted: 2}.get(result.status, 1))


def _print_result(r: RunResult) -> None:
    t = Table(title=f"Result: {r.run_id}", show_header=False)
    t.add_column("k")
    t.add_column("v")
    t.add_row("status", r.status.value + (f" ({r.abort_reason})" if r.abort_reason else ""))
    t.add_row("fault active", f"{r.fault_duration_s}s" if r.fault_duration_s is not None else "-")
    t.add_row("recovery", f"{r.recovery_s}s" if r.recovery_s is not None else "-")
    if r.pod_ready_s is not None:
        t.add_row("replacement pod Ready", f"{r.pod_ready_s}s")
    t.add_row("peak error rate", f"{r.peak_error_rate:.1%}")
    t.add_row("peak p95", _ms(r.peak_p95_ms))
    if r.impact_error_rate is not None:
        t.add_row("impact window", f"{r.impact_error_rate:.1%} errors, p95 {_ms(r.impact_p95_ms)} ms, "
                                   f"availability {r.availability:.1%}")
    t.add_row("worst flow", r.worst_flow or "-")
    t.add_row("affected flows", ", ".join(r.affected_flows) or "none")
    if r.prediction_check:
        pc = r.prediction_check
        t.add_row("prediction", f"{pc.verdict} ({pc.matched}/{pc.total}): " + "; ".join(pc.details))
    if r.pod_restarts:
        t.add_row("pod restarts", ", ".join(f"{k}={v}" for k, v in r.pod_restarts.items()))
    if r.load:
        ld = r.load
        t.add_row("load (k6)", f"target {ld.users_target} users"
                  + (f", stopped at ~{ld.users_at_stop}" if ld.users_at_stop is not None else "")
                  + (f", {ld.requests} requests ({ld.rps}/s)" if ld.requests else "")
                  + (f", {ld.failed_rate:.1%} failed" if ld.failed_rate is not None else "")
                  + (f", p95 {_ms(ld.p95_ms)} ms" if ld.p95_ms is not None else ""))
    if r.node_cpu_peak_pct is not None:
        t.add_row("VM CPU", f"{r.node_cpu_baseline_pct}% before → {r.node_cpu_peak_pct}% peak")
    console.print(t)
    if r.resources:
        rt = Table(title="Resources per service (before → peak), top 6 by CPU vs limit")
        for col in ("service", "CPU before", "CPU peak", "limit", "% of limit", "throttled", "memory peak"):
            rt.add_column(col, justify="right" if col != "service" else "left")
        top = sorted(r.resources.items(), key=lambda kv: -(kv[1].cpu_peak_pct_of_limit or 0))[:6]
        for app_name, u in top:
            rt.add_row(app_name, _cores(u.cpu_baseline_cores), _cores(u.cpu_peak_cores), _cores(u.cpu_limit_cores),
                       f"{u.cpu_peak_pct_of_limit or 0:.0f}%", f"{u.throttled_peak_pct or 0:.0f}%",
                       f"{u.memory_peak_mib or 0:.0f}/{u.memory_limit_mib or 0:.0f} MiB")
        console.print(rt)
    if r.flow_impact:
        ft = Table(title="Impact per flow (fault + recovery window vs baseline)")
        for col in ("flow", "requests", "error rate", "p95 baseline", "p95 impact", "x"):
            ft.add_column(col, justify="right" if col != "flow" else "left")
        for f, imp in r.flow_impact.items():
            ft.add_row(f, str(imp.impact.count), f"{imp.impact.error_rate:.1%}", _ms(imp.baseline.p95_ms),
                       _ms(imp.impact.p95_ms), f"{imp.p95_factor or '-'}")
        console.print(ft)


# --- status / abort --------------------------------------------------------------------------------------
@app.command()
def status() -> None:
    """Show active chaos resources created by this agent and workload health."""
    kube = Kube()
    items = kube.list_managed_chaos()
    if items:
        t = Table(title="Chaos resources (managed by chaos-agent)")
        for col in ("kind", "name", "phase", "injected", "recovered"):
            t.add_column(col)
        for o in items:
            st = chaos_state(o)
            t.add_row(o["kind"], o["metadata"]["name"], str(st["phase"]), str(st["injected"]), str(st["recovered"]))
        console.print(t)
    else:
        console.print("no chaos resources managed by chaos-agent")
    pid = loadgen.active_pid()
    console.print(f"k6 traffic load running (pid {pid})" if pid else "no k6 traffic load running")
    deps = kube.deployments()
    bad = [d["metadata"]["name"] for d in deps if not deployment_available(d)]
    console.print(f"workloads: {len(deps) - len(bad)}/{len(deps)} available" + (f" - not available: {', '.join(bad)}" if bad else ""))


@app.command()
def abort() -> None:
    """Kill switch: remove every fault created by this agent (Chaos Mesh resources and k6 traffic load), and stop
    a running campaign.

    Without the agent: kubectl delete podchaos,networkchaos,stresschaos -n boutique -l app.kubernetes.io/managed-by=chaos-agent
    and: pkill k6
    """
    from . import campaign as campaign_mod
    kube = Kube()
    t = time.time()
    if campaign_mod.request_stop_file():
        console.print("campaign told to stop (no further experiments will start)")
    pid = loadgen.stop_active()
    if pid:
        console.print(f"stopped k6 traffic load (pid {pid}) in {time.time() - t:.1f}s")
    items = kube.list_managed_chaos()
    if not items:
        console.print("no chaos resources to remove" if pid else "nothing to abort")
        return
    for o in items:
        kube.delete_chaos(o["kind"], o["metadata"]["name"])
        console.print(f"deleted {o['kind']}/{o['metadata']['name']}")
    for o in items:
        if not kube.wait_chaos_gone(o["kind"], o["metadata"]["name"]):
            console.print(f"[red]{o['kind']}/{o['metadata']['name']} still present[/]")
    console.print(f"[bold]all faults reverted[/] in {time.time() - t:.1f}s")


@app.command()
def grafana() -> None:
    """Create/update the "Chaos Lab" Grafana dashboard (experiments appear on it as shaded regions)."""
    from .grafana import Grafana
    g = Grafana.from_kube(Kube())
    if g is None:
        console.print("[red]no Grafana password: set GRAFANA_PASSWORD or create ~/.chaos-lab/grafana-admin-password[/]")
        raise typer.Exit(1)
    url = g.upsert_dashboard()
    console.print(f"dashboard ready: {url}")
    console.print("(from your laptop use the VM's public IP instead of the internal address)")


@app.command()
def runs() -> None:
    """List saved experiment runs (newest last)."""
    if not cfg.RUNS_DIR.exists():
        console.print("no runs yet")
        return
    t = Table(title="Experiment runs")
    for col in ("run id", "status", "peak err", "peak p95", "recovery"):
        t.add_column(col)
    for d in sorted(p for p in cfg.RUNS_DIR.iterdir() if (p / "result.json").exists()):
        r = RunResult.model_validate_json((d / "result.json").read_text())
        reason = f" ({r.abort_reason[:40]})" if r.abort_reason else ""
        t.add_row(d.name, r.status.value + reason, f"{r.peak_error_rate:.0%}", _ms(r.peak_p95_ms),
                  f"{r.recovery_s}s" if r.recovery_s is not None else "-")
    console.print(t)


@app.command()
def show(what: str = typer.Argument("latest", help="'baseline', 'latest', or (part of) a run id")) -> None:
    """Show a saved result without re-running anything: the baseline or an experiment run."""
    if what == "baseline":
        if not cfg.BASELINE_FILE.exists():
            console.print("[red]no baseline yet - run `chaos-agent baseline`[/]")
            raise typer.Exit(1)
        b = Baseline.model_validate_json(cfg.BASELINE_FILE.read_text())
        _print_baseline(b, f"Baseline (measured {b.generated_at}, {b.duration_s:.0f} s)")
        return
    dirs = sorted(p for p in cfg.RUNS_DIR.glob("*") if (p / "result.json").exists()) if cfg.RUNS_DIR.exists() else []
    match = dirs[-1:] if what == "latest" else [d for d in dirs if what in d.name]
    if not match:
        console.print(f"[red]no run matching '{what}' - see `chaos-agent runs`[/]")
        raise typer.Exit(1)
    run_dir = match[-1]
    _print_result(RunResult.model_validate_json((run_dir / "result.json").read_text()))
    console.print(f"[dim]events: {run_dir / 'events.log'}[/]")
    console.print((run_dir / "events.log").read_text())


def _print_baseline(b: Baseline, title: str) -> None:
    t = Table(title=title)
    for col in ("flow", "requests", "error rate", "p50 ms", "p95 ms", "p99 ms"):
        t.add_column(col, justify="right" if col != "flow" else "left")
    for name, s in [*b.flows.items(), ("OVERALL", b.overall)]:
        t.add_row(name, str(s.count), f"{s.error_rate:.1%}", _ms(s.p50_ms), _ms(s.p95_ms), _ms(s.p99_ms))
    console.print(t)


@app.command("catalog")
def catalog_cmd() -> None:
    """Print the experiment template catalog (types, parameters, caps) as JSON."""
    console.print_json(json.dumps(templates.catalog()))


def _ms(v: float | None) -> str:
    return "-" if v is None else f"{v:.0f}"


def _cores(v: float | None) -> str:
    return "-" if v is None else f"{v:.3f}"


# --- Day 2: AI planner, campaign, analyst, report --------------------------------------------------------
def _load_baseline() -> Baseline:
    if not cfg.BASELINE_FILE.exists():
        console.print("[red]no baseline yet - run `chaos-agent baseline` first[/]")
        raise typer.Exit(1)
    return Baseline.model_validate_json(cfg.BASELINE_FILE.read_text())


def _baseline_age_h(b: Baseline) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(b.generated_at)).total_seconds() / 3600


def _model() -> str:
    from .llm import client
    return client.model()


def _scope_runs(campaign_id: Optional[str], all_runs: bool):
    """(scope label, results, campaign record or None): a campaign's runs, or every saved run."""
    from . import campaign as campaign_mod
    from .llm.tools import load_runs
    if all_runs:
        return "all", list(load_runs().values()), None
    try:
        rec = campaign_mod.load(campaign_id)
    except FileNotFoundError:
        if campaign_id:
            console.print(f"[red]no campaign matching '{campaign_id}'[/]")
            raise typer.Exit(1)
        return "all", list(load_runs().values()), None
    ids = [e.run_id for e in rec.entries if e.run_id]
    return rec.campaign_id, list(load_runs(ids).values()), rec


@app.command("ai-check")
def ai_check(list_all: bool = typer.Option(False, "--all", help="List every model, not just GPT / o-series")) -> None:
    """Check the OpenAI setup: key (masked), available models, and one tiny structured-output call."""
    from pydantic import BaseModel

    from .llm import client
    console.print(f"env file: {cfg.ENV_FILE} ({'found' if cfg.ENV_FILE.exists() else 'missing'})")
    console.print(f"OPENAI_API_KEY: {client.masked_key()}")
    console.print(f"models: planner/analyst = [bold]{client.model()}[/], adapter = [bold]{client.model(fast=True)}[/]")
    console.print("reasoning effort: " + ", ".join(f"{a} = {client.effort(a)}" for a in ("planner", "adapter",
                                                                                          "analyst")))
    if not client.ai_available():
        console.print("[red]no API key - create ~/chaos-agent/.env with OPENAI_API_KEY=... (chmod 600)[/]")
        raise typer.Exit(1)
    try:
        ids = client.list_models()
    except Exception as e:
        console.print(f"[red]listing models failed:[/] {e}")
        raise typer.Exit(1)
    shown = ids if list_all else [m for m in ids if m.startswith(("gpt-", "o1", "o3", "o4", "o5"))]
    console.print(f"{len(ids)} models available to this key; {'all' if list_all else 'GPT / o-series'}:")
    console.print(", ".join(shown))
    for m in (client.model(), client.model(fast=True)):
        console.print(f"{m}: {'[green]available[/]' if m in ids else '[red]NOT available - set it in .env[/]'}")

    class Pong(BaseModel):
        ok: bool
        note: str

    async def go():
        from agents import Agent
        agent = Agent(name="ai-check", instructions="Reply ok=true and a 5-word note.", model=client.model(fast=True),
                      output_type=client.strict_output(Pong))
        t = time.time()
        out, path = await client.run_agent(agent, "ping", name="ai-check", max_turns=2, timeout_s=60, attempts=1)
        return out, path, time.time() - t

    try:
        out, path, secs = asyncio.run(go())
    except client.AIError as e:
        console.print(f"[red]structured call failed:[/] {e}")
        raise typer.Exit(1)
    console.print(f"[green]structured call ok[/] in {secs:.1f}s: {out.model_dump()} (transcript {path})")


def _print_plan(p) -> None:
    t = Table(title=f"Plan {p.plan_id} ({p.source}{', ' + p.model if p.model else ''})")
    for col in ("rank", "experiment", "duration", "reason", "prediction"):
        t.add_column(col)
    for r in p.experiments:
        s, pr = r.spec, r.spec.prediction
        pred = (f"{', '.join(pr.affected_flows) or 'no flow'}; err {pr.error_band}; slow {pr.slowdown_band}; "
                f"{pr.outcome}") if pr else "-"
        t.add_row(str(r.rank), f"{s.type.value} on {s.target}\n{s.params or ''}", f"{s.duration_s}s",
                  escape(s.reason or "-"), pred)
    console.print(f"[bold]strategy:[/] {escape(p.strategy)}")
    console.print(t)
    console.print(f"failure classes covered: {', '.join(p.coverage) or '-'}")
    for rj in p.rejected:
        console.print(f"[yellow]rejected[/] {rj['type']} on {rj['target']} {rj.get('params') or ''}: {escape(rj['error'])}")
    for sk in p.skipped_ideas:
        console.print(f"[dim]skipped idea: {escape(sk['idea'])} - {escape(sk['why_not'])}[/]")
    if p.note:
        console.print(f"[yellow]note:[/] {p.note}")


@app.command()
def plan(max_experiments: int = typer.Option(6, help=f"Budget (<= {cfg.AI_MAX_EXPERIMENTS})"),
         focus: Optional[str] = typer.Option(None, help="Optional focus, e.g. 'checkout path'"),
         fallback: bool = typer.Option(False, help="Use the deterministic fallback planner (no AI)")) -> None:
    """Planner agent: rank and select experiments from the dependency map -> results/plans/<id>.json."""
    from .llm import planner
    from .llm.tools import load_runs
    kube = Kube()
    g = refresh_graph(kube)
    base = _load_baseline()
    n = min(max_experiments, cfg.AI_MAX_EXPERIMENTS)
    console.print(f"planning up to {n} experiments ({'fallback' if fallback else 'planner agent, ' + _model()})...")
    p = asyncio.run(planner.make_plan(g, base, load_runs(), n, focus, kube, use_ai=not fallback))
    path = planner.save(p)
    _print_plan(p)
    console.print(f"saved {path}")


@app.command()
def campaign(plan_id: Optional[str] = typer.Option(None, "--plan", help="Plan id (part) to run; default: latest"),
             new: bool = typer.Option(False, "--new", help="Make a new plan first (planner agent)"),
             yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for approval"),
             no_adapt: bool = typer.Option(False, "--no-adapt", help="Don't call the adapter agent between runs"),
             max_experiments: Optional[int] = typer.Option(None, help="Hard budget of experiments (incl. follow-ups)"),
             rebaseline: bool = typer.Option(False, help="Re-measure the baseline first"),
             fallback: bool = typer.Option(False, help="With --new: deterministic fallback planner")) -> None:
    """Run a plan's experiments one by one (guard armed); the adapter agent reacts after each run."""
    from . import campaign as campaign_mod
    from .llm import planner
    from .llm.tools import load_runs
    from .runner import cleanup_finished

    kube = Kube()
    g = refresh_graph(kube)
    active = cleanup_finished(kube, lambda m: console.print(f"[dim]{m}[/]"))
    if active:
        console.print(f"[red]another experiment is active: {', '.join(active)}[/] - wait or `chaos-agent abort`")
        raise typer.Exit(1)
    if rebaseline or not cfg.BASELINE_FILE.exists() or _baseline_age_h(_load_baseline()) > cfg.BASELINE_MAX_AGE_H:
        console.print("[bold]baseline missing or older than "
                      f"{cfg.BASELINE_MAX_AGE_H} h - measuring a fresh one (2.5 min)[/]")
        baseline(duration=120, warmup=30)
    base = _load_baseline()
    # One event loop for planning and the campaign: the OpenAI client binds to the loop of its first call.
    aio = asyncio.Runner()
    if new:
        budget = cfg.AI_MAX_EXPERIMENTS if max_experiments is None else min(max_experiments, cfg.AI_MAX_EXPERIMENTS)
        p = aio.run(planner.make_plan(g, base, load_runs(), budget, kube=kube, use_ai=not fallback))
        console.print(f"saved {planner.save(p)}")
    else:
        try:
            p = planner.load(plan_id)
        except FileNotFoundError as e:
            console.print(f"[red]{e}[/] - use --new or `chaos-agent plan`")
            raise typer.Exit(1)
    _print_plan(p)
    if not p.experiments:
        console.print("[red]the plan has no runnable experiments[/]")
        raise typer.Exit(1)
    if not yes and not typer.confirm("Run this campaign now?", default=False):
        raise typer.Exit(0)

    camp = campaign_mod.Campaign(g, base, p, console=console, kube=kube, adapt=not no_adapt,
                                 max_experiments=max_experiments)

    async def go():
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, camp.request_stop, "manual abort (Ctrl-C)")
        loop.add_signal_handler(signal.SIGTERM, camp.request_stop, "manual abort (SIGTERM)")
        return await camp.run()

    try:
        campaign_mod.acquire_lock()
    except RuntimeError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    try:
        rec = aio.run(go())
    finally:
        campaign_mod.release_lock()
        aio.close()
    t = Table(title=f"Campaign {rec.campaign_id}: {rec.status}")
    for col in ("rank", "experiment", "origin", "status", "peak err", "peak p95", "recovery", "prediction"):
        t.add_column(col)
    for e in rec.entries:
        t.add_row(str(e.rank), f"{e.spec.type.value} on {e.spec.target}", e.spec.origin,
                  (e.status or "-") + (f" ({e.abort_reason[:40]})" if e.abort_reason else ""),
                  f"{e.peak_error_rate or 0:.0%}", _ms(e.peak_p95_ms),
                  f"{e.recovery_s}s" if e.recovery_s is not None else "-", e.prediction or "-")
    console.print(t)
    console.print(f"saved {cfg.CAMPAIGNS_DIR / rec.campaign_id / 'campaign.json'} - next: `chaos-agent analyze`")


@app.command()
def score(campaign_id: Optional[str] = typer.Option(None, "--campaign", help="Campaign id (part); default latest"),
          all_runs: bool = typer.Option(False, "--all", help="Score every saved run instead of one campaign")) -> None:
    """Deterministic facts and anchor scores per service (no AI) - the analyst's starting point."""
    from . import scoring
    g = refresh_graph(Kube())
    scope, results, _ = _scope_runs(campaign_id, all_runs)
    anchors = scoring.service_anchors(results, g)
    t = Table(title=f"Anchor scores ({scope}, {len(results)} runs) - overall {scoring.anchor_overall(anchors):.0f}")
    for col in ("service", "score", "grade", *scoring.WEIGHTS, "weight", "runs"):
        t.add_column(col)
    for svc, a in sorted(anchors.items(), key=lambda kv: kv[1]["score"]):
        d = a["dimensions"]
        t.add_row(svc, f"{a['score']:.0f}", a["grade"],
                  *(f"{d[k]:.0f}" if d[k] is not None else "-" for k in scoring.WEIGHTS),
                  f"{a['criticality']:g}", str(len(a["runs"])) if a["tested"] else "untested")
    console.print(t)
    console.print(f"coverage: {scoring.coverage(results)}")


@app.command()
def analyze(campaign_id: Optional[str] = typer.Option(None, "--campaign", help="Campaign id (part); default latest"),
            all_runs: bool = typer.Option(False, "--all", help="Analyse every saved run"),
            no_ai: bool = typer.Option(False, "--no-ai", help="Code anchors only (no analyst agent)")) -> None:
    """Analyst agent: ranked findings, fixes and the resilience score -> results/reports/<id>/analysis.json."""
    from .llm import analyst
    g = refresh_graph(Kube())
    base = _load_baseline()
    scope, results, _ = _scope_runs(campaign_id, all_runs)
    if not results:
        console.print("[red]no runs to analyse[/]")
        raise typer.Exit(1)
    console.print(f"analysing {len(results)} runs ({scope}) with {'code anchors only' if no_ai else _model()}...")
    rec = asyncio.run(analyst.analyze(results, g, base, scope, use_ai=not no_ai))
    path = analyst.save(rec)
    console.print(f"[bold]resilience score {rec.overall_score:.0f}/100 (grade {rec.grade})[/], "
                  f"code anchor {rec.overall_anchor:.0f}")
    for i, f in enumerate((rec.ai or {}).get("findings", []), 1):
        console.print(f"  F{i} {escape('[' + f['severity'].upper() + ']')} {escape(f['title'])}")
    if rec.guardrail.get("problems"):
        console.print(f"[yellow]guardrail: {len(rec.guardrail['problems'])} issue(s) after "
                      f"{rec.guardrail['attempts']} attempt(s)[/]")
        for prob in rec.guardrail["problems"]:
            console.print(f"[dim]  - {escape(prob)}[/]")
    if rec.guardrail.get("error"):
        console.print(f"[red]analyst error:[/] {rec.guardrail['error']}")
    console.print(f"saved {path} - next: `chaos-agent report`")


@app.command()
def report(report_id: Optional[str] = typer.Argument(None, help="Analysis id (part); default latest")) -> None:
    """Render the findings report (markdown) from an analysis -> results/reports/<id>/findings-report.md."""
    from . import campaign as campaign_mod
    from . import report as report_mod
    from .llm import analyst, planner
    from .llm.tools import load_runs
    try:
        a = analyst.load(report_id)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/] - run `chaos-agent analyze` first")
        raise typer.Exit(1)
    g = Graph.model_validate_json(cfg.GRAPH_FILE.read_text())
    runs = list(load_runs(a.run_ids).values())
    camp = plan_rec = None
    if a.scope != "all":
        try:
            camp = campaign_mod.load(a.scope)
            plan_rec = planner.load(camp.plan_id)
        except FileNotFoundError:
            pass
    md = report_mod.render(a, runs, g, _load_baseline(), plan_rec, camp)
    out = cfg.REPORTS_DIR / a.report_id / "findings-report.md"
    out.write_text(md)
    console.print(f"saved {out} ({len(md.splitlines())} lines) - copy to docs/findings-report.md in the repo")
