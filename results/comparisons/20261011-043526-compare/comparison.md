# Fix-and-rerun comparison `20261011-043526-compare`

Flags compared: **cart-redundancy** (OFF arm = off, ON arm = on) · repeats: 1 · status: completed · started 2026-10-11T04:35:26+00:00

- `cart-redundancy` — cartservice 2 replicas + PodDisruptionBudget minAvailable 1 (fixes: F1 cartservice is a single point of failure)

Per-arm steady-state baseline (60 s, measured right before each arm): OFF p95 18 ms, errors 0.0%; ON p95 18 ms, errors 0.0%

## Per experiment (mean over repeats; OFF → ON)

### `pod_kill:cartservice:grace_period_s=0:30s`

| Metric | OFF | ON | Change |
|---|---|---|---|
| peak errors | 100.0% | 0.0% | better |
| errors (impact window) | 89.0% | 0.0% | better |
| availability | 11.0% | 100.0% | better |
| peak p95 | 1005 ms | 1048 ms | same |
| worst slowdown | 68.5x | 47.2x | better |
| recovery | 28.6 s | 2.1 s | better |
| replacement pod Ready | 22.6 s | 22.1 s | same |
| outcome | COMPLETED | COMPLETED | |

## Resilience score (code anchors, same rubric as the findings report)

| Service | OFF | ON | Change |
|---|---|---|---|
| cartservice | 35 | 82 | +47 |
| **overall (tested services)** | **35** | **82** | **+47** |

## Runs

| Arm | Repeat | Run | Status |
|---|---|---|---|
| OFF | 1 | `20261011-043646-pod_kill-cartservice` | COMPLETED |
| ON | 1 | `20261011-043931-pod_kill-cartservice` | COMPLETED |
