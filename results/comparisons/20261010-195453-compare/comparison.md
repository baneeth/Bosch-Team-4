# Fix-and-rerun comparison `20261010-195453-compare`

Flags compared: **cart-redundancy** (OFF arm = off, ON arm = on) · repeats: 1 · status: completed · started 2026-10-10T19:54:53+00:00

- `cart-redundancy` — cartservice 2 replicas + PodDisruptionBudget minAvailable 1 (fixes: F1 cartservice is a single point of failure)

Per-arm steady-state baseline (60 s, measured right before each arm): OFF p95 15 ms, errors 0.0%; ON p95 18 ms, errors 0.0%

## Per experiment (mean over repeats; OFF → ON)

### `traffic_spike:frontend:checkout_pct=20,ramp_s=30,think_ms=1000,users=100:120s+pod_kill:cartservice@45s`

| Metric | OFF | ON | Change |
|---|---|---|---|
| peak errors | 100.0% | 0.0% | better |
| errors (impact window) | 21.8% | 0.0% | better |
| availability | 78.2% | 100.0% | better |
| peak p95 | 5003 ms | 1629 ms | better |
| worst slowdown | 324.9x | 139.7x | better |
| recovery | 24.5 s | 0.0 s | better |
| replacement pod Ready | 22.9 s | 22.9 s | same |
| outcome | ABORTED (guard: frontend unresponsive for 4.5s) | ABORTED (guard: p95 1629ms > 1500ms) | |

## Resilience score (code anchors, same rubric as the findings report)

| Service | OFF | ON | Change |
|---|---|---|---|
| **overall (tested services)** | **0** | **0** | **+0** |

## Runs

| Arm | Repeat | Run | Status |
|---|---|---|---|
| OFF | 1 | `20261010-195613-combined-pod_kill-cartservice` | ABORTED |
| ON | 1 | `20261010-195945-combined-pod_kill-cartservice` | ABORTED |
