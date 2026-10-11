# AI-Driven Chaos Engineering for Cloud-Native Resilience

(Source: 4 slides in `Requirements/ChaosEngineering-1..4.jpeg`, Bosch Hackathon)

---

## Slide 1 — Overview

### Problem Statement
Build an AI system that intelligently introduces controlled failures into a cloud-native application environment and analyzes how the system responds to improve its resilience.

### Explanation in Detail
Chaos engineering means **deliberately breaking parts of a system, such as shutting down a server or slowing a network**, to see whether the system survives and recovers. Doing this by hand is slow and unfocused. Teams must build an AI that decides what to break, when, and how safely, then explains what the failure revealed and how to fix it.

Teams choose their own free, open-source, cloud-native sample application from a public GitHub repository and run it in their own sandbox.

### What a Team's Solution Should Do

1. **Select and understand a sample application**
   Pick any free, open-source, cloud-native application that can be built and deployed from a public GitHub repository. Map its services, dependencies, and data flows automatically or semi-automatically.

2. **Plan failure experiments intelligently**
   Choose the most informative or riskiest experiments first, based on the dependency map and observed behavior.

3. **Inject controlled failures with safety limits**
   Stop automatically if impact exceeds defined thresholds, such as error rate, latency or blast radius.

4. **Observe application behavior**
   Capture errors, slowdowns and recovery time.

5. **Analyze and report**
   Identify weaknesses, recommend fixes, and ideally provide a ranked resilience score.

---

## Slide 2 — Expected Outcome: Milestone 1 (Day 1)

*These are guidance checkpoints, not scoring gates.*

| Milestone | Expected Outcome |
|---|---|
| Setup and app selection | Sample app chosen and running end to end in the sandbox. |
| Application mapping | Services, dependencies and data flows mapped (manual corrections allowed). |
| Monitoring baseline | Normal error rate, latency and health captured under steady load. |
| First failure injection | At least one failure (e.g., container crash or network delay) triggered and observed. |
| Safety controls | Automatic stop or rollback on threshold breach, tested at least once. |
| Day-1 checkpoint | Short demo: app running, one safe failure injected, impact visible. |

**Day-1 minimum:** a running app, a dependency map, and one safe failure injection with a working kill switch.

---

## Slide 3 — Expected Outcome: Milestone 2 (Day 2)

*AI, analysis and submission*

| Milestone | Expected Outcome |
|---|---|
| AI experiment planner | AI ranks and selects experiments using the dependency map, with a short reason for each. |
| Multiple experiments | Three or more failure types run; errors, slowdowns and recovery time recorded. |
| Analysis and scoring | AI explains findings, ranks weaknesses by severity, recommends fixes, and ideally gives a resilience score. |
| Feature freeze and polish | No new features; fix bugs, finalize README, do a clean run from scratch. |
| Demo video and documents | Record the demo (3 minutes or less); finish architecture overview and findings report. |
| Submission | Complete team form, verify all five deliverables open correctly, submit (aim to finish 30 minutes early). |

**Day-2 minimum:** three or more experiments run, an AI-generated ranked findings report with fixes, and all five deliverables submitted.

---

## Slide 4 — Optional Stretch Goals and Deliverables and Rules

### Optional Stretch Goals

- **Re-run experiments after applying a fix** — Show improvement in system behavior after implementing the fix.
- **Live dashboard or simple UI** — Visualize experiments, metrics and findings in real time.
- **Combined failures** — Run multiple failures together (e.g., traffic spike while a service is down).

### Deliverables and Rules

Every team submits five deliverables by the end of the Hackathon through a single submission form or portal.

| # | Deliverable | Description |
|---|---|---|
| 1 | Source code | Public or shared repository link with a README covering setup and how to run. |
| 2 | Demo video | 3 minutes or less showing the system injecting a failure, observing the impact, and reporting findings. |
| 3 | Architecture overview | One page or slide: components, how the AI makes decisions, safety controls. |
| 4 | Findings report | Sample output from the sandbox application: weaknesses found, ranked, with recommended fixes. |
| 5 | Team form | Team name, members, tools and AI models used, third-party libraries and data used. |
