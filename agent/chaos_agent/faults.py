"""Fault handles: one interface over the two ways we break things.

- ChaosMeshFault: a Chaos Mesh resource (pod kill/failure, network delay/loss, CPU/memory stress). Remove = delete it.
- LoadFault: a k6 process flooding the frontend (traffic spike). Remove = stop the process.

Both end by themselves after `duration` (Chaos Mesh timer / k6 script length) - the safety net if the agent dies.
"""
from __future__ import annotations

import asyncio
import time

from . import config as cfg
from . import templates
from .k8s import Kube, chaos_state
from .loadgen import K6Process

# poll() states
PENDING, ACTIVE, FINISHED, GONE = "pending", "active", "finished", "gone"


class ChaosMeshFault:
    def __init__(self, kube: Kube, manifest: dict, instant: bool) -> None:
        self.kube = kube
        self.manifest = manifest
        self.kind = manifest["kind"]
        self.name = manifest["metadata"]["name"]
        self.label = f"{self.kind}/{self.name}"
        self.instant = instant

    async def dry_run(self) -> None:
        """Server-side validation of the manifest; raises on rejection, creates nothing."""
        await asyncio.to_thread(self.kube.create_chaos, self.manifest, True)

    async def inject(self) -> None:
        await asyncio.to_thread(self.kube.create_chaos, self.manifest)

    async def poll(self, now: float, deadline: float) -> tuple[str, str]:
        obj = await asyncio.to_thread(self.kube.get_chaos, self.kind, self.name)
        if obj is None:
            return GONE, "chaos resource was deleted externally"
        st = chaos_state(obj)
        if st["phase"] == "Stop" and st["recovered"]:
            return FINISHED, ""
        return (ACTIVE if st["injected"] else PENDING), ""

    async def remove(self) -> bool:
        await asyncio.to_thread(self.kube.delete_chaos, self.kind, self.name)
        return await asyncio.to_thread(self.kube.wait_chaos_gone, self.kind, self.name)

    def describe(self, elapsed_s: float) -> str:
        return ""


class LoadFault:
    instant = False

    def __init__(self, k6: K6Process, params: templates.TrafficSpikeParams) -> None:
        self.k6 = k6
        self.params = params
        self.label = f"k6 traffic spike ({params.users} users)"
        self.started = 0.0
        self.stopped_at_users: int | None = None

    async def dry_run(self) -> None:
        """k6 has no server-side validation; the script is generated from capped params."""

    async def inject(self) -> None:
        await asyncio.to_thread(self.k6.start)
        self.started = time.time()

    async def poll(self, now: float, deadline: float) -> tuple[str, str]:
        if self.k6.running():
            return ACTIVE, ""
        if now >= deadline - 1:
            return FINISHED, ""
        return GONE, f"load generator stopped before its scheduled end (exit code {self.k6.returncode})"

    async def remove(self) -> bool:
        return await asyncio.to_thread(self.k6.stop)

    def users_now(self) -> int:
        return templates.users_at(self.params, time.time() - self.started) if self.started else 0

    def describe(self, elapsed_s: float) -> str:
        return f"users≈{templates.users_at(self.params, elapsed_s)}" if self.k6.running() else "users=0"


async def wait_finished(fault, deadline: float, log) -> float:
    """Duration elapsed: wait until the fault has really ended (Chaos Mesh AllRecovered / k6 exited)."""
    limit = time.time() + 15
    while time.time() < limit:
        state, _ = await fault.poll(time.time(), deadline)
        if state in (FINISHED, GONE):
            log("duration elapsed - fault ended by itself" if isinstance(fault, LoadFault)
                else "duration elapsed - Chaos Mesh reverted the fault")
            return time.time()
        await asyncio.sleep(0.5)
    log("[yellow]fault has not reported its end; removing it[/]")
    await fault.remove()
    return time.time()
