# Fix-and-rerun comparison `20261010-183427-compare`

Flags compared: **cart-redundancy** (OFF arm = off, ON arm = on) · repeats: 1 · status: completed · started 2026-10-10T18:34:27+00:00

- `cart-redundancy` — cartservice 2 replicas + PodDisruptionBudget minAvailable 1 (fixes: F1 cartservice is a single point of failure)

Per-arm steady-state baseline (60 s, measured right before each arm): OFF p95 13 ms, errors 0.0%; ON p95 14 ms, errors 0.0%

## Per experiment (mean over repeats; OFF → ON)

### `pod_kill:cartservice:grace_period_s=0:30s`

| Metric | OFF | ON | Change |
|---|---|---|---|
| peak errors | 100.0% | 0.0% | better |
| errors (impact window) | 83.3% | 0.0% | better |
| availability | 16.7% | 100.0% | better |
| peak p95 | 792 ms | 28 ms | better |
| worst slowdown | 56.2x | 2.3x | better |
| recovery | 31.2 s | 0.0 s | better |
| replacement pod Ready | 23.1 s | 22.1 s | same |
| outcome | COMPLETED | COMPLETED | |

## Resilience score (code anchors, same rubric as the findings report)

| Service | OFF | ON | Change |
|---|---|---|---|
| cartservice | 35 | 98 | +63 |
| **overall (tested services)** | **35** | **98** | **+63** |

## Runs

| Arm | Repeat | Run | Status |
|---|---|---|---|
| OFF | 1 | `20261010-183547-pod_kill-cartservice` | COMPLETED |
| ON | 1 | `20261010-183834-pod_kill-cartservice` | COMPLETED |
