# AI-Driven Chaos Engineering Lab

An AI system that **plans, injects and analyses controlled failures** in a cloud-native application, then produces a
**ranked findings report with fixes and a resilience score**.

- **Target app:** Google [Online Boutique](https://github.com/GoogleCloudPlatform/microservices-demo) v0.10.7 (11 gRPC microservices) on single-node k3s.
- **Chaos engine:** [Chaos Mesh](https://chaos-mesh.org) 2.8.4 for pod, network, CPU and memory faults; [k6](https://k6.io) for traffic spikes.
- **AI:** three bounded agents (planner, adapter, analyst) built with the OpenAI Agents SDK.
- **Safety:** a deterministic guard (plain code, not the LLM) aborts and rolls back any experiment that crosses a threshold.

Built for the Bosch hackathon, "AI-Driven Chaos Engineering for Cloud-Native Resilience".

---

## How it works

```
 map the app ──► AI plans experiments ──► inject with safety limits ──► observe ──► AI analyses, ranks, scores
 (graph)         (planner agent)          (guard + Chaos Mesh / k6)      (probe,      (analyst agent → report)
                                                  ▲                       Prometheus)
                                                  └── adapter agent reacts after every run
```

1. **Map.** `chaos-agent graph` builds a dependency map from the services' `*_ADDR` environment variables. It adds fan-in/out, risk and which user flows each service reaches.
2. **Baseline.** `chaos-agent baseline` records normal error rate and latency under steady load.
3. **Plan.** The **planner** agent ranks the most informative experiments from the map, baseline, fault catalog and past runs. It gives a reason and a prediction for each.
4. **Inject.** A **campaign** runs the plan one experiment at a time. Each experiment goes through validation, a Kubernetes server-side dry-run, a pre-check, 20 s of observation, injection, then a guard check every second. After that it reverts and waits for recovery.
5. **Adapt.** After each run the **adapter** agent decides: continue, modify the next experiment, insert a follow-up, or stop.
6. **Analyse.** The **analyst** agent writes findings ranked by severity, with root cause, a structured fix and a re-run command to verify it. It also scores every service on a fixed rubric.

### Fault types

| Type | Engine | What it does (hard caps in `templates.py`) |
|---|---|---|
| `pod_kill` | Chaos Mesh | Kill a pod |
| `pod_failure` | Chaos Mesh | Make a pod unavailable for a duration |
| `network_delay` | Chaos Mesh | Add latency to a service |
| `network_loss` | Chaos Mesh | Drop packets (≤ 50 %) |
| `cpu_stress` | Chaos Mesh | Burn CPU inside a pod |
| `memory_stress` | Chaos Mesh | Allocate memory (≤ 256 MB) |
| `traffic_spike` | k6 | Ramp up to N virtual users on the frontend (≤ 500) |

Durations are 10–180 s, and only one experiment runs at a time. Run `chaos-agent catalog` for the full list.

---

## Safety controls

| Control | How it works |
|---|---|
| **Deterministic guard** | Checked every second while a fault is active. It aborts if the error rate exceeds 20 %, or p95 exceeds max(3 × baseline, 1500 ms), or the frontend is silent for 3 s, or a non-target pod becomes NotReady. Two checks in a row are required. Per-run overrides can only go up to the hard caps (50 % / 5000 ms). |
| **Automatic rollback** | A breach deletes the chaos resource (or stops k6) and the fault reverts. A demo run was aborted 2 s after the breach, with the fault removed 0.5 s later. |
| **Auto-expiry** | Every fault carries a `duration`, so it ends on its own even if the agent dies. Tested by killing the agent mid-run with `kill -9`. |
| **Namespace allowlist** | Chaos Mesh may only act in the `boutique` namespace. |
| **Hard caps** | `templates.validate()` is the only path to a fault definition. CLI input and AI output both go through it. |
| **AI policy** | Extra rules for AI-proposed experiments: no pod kill/failure on the frontend, error-class faults ≤ 60 s, spikes ≤ 300 users. |
| **Kill switch** | `chaos-agent abort` removes all our chaos resources, stops k6 and stops a running campaign. |

### AI design rules
- The AI never runs `kubectl` or a shell. Its tools are read-only, plus `check_experiment` (a dry run of our validator).
- Every AI-proposed experiment is validated and dry-run before it runs. Rejected items are recorded.
- The AI may only make safety thresholds stricter, never looser.
- **Resilience score:** the AI scores each service on a fixed 5-dimension rubric (availability 35 %, latency 25 %, recovery 20 %, containment 10 %, posture 10 %). Code computes anchor scores, clamps the AI to ±15 of them, and combines the result. This keeps before/after comparisons fair.
- If the API is unavailable, a deterministic fallback planner takes over (and is labelled as such), the adapter is skipped, and the analyst falls back to the code anchors.
- Every AI call is saved in `results/ai/` (instructions, input, tool calls, output, tokens) so each decision can be audited.

---

## Repository layout (every file)

### Root
| File | What it is |
|---|---|
| `README.md` | This file. |
| `deployment-steps.md` | Detailed deployment guide: cloud VM, Multipass, native Ubuntu, troubleshooting, cleanup. |
| `.gitignore` | Keeps secrets, keys, local notes and build output out of git. |
| `.gitattributes` | Forces LF line endings on `.sh` files. |

### `agent/` — the Python agent (runs on the VM)
| File | What it is |
|---|---|
| `pyproject.toml` | Package metadata, dependencies and the `chaos-agent` command. |
| `chaos_agent/__init__.py` | Package marker with the version. |
| `chaos_agent/__main__.py` | Lets you run the agent with `python -m chaos_agent`. |
| `chaos_agent/cli.py` | All CLI commands (`graph`, `baseline`, `run`, `plan`, `campaign`, `analyze`, `report`, …). |
| `chaos_agent/config.py` | Every threshold, hard cap, path, flow-to-service map, AI policy and model setting. |
| `chaos_agent/models.py` | Pydantic data types for experiments, results, graph, baseline, plans, campaigns and analyses. |
| `chaos_agent/k8s.py` | Thin Kubernetes wrapper, including the server-side dry-run of chaos resources. |
| `chaos_agent/graph.py` | Builds the dependency map from `*_ADDR` env vars and adds risk, reach and a Mermaid diagram. |
| `chaos_agent/probe.py` | Virtual shoppers that walk five user flows and measure error rate and latency. |
| `chaos_agent/prom.py` | Reads per-service CPU, throttling and memory from Prometheus. |
| `chaos_agent/templates.py` | The only way to create a fault: typed parameters with hard caps, rendered to Chaos Mesh or k6. |
| `chaos_agent/faults.py` | One inject/poll/remove interface for Chaos Mesh faults and k6 load. |
| `chaos_agent/loadgen.py` | Starts, stops and summarises the k6 traffic-spike process. |
| `chaos_agent/policy.py` | Extra rules that apply only to AI-proposed experiments. |
| `chaos_agent/guard.py` | Deterministic safety rules that decide when to abort (no LLM). |
| `chaos_agent/runner.py` | One experiment's lifecycle: pre-check, observe, inject, guard, revert, recovery, result files. |
| `chaos_agent/campaign.py` | Runs a plan's experiments in turn, calls the adapter, applies the stop rules. |
| `chaos_agent/scoring.py` | Pure functions: affected flows, prediction verdicts, rubric anchor scores, grades. |
| `chaos_agent/fixes.py` | Structured fix actions (replicas, resources, PDB, HPA, probes, code change) rendered to kubectl/YAML. |
| `chaos_agent/report.py` | Renders the markdown findings report; numbers come from code, AI text is labelled. |
| `chaos_agent/grafana.py` | Creates the "Chaos Lab" Grafana dashboard and one annotation per run. |
| `chaos_agent/llm/__init__.py` | Package marker for the AI agents. |
| `chaos_agent/llm/client.py` | OpenAI Agents SDK plumbing: settings, bounded runs, strict schemas, transcripts. |
| `chaos_agent/llm/schemas.py` | Structured-output models the AI fills in; experiment types are generated from `templates.py`. |
| `chaos_agent/llm/tools.py` | Read-only tools for the agents (plus the `check_experiment` dry run) and compact data views. |
| `chaos_agent/llm/planner.py` | Planner agent that ranks experiments, plus the deterministic fallback planner. |
| `chaos_agent/llm/adapter.py` | Adapter agent that decides continue / modify / follow-up / stop after each run. |
| `chaos_agent/llm/analyst.py` | Analyst agent: ranked findings, fixes, per-service scores, and the guardrail that checks them. |
| `tests/conftest.py` | Shared test fixtures. |
| `tests/test_ai.py` | AI flow tests with the API stubbed. |
| `tests/test_campaign.py` | Campaign loop and stop-rule tests. |
| `tests/test_grafana.py` | Grafana dashboard tests. |
| `tests/test_graph.py` | Dependency-map tests. |
| `tests/test_guard.py` | Safety guard tests on synthetic samples. |
| `tests/test_load.py` | k6 load-generator tests. |
| `tests/test_policy.py` | AI policy tests. |
| `tests/test_scoring.py` | Scoring and prediction tests. |
| `tests/test_templates.py` | Fault manifest and cap tests. |

### `dashboard/` — demo UI (React + Vite, small Node server)
| File | What it is |
|---|---|
| `README.md` | How to run the dashboard, its tabs and API. |
| `package.json`, `package-lock.json` | Node dependencies and scripts. |
| `dev.js` | Runs the API server and the Vite dev server together. |
| `server/index.js` | HTTP server: serves the built app and the JSON API. |
| `server/vm.js` | ssh helpers, input validation and the parameter allowlist that mirrors `templates.py`. |
| `server/jobs.js` | Runs the plan → campaign → analyze → report jobs and buffers their output. |
| `server/visible.js` | Checks that a plan's failures are different and visible to shoppers. |
| `server/cluster.js` | Live pods and active chaos objects, plus Prometheus queries. |
| `server/probe.js` | The dashboard's own storefront monitor with 1-second buckets. |
| `server/proxy.js` | Serves the shop on localhost so the cart cookie works inside the frame. |
| `server/results.js` | Read-only access to `./results` for replay and findings. |
| `client/index.html`, `client/vite.config.js` | Page shell and Vite build config. |
| `client/src/main.jsx`, `App.jsx` | App entry point and layout with the tabs. |
| `client/src/api.js`, `live.js`, `meta.js` | API calls, live-data polling and labels/constants. |
| `client/src/styles.css` | The theme and all styling. |
| `client/src/views/LiveLab.jsx` | Live tab: shop frame, flow tiles, charts, AI pipeline, kill switch. |
| `client/src/views/Campaign.jsx` | AI Agents tab: plan with reasons, campaign record, predictions vs observed. |
| `client/src/views/Monitor.jsx` | Per-service CPU/memory, throttling, restarts, VM CPU. |
| `client/src/views/Replay.jsx` | Play back any recorded experiment. |
| `client/src/views/Findings.jsx` | Resilience score and ranked findings. |
| `client/src/components/*.jsx` | Reusable parts: `LineChart`, `Topology`, `Storefront`, `Terminal`, `PlanView`, `PipelinePanel`, `ResourceCard`. |

### `infra/` and `scripts/`
| File | What it is |
|---|---|
| `infra/vm-bootstrap.sh` | One idempotent script that installs k3s, Helm, the shop, Chaos Mesh, Prometheus/Grafana and k6, then runs 13 smoke tests. |
| `infra/vm.env.example` | Template for `infra/vm.env` (VM host, user, key path); the real file is gitignored. |
| `scripts/deploy.sh` | Copies `agent/` to the VM and installs it in a venv. |
| `scripts/run.sh` | Runs a `chaos-agent` command on the VM; also `test` and `pull-results`. |

### `results/`, `docs/`, `Requirements/` — evidence and brief
| File | What it is |
|---|---|
| `results/README.md` | Index of every recorded run and what it showed. |
| `results/baseline.json` | Steady-state error rate and latency. |
| `results/graph.json`, `results/graph.mmd` | The dependency map as data and as a Mermaid diagram. |
| `results/runs/<id>/` | One folder per experiment: `spec.json`, `result.json`, `timeline.csv`, `samples.csv`, `events.log` (+ k6 files for spikes). |
| `results/plans/`, `results/campaigns/`, `results/reports/` | AI plans, campaign records, and analyses with their markdown reports. |
| `results/comparisons/` | Side-by-side comparisons of recorded runs (`comparison.json` / `comparison.md`). |
| `results/ai/` | One transcript per AI call (instructions, input, tool calls, output, tokens). |
| `docs/findings-report.md` | The AI-generated findings report (61/100, grade D). |
| `docs/images/chaos-lab-dashboard.png` | Screenshot of the Grafana "Chaos Lab" dashboard. |
| `Requirements/ChaosEngineeringRequirements.md` | The hackathon brief. |
| `Requirements/images-ignore-this/` | The brief's original slide images. |

---

## Quick start

You need an Ubuntu 22.04 / 24.04 / 26.04 machine (x86_64, 4 vCPU, 12–16 GB RAM, 40–50 GB disk). A cloud VM is recommended.
The full guide, including firewall ports, options and troubleshooting, is in **[deployment-steps.md](deployment-steps.md)**.

### 1. Set up the VM (~15 min, idempotent)

```bash
scp -i <key.pem> infra/vm-bootstrap.sh ubuntu@<VM_IP>:~
ssh -i <key.pem> ubuntu@<VM_IP>
sudo GRAFANA_ADMIN_PASSWORD='<choose-a-password>' ./vm-bootstrap.sh
```

This installs k3s, Helm, Online Boutique, Chaos Mesh, Prometheus and Grafana, and k6. It ends with 13 smoke tests and must print `All smoke tests passed`.

| Service | Port |
|---|---|
| Shop | 30080 |
| Grafana | 30300 |
| Prometheus | 30090 |

### 2. Install the agent

From your laptop (Linux, macOS or Git Bash):

```bash
cp infra/vm.env.example infra/vm.env     # set VM_HOST, VM_USER, VM_KEY (file is gitignored)
scripts/deploy.sh                        # copies agent/ to the VM and installs it in a venv
scripts/run.sh test                      # unit tests on the VM (AI calls are stubbed)
```

### 3. Configure the AI

Create `~/chaos-agent/.env` **on the VM** (it is gitignored; `chmod 600` it):

```ini
OPENAI_API_KEY=<your key>
OPENAI_MODEL=<model for planner and analyst>
OPENAI_MODEL_FAST=<model for the adapter>
OPENAI_EFFORT_PLANNER=high
OPENAI_EFFORT_ADAPTER=medium
OPENAI_EFFORT_ANALYST=high
```

Then check it with `scripts/run.sh ai-check`. Without a key, use `--fallback` on `plan` and `campaign`, and `--no-ai` on `analyze`.

### 4. Run it

```bash
scripts/run.sh graph                 # dependency map
scripts/run.sh baseline              # normal behaviour (~2.5 min)
scripts/run.sh grafana               # create the "Chaos Lab" dashboard
scripts/run.sh plan                  # AI plans experiments
scripts/run.sh campaign --new --yes  # plan + run the campaign under the guard
scripts/run.sh analyze               # AI findings + resilience score
scripts/run.sh report                # markdown report in results/reports/<id>/
scripts/run.sh pull-results          # copy results back to ./results
```

A single manual experiment:

```bash
scripts/run.sh run --type network_delay --target productcatalogservice -p latency_ms=100 --duration 60
```

`run` exits 0 = COMPLETED, 2 = ABORTED (guard or kill switch), 1 = other (refused, not recovered, pre-check failed).

### CLI reference

| Command | Purpose |
|---|---|
| `graph`, `baseline`, `catalog` | Dependency map, steady state, fault types and limits |
| `run`, `status`, `abort` | One guarded experiment, current state, emergency stop |
| `runs`, `show [baseline\|latest\|<id>]` | Browse recorded runs |
| `ai-check` | Verify the API key and the models available |
| `plan [--max-experiments N] [--focus …] [--fallback]` | AI experiment plan |
| `campaign [--plan <id>\|--new] [--yes] [--no-adapt] [--max-experiments N] [--rebaseline] [--fallback]` | Run a plan |
| `score`, `analyze`, `report` | Score runs, AI analysis, markdown report |
| `grafana` | Create the Grafana dashboard |

### Dashboard 

A demo UI to watch the shop degrade in real time, with the guard, the AI plan, the replay and the findings. It has no manual fault injection; the AI decides.

```bash
cd dashboard && npm install && npm run build && npm start     # http://localhost:5050
```

Needs Node 20+ and `infra/vm.env`. It also works in offline mode, replaying recorded runs from `./results`. Details are in [dashboard/README.md](dashboard/README.md).

---

## Results

An AI analysis of 41 recorded experiments scored the shop **61 / 100 (grade D)**. Full report: **[docs/findings-report.md](docs/findings-report.md)**. Evidence index: [results/README.md](results/README.md).

| # | Severity | Finding | Suggested fix |
|---|---|---|---|
| F1 | Critical | Losing cartservice fails all shopping flows (100 % peak errors, 31–42 s recovery) | Run 2 replicas |
| F2 | High | Product catalog pod loss breaks core flows and spreads to a dependent pod | Run 2 replicas |
| F3 | High | Frontend 0.2-CPU limit throttles under load (500 users: p95 ≈ 4 s, 0 errors) | Raise the CPU limit |
| F4 | High | A Redis cart outage leaves the frontend unresponsive for seconds | Add a short deadline on cart calls |
| F5 | High | Delay and packet loss on RPC paths are amplified (+100 ms on the catalog gives ≈ 70× p95) | Add timeouts and a request budget |
| F6 | High | paymentservice outage fails 83 % of checkouts | Run 2 replicas |

Other observations:
- Under load the shop fails slowly (queueing), not with errors.
- 20 % packet loss hurts more than +100 ms delay.
- emailservice and checkoutservice outages were invisible to shoppers.

Baseline: 0 % errors, p50 9 ms, p95 13 ms.

---

## Tech stack

| Area | Tools |
|---|---|
| Orchestration | k3s v1.35, Helm |
| Target app | Online Boutique v0.10.7 |
| Chaos / load | Chaos Mesh 2.8.4, k6 v2.3.0 |
| Monitoring | kube-prometheus-stack 92.2.0 (Prometheus, Grafana) |
| Agent | Python (tested on 3.14): `kubernetes`, `httpx`, `pydantic`, `typer`, `rich`, `pytest`, `openai-agents`, `python-dotenv` |
| AI | OpenAI Agents SDK, with structured outputs mapped to pydantic models |
| Dashboard | React 18, Vite, Node 20+ (no server dependencies) |

---

## Security notes

- **Nothing secret is stored in this repository.** The API key, VM address, SSH key, kubeconfig and passwords live only in gitignored files (`.env`, `infra/vm.env`, `*.pem`, `kubeconfig*`).
- Never commit those files. Before pushing, run `git status` and `git grep -nE 'sk-|BEGIN .*PRIVATE KEY'` to check.
- Chaos Mesh is limited to the `boutique` namespace. Do not annotate other namespaces with `chaos-mesh.org/inject=enabled`.
- Treat the VM as disposable lab infrastructure and terminate it when you are done.
- 
## Team Members:
| Lateesha Bhatia 
| Preethana Couppoussamy 
