"""Safety guard: deterministic rules, no LLM. Pure functions over probe samples and pod state.

The runner calls `Guard.check()` once per second while a fault is active; when it returns a reason, the runner
deletes the chaos resource (Chaos Mesh then reverts the fault).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config as cfg
from .k8s import pod_app, pod_ready
from .models import Baseline, FlowStats, Thresholds
from .probe import Sample, stats


@dataclass
class Check:
    ts: float
    window: FlowStats
    breaches: list[str]


def evaluate(window: list[Sample], now: float, last_response: float, th: Thresholds,
             blast_radius: list[str] | None = None) -> Check:
    """All threshold breaches for one moment in time."""
    w = stats(window)
    breaches: list[str] = []
    if now - last_response > cfg.GUARD_UNRESPONSIVE_S:
        breaches.append(f"frontend unresponsive for {now - last_response:.1f}s")
    if w.count >= cfg.GUARD_MIN_SAMPLES:
        if w.error_rate > th.max_error_rate:
            breaches.append(f"error rate {w.error_rate:.0%} > {th.max_error_rate:.0%}")
        if w.p95_ms is not None and w.p95_ms > th.max_p95_ms:
            breaches.append(f"p95 {w.p95_ms:.0f}ms > {th.max_p95_ms:.0f}ms")
    breaches.extend(blast_radius or [])
    return Check(ts=now, window=w, breaches=breaches)


def blast_radius_issues(pods: list[dict], target: str) -> list[str]:
    """Faults are `mode: one` on `target`: any other NotReady pod, or >1 target pod down, is out of bounds."""
    issues = []
    down_target = 0
    for p in pods:
        if (p.get("metadata") or {}).get("deletionTimestamp"):
            continue                                     # terminating (e.g. the killed pod) - expected
        if pod_ready(p):
            continue
        if pod_app(p) == target:
            down_target += 1
        else:
            issues.append(f"blast radius: non-target pod {p['metadata']['name']} is NotReady")
    if down_target > 1:
        issues.append(f"blast radius: {down_target} {target} pods NotReady (expected at most 1)")
    return issues


@dataclass
class Guard:
    thresholds: Thresholds
    consecutive: int = cfg.GUARD_CONSECUTIVE
    streak: int = 0
    seen: list[str] = field(default_factory=list)       # every distinct breach observed (for the report)
    _kinds: set[str] = field(default_factory=set)

    def check(self, c: Check) -> str | None:
        """Feed one check; returns an abort reason after `consecutive` breaching checks in a row."""
        for b in c.breaches:
            kind = re.sub(r"[\d.]+", "#", b)            # same rule with different numbers = same breach
            if kind not in self._kinds:
                self._kinds.add(kind)
                self.seen.append(b)
        if not c.breaches:
            self.streak = 0
            return None
        self.streak += 1
        if self.streak >= self.consecutive:
            return "; ".join(c.breaches)
        return None


def within_tolerance(w: FlowStats, baseline: Baseline) -> bool:
    """Steady state: error rate and p95 back near the baseline."""
    if w.count < cfg.GUARD_MIN_SAMPLES:
        return False
    base = baseline.overall
    base_p95 = base.p95_ms or 0.0
    p95_limit = max(cfg.RECOVERY_P95_FACTOR * base_p95, base_p95 + cfg.RECOVERY_P95_SLACK_MS)
    return w.error_rate <= base.error_rate + cfg.RECOVERY_ERROR_SLACK and (w.p95_ms or 0.0) <= p95_limit


def precheck_ok(w: FlowStats, baseline: Baseline) -> tuple[bool, str]:
    """Is the app healthy enough to start an experiment?"""
    base = baseline.overall
    base_p95 = base.p95_ms or 0.0
    p95_limit = max(cfg.PRECHECK_P95_FACTOR * base_p95, base_p95 + cfg.PRECHECK_P95_SLACK_MS)
    if w.count < cfg.GUARD_MIN_SAMPLES:
        return False, f"only {w.count} probe samples in the pre-window"
    if w.error_rate > base.error_rate + cfg.PRECHECK_ERROR_SLACK:
        return False, f"error rate {w.error_rate:.0%} before injection (baseline {base.error_rate:.0%})"
    if (w.p95_ms or 0) > p95_limit:
        return False, f"p95 {w.p95_ms:.0f}ms before injection (limit {p95_limit:.0f}ms)"
    return True, "healthy"
