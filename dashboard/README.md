# Chaos Lab dashboard

A demo UI for the AI-driven chaos engineering lab: inject a failure, watch the Online Boutique shop degrade or
crash in real time, see the safety guard stop it, and walk through the AI agents' plan, decisions and findings.

React 18 + Vite + plain CSS on the front end; a dependency-free Node server (`server/`) on the back end.
The server talks to the lab VM the same way `scripts/run.sh` does (ssh), so nothing new is installed on the VM.

## Run it

```bash
cd dashboard
npm install
npm run build        # once (and after editing client/)
npm start            # http://localhost:5050
```

Development with hot reload: `npm run dev` (UI on http://localhost:5173, API on 5050).

Needs Node 20+ and `infra/vm.env` (same file the scripts use: `VM_HOST`, `VM_USER`, `VM_KEY`). Without it the
dashboard still opens in **offline mode**: Replay, AI Agents (records) and Findings work from `./results`.

## The AI decides - nobody injects by hand

The dashboard has **no manual fault injection**. It only starts the agent commands, so the AI stays in charge:

1. **Let the AI plan** (2, 3 or 4 failures) -> `chaos-agent plan`: the planner ranks experiments from the dependency map, with a reason each.
   The dashboard checks the plan: every failure must be a **different type** and **visible to shoppers** (`server/visible.js`,
   thresholds taken from recorded runs). If not, it tells the planner what was wrong and asks again (up to 3 tries).
2. **Approve plan & run the campaign** -> `chaos-agent campaign --plan <id> --yes --max-experiments N`, then `analyze` and `report`:
   the failures run back to back under the deterministic safety guard, the adapter reacts after each run, the analyst scores.
3. **Autopilot** does steps 1-2 in one go. The plan itself is shown only in the **AI Agents** tab.

## Tabs

| Tab | What it shows | Data source |
|---|---|---|
| **Live Lab** | The real shop in a frame (fully interactive: add to cart, check out, order placed) with live flow tiles, error rate and p95, a banner naming what is failing; the AI pipeline; error, latency and CPU/memory-vs-limit charts of the attacked service with injection markers; live agent log, safety-guard status, service map | proxy to the shop, own probe, `kubectl` over ssh, Prometheus, agent output |
| **AI Agents** | Planner / adapter / analyst roles, the planner's ranked plan with reasons + dependency-map facts, campaign record (predicted vs observed, adapter decisions) | `results/plans`, `results/campaigns` |
| **Monitor** | Per-service CPU / memory vs limit, CPU throttling, restarts, VM CPU; link to the Grafana "Chaos Lab" dashboard | Prometheus (NodePort 30090) |
| **Replay** | Every recorded experiment with a play / scrub timeline, per-flow impact, event log (works offline) | `results/runs` |
| **Findings** | Resilience score, ranked findings with root cause + fix, per-service scores | `results/reports` |

The red **Kill switch** (top right) runs `chaos-agent abort` (also stops a campaign). Faults still revert by themselves at `duration`.

## How it works

- `server/probe.js` - two virtual shoppers walk home -> product -> add to cart -> view cart -> checkout against the
  shop (5 s timeout = error) and keep 1-second buckets; the browser polls `/api/health`.
- `server/cluster.js` - pods + active Chaos Mesh objects (one ssh call, cached 1.5 s) and Prometheus queries.
- `server/jobs.js` - runs a chain of agent commands over ssh (`plan`, or `campaign && analyze && report`), buffers output lines for polling,
  then copies `results/` back so Replay / Findings show it. One job at a time; a reloaded page re-attaches to it.
- `server/proxy.js` - serves the real shop on http://localhost:5051. The shop's session cookie has no SameSite attribute, so a browser will
  not send it in a cross-site frame (shop on the VM IP): the cart stayed empty and checkout failed. Through the proxy it is same-site.
- `server/visible.js` - what the planner is asked for and how its plan is checked.
- `server/vm.js` - ssh helpers; on Windows it also restricts the ssh key to the current
  user (OpenSSH refuses keys readable by "Users"/"Authenticated Users").
- The server listens on 127.0.0.1 only.

## API (JSON)

`GET /api/config | health | pods | metrics | graph | runs | runs/:id | campaigns | campaigns/:id | reports | reports/:id`
`POST /api/plan` (count 2-4, focus) - `POST /api/campaign` (planId or autopilot, count 2-4, noAdapt) - `GET /api/plans`
`GET /api/jobs/:id?from=N` - `POST /api/abort` - `POST /api/sync` (copy all results from the VM)

## Demo tips

1. **Live Lab**, "Show the real site" on. Press **1 · Let the AI plan** (about 80 s) and read the reasons it gives.
2. Press **2 · Approve plan & run the campaign**. Watch the flow tiles, the red zones on the charts and the service map; the
   guard stops a run that goes too far, and the campaign stops itself after two guard aborts in a row.
3. When the analyst finishes, open **Findings** (score, ranked fixes) and **Replay** (scrub any run).
4. Teammates share the VM: the dashboard refuses to start a job while one of its own is running, but check `chaos-agent status` if someone else might be injecting.
