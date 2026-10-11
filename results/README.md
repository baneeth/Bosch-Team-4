# Results (evidence)

Produced by `chaos-agent` on the VM and copied here with `scripts/run.sh pull-results`.

| File | What |
|---|---|
| `graph.json` / `graph.mmd` | Dependency map (12 workloads, 17 edges, 1 dangling) — Mermaid renders on GitHub |
| `baseline.json` | Steady state: 475 requests / 120 s, 0 % errors, p50 9 ms, p95 13 ms |
| `runs/<id>/` | One folder per experiment: `spec.json` (request + Chaos Mesh manifest), `result.json`, `timeline.csv` (1 s guard checks), `samples.csv` (every probe request), `events.log` |

## Day-1 runs

| Run | Experiment | Outcome |
|---|---|---|
| `20261010-073737-network_delay-productcatalogservice` | +100 ms on productcatalogservice, 60 s | COMPLETED — product/cart pages 10 ms → ~712 ms (~70×), checkout 17 → 718 ms, 0 errors, recovery 0 s |
| `20261010-073913-network_delay-productcatalogservice` | +500 ms, guard p95 limit 300 ms | **ABORTED by the guard** 2 s after the first breach; fault removed 0.5 s after the decision; active only 2.5 s; recovery 5 s |
| `20261010-074007-cpu_stress-cartservice` | CPU stress 1×80 % on cartservice, 120 s | **ABORTED by the manual kill switch** (`chaos-agent abort` from a second shell) after 16 s; p95 13 → 82 ms, 0 errors |
| `20261010-074206-pod_kill-adservice` | Kill the adservice pod | COMPLETED — no user-visible errors (frontend degrades gracefully without ads); replacement pod Ready after 32.5 s |

## Traffic spikes (many users, k6) — resource exhaustion

Load folders also contain `load.js` (the generated k6 script), `k6.log` and `k6-summary.json`; every run's
`result.json` now includes per-service CPU/memory/throttling from Prometheus (`resources`).

| Run | Load | Outcome |
|---|---|---|
| `20261010-080318-traffic_spike-frontend` | ramp to 100 users in 30 s, 90 s | COMPLETED — 0 errors, 72.6 req/s; home p95 12 → 485 ms; **frontend CPU 98 % of its limit, throttled 97 %**; VM CPU only 31 % |
| `20261010-080539-traffic_spike-frontend` | ramp to 300 users in 60 s, default guard | **ABORTED by the guard at ~285 users** (p95 1686 ms > 1500 ms); load stopped, back to normal 1 s later |
| `20261010-080723-traffic_spike-frontend` | 50 users | **ABORTED by the manual kill switch** (`chaos-agent abort` from a second shell stopped k6 in 0.4 s) |
| `20261010-080822-traffic_spike-frontend` | ramp to 500 users, guard at hard caps (5 s / 50 %) | COMPLETED — still **0 errors** but p95 3–4 s (home 318× slower); frontend 100 % of CPU limit, throttled 100 %; it fails *slow*, not with errors |

Crash safety (no result folder, the agent was killed on purpose): `kill -9` of the agent during a +100 ms delay —
Chaos Mesh still reverted the fault at its 30 s `duration` (product page 709 ms → 6 ms), and `chaos-agent abort`
removed the finished leftover resource.

## Day 2 — new fault types, campaign, scoring, report

Every run now also records `impact_error_rate` / `impact_p95_ms` / `availability`, `affected_flows`, a per-second
`health` label in `timeline.csv` (OK / IMPACTED / BREACH / RECOVERING) and, for planned runs, a `prediction_check`
(confirmed / partly / refuted). Every Chaos Mesh fault passes a Kubernetes server-side dry-run before injection.

| Run | Experiment | Outcome |
|---|---|---|
| `20261010-170944-pod_failure-paymentservice` | paymentservice unavailable 30 s | COMPLETED — **checkout 83 % errors**, other flows unaffected (graceful), recovery 0 s, pod restarted twice |
| `20261010-171055-network_loss-cartservice` | 20 % packet loss on cartservice | **ABORTED by the guard after 6.6 s** (p95 1564 ms) — 0 errors but cart pages 4 → 1378 ms (TCP retransmits); worse than +100 ms delay |
| `20261010-171142-memory_stress-productcatalogservice` | 64 MB memory stress | COMPLETED — memory 9 → 72/128 MiB, no user impact |

**Campaign `campaigns/20261010-171530-campaign/`** — deterministic fallback plan `plans/20261010-171249-plan.json`
(no API key yet), adapter off, 4 experiments covering 4 failure classes:

| Run | Experiment | Outcome | Prediction |
|---|---|---|---|
| `20261010-171530-pod_failure-productcatalogservice` | catalog down 30 s | **ABORTED by the guard** (60 % errors) after 3.6 s; recovery 2 s | confirmed |
| `20261010-171613-network_delay-productcatalogservice` | +100 ms, 45 s | COMPLETED — p95 757 ms (~70×), 0 errors (amplification reproduced) | confirmed |
| `20261010-171736-traffic_spike-frontend` | 150 users | COMPLETED — p95 1094 ms, 0 errors | confirmed |
| `20261010-171948-pod_kill-cartservice` | kill the cart pod | COMPLETED — **100 % peak errors** until the replacement was Ready (24 s); recovery 31 s | partly (errors worse than predicted) |

Analysis without AI (`analyze --no-ai`, code anchors): `reports/20261010-172113-report/` — resilience score
**47/100 (F)**; worst services cartservice 33, productcatalogservice 39; frontend 74.

### AI runs (OpenAI Agents SDK, model `gpt-6-luna`; effort planner/analyst high, adapter medium)

Every AI call is saved in `ai/` (instructions, input, tool calls, output, tokens).

- `ai/20261010-172705-ai-check.json` — key + model check (structured call in ~3 s).
- **Planner** `plans/20261010-172915-plan.json` (84 s, ~40k tokens): 6 ranked experiments, each self-checked with
  `check_experiment`, 0 rejected; skipped repeats of known results (+100 ms catalog delay, 300-user spike) and
  frontend outages (policy).
- **Campaign 1** `campaigns/20261010-172943-campaign/`: redis-cart `pod_failure` → **frontend unresponsive 4.2 s**
  (guard abort, home 100 % errors, p95 5 s); cartservice `memory_stress` 110 MB → OOM restart, 97 % impact errors,
  recovery 42 s (guard abort; prediction confirmed). The adapter then stopped the campaign because the last timeline
  samples still said RECOVERING → fixed: code now tells the adapter the shop is verified healthy (and stops the
  campaign itself if it isn't).
- **Campaign 2** `campaigns/20261010-173550-campaign/` (`campaign --new`): 4 planned + 1 adapter follow-up
  (shippingservice delay 40 ms → 20 ms dose-response); emailservice/checkoutservice outages invisible to users;
  adapter stopped once coverage was complete.
- **Analyst** `reports/20261010-174507-report/` over all 41 runs (160 s, ~107k input tokens, guardrail passed on
  attempt 1): **resilience score 61/100 (D)**, code anchor 62; 6 findings (1 critical, 5 high) with fixes →
  copied to `docs/findings-report.md`.
