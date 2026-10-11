"""Settings: paths, namespaces, probe tuning, safety thresholds and hard caps.

Hard caps are the outer safety envelope. Nothing - CLI flags or (later) the LLM planner - can exceed them.
"""
from __future__ import annotations

import os
from pathlib import Path

# --- Cluster ---------------------------------------------------------------------------------------------
APP_NAMESPACE = "boutique"            # the only namespace chaos may target (Chaos Mesh allowlist agrees)
APP_LABEL = "app"                     # Online Boutique pods are labelled app=<deployment name>
ENTRYPOINT = "frontend"
CHECKOUT_ROOT = "checkoutservice"     # the revenue flow; its transitive dependencies are "checkout path"
TRAFFIC_WORKLOADS = {"loadgenerator"}  # generates traffic, never a chaos target
STATEFUL_WORKLOADS = {"redis-cart"}

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "chaos-agent"
RUN_ID_LABEL = "chaos-agent/run-id"

# --- Paths -----------------------------------------------------------------------------------------------
HOME = Path(os.environ.get("CHAOS_AGENT_HOME", Path.home() / "chaos-agent"))
RESULTS_DIR = Path(os.environ.get("CHAOS_AGENT_RESULTS", HOME / "results"))
GRAPH_FILE = RESULTS_DIR / "graph.json"
BASELINE_FILE = RESULTS_DIR / "baseline.json"
RUNS_DIR = RESULTS_DIR / "runs"

PLANS_DIR = RESULTS_DIR / "plans"
CAMPAIGNS_DIR = RESULTS_DIR / "campaigns"
AI_DIR = RESULTS_DIR / "ai"                     # transcript of every AI agent call
REPORTS_DIR = RESULTS_DIR / "reports"
ENV_FILE = HOME / ".env"                        # OPENAI_API_KEY etc. (chmod 600, never committed)

STATE_DIR = HOME / "state"                      # pid file of a running k6 load, campaign lock/stop flag
FRONTEND_URL = os.environ.get("FRONTEND_URL")  # default: the frontend Service's ClusterIP
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL")  # default: http://<node InternalIP>:30090
PROMETHEUS_NODEPORT = 30090
GRAFANA_NODEPORT = 30300

# --- Probe -----------------------------------------------------------------------------------------------
PROBE_USERS = 4            # virtual users, each with its own session cookie
PROBE_THINK_S = 1.0        # pause between a user's requests (~3-4 req/s in total at baseline)
PROBE_TIMEOUT_S = 5.0      # a request slower than this counts as an error
WINDOW_S = 5.0             # rolling window for error rate / percentiles
CHECK_INTERVAL_S = 1.0     # guard and timeline cadence

# --- Safety guard ----------------------------------------------------------------------------------------
GUARD_ERROR_RATE = 0.20          # abort when > 20 % of requests in the window fail
GUARD_P95_BASELINE_FACTOR = 3.0  # ... or p95 > max(3 x baseline p95, GUARD_P95_FLOOR_MS)
GUARD_P95_FLOOR_MS = 1500.0
GUARD_CONSECUTIVE = 2            # breaching checks in a row before aborting (ignores single spikes)
GUARD_UNRESPONSIVE_S = 3.0       # abort when no response at all arrives for this long
GUARD_MIN_SAMPLES = 3            # windows with fewer samples are not judged on error rate / p95

CAP_ERROR_RATE = 0.50            # per-experiment thresholds may be set up to these, never above
CAP_P95_MS = 5000.0

# --- Experiment limits -----------------------------------------------------------------------------------
MIN_DURATION_S = 10
MAX_DURATION_S = 180
PRE_WINDOW_S = 20                # healthy traffic observed before injecting
INJECT_CONFIRM_TIMEOUT_S = 20    # warn if Chaos Mesh hasn't reported AllInjected by then
DELETE_TIMEOUT_S = 30            # how long to wait for Chaos Mesh to revert and remove a resource

MAX_LOSS_PCT = 50                # network_loss cap
MAX_MEMORY_STRESS_MB = 256       # memory_stress cap per worker

