# Fix-and-rerun comparison `20261010-184127-compare`

Flags compared: **cart-redundancy, catalog-redundancy, frontend-capacity, payment-redundancy** (OFF arm = off, ON arm = on) · repeats: 1 · status: completed · started 2026-10-10T18:41:27+00:00

- `cart-redundancy` — cartservice 2 replicas + PodDisruptionBudget minAvailable 1 (fixes: F1 cartservice is a single point of failure)
- `catalog-redundancy` — productcatalogservice 2 replicas + PodDisruptionBudget minAvailable 1 (fixes: F2 productcatalogservice loss breaks core flows)
- `frontend-capacity` — frontend CPU limit 200m -> 800m (request unchanged) (fixes: F3 frontend CPU throttling under load)
- `payment-redundancy` — paymentservice 2 replicas + PodDisruptionBudget minAvailable 1 (fixes: F6 paymentservice down makes checkout fail)

Per-arm steady-state baseline (60 s, measured right before each arm): OFF p95 13 ms, errors 0.0%; ON p95 13 ms, errors 0.0%

## Per experiment (mean over repeats; OFF → ON)

### `pod_kill:cartservice:grace_period_s=0:30s`

| Metric | OFF | ON | Change |
|---|---|---|---|
| peak errors | 100.0% | 0.0% | better |
| errors (impact window) | 92.7% | 0.0% | better |
| availability | 7.3% | 100.0% | better |
| peak p95 | 1093 ms | 14 ms | better |
| worst slowdown | 85.0x | 1.2x | better |
| recovery | 28.2 s | 0.0 s | better |
| replacement pod Ready | 23.2 s | 23.1 s | same |
| outcome | COMPLETED | COMPLETED | |

### `pod_kill:productcatalogservice:grace_period_s=0:30s`

| Metric | OFF | ON | Change |
|---|---|---|---|
| peak errors | 100.0% | 0.0% | better |
| errors (impact window) | 33.3% | 0.0% | better |
| availability | 66.7% | 100.0% | better |
| peak p95 | 5003 ms | 17 ms | better |
| worst slowdown | 450.7x | 1.8x | better |
| recovery | 23.6 s | 0.0 s | better |
| replacement pod Ready | 1.6 s | 2.1 s | worse |
| outcome | COMPLETED | COMPLETED | |

### `traffic_spike:frontend:checkout_pct=20,ramp_s=30,think_ms=1000,users=150:90s`

| Metric | OFF | ON | Change |
|---|---|---|---|
| peak errors | 0.0% | 0.0% | same |
| errors (impact window) | 0.0% | 0.0% | same |
| availability | 100.0% | 100.0% | same |
| peak p95 | 1157 ms | 1147 ms | same |
| worst slowdown | 66.2x | 71.0x | worse |
| recovery | 1.0 s | 2.0 s | worse |
| outcome | COMPLETED | COMPLETED | |

### `pod_failure:paymentservice::30s`

| Metric | OFF | ON | Change |
|---|---|---|---|
| peak errors | 21.1% | 0.0% | better |
| errors (impact window) | 14.8% | 0.0% | better |
| availability | 85.2% | 100.0% | better |
| peak p95 | 16 ms | 17 ms | worse |
| worst slowdown | 1.4x | 1.4x | same |
| recovery | 2.0 s | 0.0 s | better |
| outcome | ABORTED (guard: error rate 21% > 20%) | COMPLETED | |

## Resilience score (code anchors, same rubric as the findings report)

| Service | OFF | ON | Change |
|---|---|---|---|
| cartservice | 34 | 100 | +66 |
| frontend | 74 | 75 | +0 |
| paymentservice | 77 | 100 | +23 |
| productcatalogservice | 25 | 99 | +74 |
| **overall (tested services)** | **48** | **94** | **+45** |

## Runs

| Arm | Repeat | Run | Status |
|---|---|---|---|
| OFF | 1 | `20261010-184247-pod_kill-cartservice` | COMPLETED |
| OFF | 1 | `20261010-184354-pod_kill-productcatalogservice` | COMPLETED |
| OFF | 1 | `20261010-184456-traffic_spike-frontend` | COMPLETED |
| OFF | 1 | `20261010-184709-pod_failure-paymentservice` | ABORTED |
| ON | 1 | `20261010-184948-pod_kill-cartservice` | COMPLETED |
| ON | 1 | `20261010-185042-pod_kill-productcatalogservice` | COMPLETED |
| ON | 1 | `20261010-185116-traffic_spike-frontend` | COMPLETED |
| ON | 1 | `20261010-185330-pod_failure-paymentservice` | COMPLETED |