# --- Traffic spike (k6)----------------------------------------------------------------------------------
K6_BIN = os.environ.get("K6_BIN", "k6")
MAX_LOAD_USERS = 500             # hard cap on concurrent virtual users (k6 shares the VM's 4 vCPUs with the app)
K6_STOP_TIMEOUT_S = 8            # SIGINT, then SIGKILL after this long

# --- Recovery --------------------------------------------------------------------------------------------
RECOVERY_ERROR_SLACK = 0.02      # steady = error rate <= baseline + 2 percentage points
RECOVERY_P95_FACTOR = 1.5        # ... and p95 <= max(1.5 x baseline p95, baseline p95 + slack)
RECOVERY_P95_SLACK_MS = 100.0
RECOVERY_STREAK = 3              # consecutive in-tolerance checks needed
RECOVERY_TIMEOUT_S = 180         # otherwise NOT_RECOVERED

# Pre-injection health gate (relative to baseline)
PRECHECK_ERROR_SLACK = 0.05
PRECHECK_P95_FACTOR = 2.0
PRECHECK_P95_SLACK_MS = 200.0

# --- User flows -> services the frontend calls for them (Online Boutique frontend handlers) ---------------
# Used for "reach": a service's reach is the share of these flows whose call chain (incl. transitive
# dependencies from the graph) includes it.
FLOW_CALLS = {
    "home": ["currencyservice", "productcatalogservice", "cartservice", "adservice"],
    "product": ["productcatalogservice", "currencyservice", "cartservice", "recommendationservice", "adservice"],
    "add_to_cart": ["productcatalogservice", "cartservice"],
    "view_cart": ["cartservice", "productcatalogservice", "currencyservice", "shippingservice",
                  "recommendationservice"],
    "checkout": ["checkoutservice", "currencyservice", "recommendationservice"],
}

# --- Impact classification (run facts, prediction checks) -------------------------------------------------
AFFECTED_ERROR_SLACK = 0.02      # a flow is "affected" if its error rate rises > 2 pp above baseline ...
AFFECTED_P95_FACTOR = 3.0        # ... or its p95 grows > 3x
AFFECTED_P95_MIN_MS = 50.0       # ... and by at least 50 ms (ignores noise on a 10 ms baseline)
ERROR_BAND_LOW = 0.02            # error band: none <= 2 % < low <= 20 % < high
ERROR_BAND_HIGH = 0.20
SLOWDOWN_MODERATE = 2.0          # slowdown band: none < 2x <= moderate <= 10x < severe
SLOWDOWN_SEVERE = 10.0

# --- AI policy (applies to AI-proposed experiments only, on top of the hard caps) --------------------------
AI_NO_OUTAGE_TARGETS = {"frontend"}            # no pod_kill / pod_failure on the entrypoint (= total outage)
AI_ERROR_FAULT_MAX_S = 60                      # pod_failure / network_loss at most 60 s
AI_MAX_LOAD_USERS = 300                        # Day 1: the guard stops ~285 users anyway
AI_MAX_EXPERIMENTS = 8

# --- Campaign --------------------------------------------------------------------------------------------
CAMPAIGN_SETTLE_TIMEOUT_S = 90   # between runs: wait until the probe is back within baseline tolerance
CAMPAIGN_MAX_INSERTS = 2         # follow-up experiments the adapter may add
BASELINE_MAX_AGE_H = 6           # older baseline -> re-measure before a campaign (--rebaseline forces it)

# --- AI models (override in ~/chaos-agent/.env) ----------------------------------------------------------
DEFAULT_MODEL = "gpt-6-luna"     # planner + analyst (available to the key per `chaos-agent ai-check`)
DEFAULT_MODEL_FAST = "gpt-6-luna"  # adapter (same model, lower reasoning effort)
DEFAULT_EFFORT = {"planner": "high", "analyst": "high", "adapter": "medium"}  # OPENAI_EFFORT_<AGENT> overrides
AI_TIMEOUT_S = 240               # one agent run (incl. tool calls)
AI_MAX_TURNS = 12
SCORE_TOLERANCE = 15             # AI rubric scores must be within +-15 of the code anchor
