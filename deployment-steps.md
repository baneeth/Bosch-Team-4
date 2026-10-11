# Deployment steps

How to deploy the **AI-driven chaos engineering lab** yourself: on a cloud VM (recommended) or on your own
laptop. It is one Ubuntu machine running everything; the whole setup is automated by one script.

> No passwords or keys are stored in this repository. You choose your own passwords during setup (step 3).

---

## 1. What gets deployed

```
 One Ubuntu machine (cloud VM or local VM)
 ├── k3s                    single-node Kubernetes
 ├── Online Boutique        demo shop: 11 microservices + a background traffic generator   (namespace: boutique)
 ├── Chaos Mesh             injects faults - allowed ONLY in namespace "boutique"           (namespace: chaos-mesh)
 ├── Prometheus + Grafana   metrics and dashboards (kube-prometheus-stack)                  (namespace: monitoring)
 ├── k6                     load generator for "many users" experiments
 └── chaos-agent            our Python tool: dependency map, baseline, guarded experiments, results
```

| Component | Version |
|---|---|
| k3s (Kubernetes) | v1.35.9+k3s1 |
| Helm | v3.22.0 |
| Online Boutique | v0.10.7 |
| Chaos Mesh | 2.8.4 |
| kube-prometheus-stack | 92.2.0 |
| k6 | v2.3.0 |
| chaos-agent | Python ≥ 3.11 (tested on 3.14) |

All versions are pinned in `infra/vm-bootstrap.sh` and can be overridden with environment variables (step 3).

| Service | Port on the machine |
|---|---|
| Online Boutique (shop) | 30080 |
| Grafana | 30300 |
| Prometheus | 30090 |
| Kubernetes API (optional, for kubectl from your laptop) | 6443 |
| SSH | 22 |

---

## 2. Choose where to run it

| Option | Best for | Tested |
|---|---|---|
| **A. Cloud VM** (AWS, Azure, GCP, Hetzner, Hostinger, DigitalOcean, …) | Teams, demos, laptops with < 32 GB RAM | ✅ AWS EC2, Ubuntu 26.04, x86_64 |
| **B. Local VM with Multipass** (Windows / macOS / Linux) | Working offline / no cloud account | ⚠️ not tested yet (same script, should work) |
| **C. Native Ubuntu laptop / desktop** | Linux users | ⚠️ not tested yet (same script) |
| WSL2 on Windows | — | ❌ not recommended (needs systemd; network-fault support depends on the WSL kernel) |

**Machine requirements (all options):**

| | Minimum | Recommended |
|---|---|---|
| CPU | 4 vCPU | 4 vCPU |
| RAM | 12 GB | 16 GB |
| Disk | 40 GB | 50 GB SSD |
| OS | Ubuntu 22.04 / 24.04 / 26.04 | Ubuntu 24.04 LTS or 26.04 |
| CPU architecture | x86_64 (amd64) | x86_64 — ARM64 (e.g. Apple Silicon, AWS Graviton) is untested |

At rest the whole stack uses ~10 % CPU and ~2.5 GB RAM on a 4 vCPU / 16 GB machine; load experiments use more.

---

## 3A. Option A — Cloud VM (recommended)

### Step 1 — Create the VM

Example for **AWS EC2** (other clouds: same sizes and ports):

1. **AMI:** Ubuntu Server 24.04 LTS or 26.04 (x86_64).
2. **Instance type:** 4 vCPU / 16 GB, e.g. `m6a.xlarge`, `m6i.xlarge`, `t3.xlarge`
   (Azure `Standard_D4s_v5`, GCP `e2-standard-4`, Hetzner `CX42`).
3. **Storage:** 50 GB gp3.
4. **Key pair:** create or choose one, and download the `.pem` file. **Never commit it to git.**
5. **Security group / firewall — inbound rules:**

   | Port | Purpose | Source |
   |---|---|---|
   | 22 | SSH | your IP |
   | 30080 | shop | your IP (or the audience's network for a demo) |
   | 30300 | Grafana | your IP |
   | 30090 | Prometheus | your IP |
   | 6443 | Kubernetes API (only if you want kubectl from your laptop) | your IP |

   *Prefer "My IP" over "Anywhere". If you open nothing but port 22, use the SSH tunnel in step 6.*
6. **Elastic IP (recommended):** allocate one and attach it. Without it, the public IP changes on every
   stop/start.
7. Launch, and note the **public IP**. Below it is written as `<VM_IP>`.

### Step 2 — Copy the setup script to the VM

From your laptop, in a clone of this repository:

```bash
scp -i <path/to/key.pem> infra/vm-bootstrap.sh ubuntu@<VM_IP>:~
```

(Windows: run this in PowerShell or Git Bash; both include `ssh`/`scp`. Or clone the repo on the VM with
`git clone <repo-url>` and use `infra/vm-bootstrap.sh` from there.)

### Step 3 — Run the setup script (~15 minutes)

```bash
ssh -i <path/to/key.pem> ubuntu@<VM_IP>
chmod +x vm-bootstrap.sh
sudo GRAFANA_ADMIN_PASSWORD='<choose-a-grafana-password>' ./vm-bootstrap.sh
```

Optional settings (put them before `./vm-bootstrap.sh`, like the Grafana password):

| Variable | What it does | Default |
|---|---|---|
| `GRAFANA_ADMIN_PASSWORD` | Grafana `admin` password | random; saved in `~/.chaos-lab/grafana-admin-password` |
| `SSH_PASSWORD` | Sets a password for the `ubuntu` user **and enables SSH password login**. Handy for teammates without the key; only use it with a strong password and a restricted firewall. | not set (key-only login) |
| `K3S_TLS_SAN` | Extra address(es) for the Kubernetes API certificate, comma-separated. **Needed outside AWS** if you want kubectl from your laptop: set it to the public IP. | auto-detected on AWS |
| `INSTALL_MONITORING` | `false` skips Prometheus/Grafana | `true` |
| `INSTALL_K6` | `false` skips k6 | `true` |
| `K3S_VERSION`, `HELM_VERSION`, `BOUTIQUE_VERSION`, `CHAOS_MESH_VERSION`, `KPS_VERSION`, `K6_VERSION` | Override pinned versions | see section 1 |

Example outside AWS: `sudo GRAFANA_ADMIN_PASSWORD='…' K3S_TLS_SAN=<VM_IP> ./vm-bootstrap.sh`

The script ends with **13 smoke tests**. It must finish with **`All smoke tests passed`** and prints the access
URLs (they are also saved in `~/chaos-lab-info.txt`). It is **safe to re-run**: when everything is installed it
takes ~1 minute and only repairs what is missing.

Then log out and back in (so `KUBECONFIG` is set), and check: `kubectl get pods -A` → everything `Running`.

### Step 4 — Install the chaos-agent

**Option 1, from your laptop** (Linux / macOS / Git Bash on Windows):

```bash
cp infra/vm.env.example infra/vm.env      # then edit: VM_HOST=<VM_IP>, VM_KEY=<path/to/key.pem>
scripts/deploy.sh                          # copies agent/ to ~/chaos-agent on the VM and installs it in a venv
scripts/run.sh test                        # runs the unit tests on the VM - expect "41 passed" (or more)
```

**Option 2, directly on the VM:**

```bash
git clone <repo-url> ~/chaos-lab-repo
mkdir -p ~/chaos-agent && cp -r ~/chaos-lab-repo/agent/. ~/chaos-agent/
cd ~/chaos-agent && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
echo 'export PATH="$HOME/chaos-agent/.venv/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
cd ~/chaos-agent && python -m pytest -q   # expect all tests to pass
```

### Step 5 — First run (on the VM)

```bash
cd ~/chaos-agent
chaos-agent status       # no chaos resources, no k6 load, workloads 12/12 available
chaos-agent graph        # dependency map  -> results/graph.json, results/graph.mmd
chaos-agent baseline     # 2.5 min: normal error rate and latency -> results/baseline.json
chaos-agent grafana      # creates the "Chaos Lab - Online Boutique" dashboard in Grafana
chaos-agent run --type network_delay --target productcatalogservice -p latency_ms=100 --duration 60
```

The last command injects a 100 ms network delay for 60 s, with the safety guard armed. Expect `COMPLETED` and a
results table (product pages become ~0.7 s slower). Other commands: `chaos-agent --help`, `chaos-agent catalog`
(fault types and limits), `chaos-agent runs`, `chaos-agent show latest`, `chaos-agent abort` (emergency stop).

### Step 6 — Open the web interfaces

If you opened the ports (step 1):

| | URL | Login |
|---|---|---|
| Shop | `http://<VM_IP>:30080` | — |
| Grafana | `http://<VM_IP>:30300` → Dashboards → *Chaos Lab - Online Boutique* | `admin` / your Grafana password |
| Prometheus | `http://<VM_IP>:30090` | — |

If you only opened SSH, use a tunnel and open the URLs on `localhost` instead:

```bash
ssh -i <key.pem> -N -L 8080:localhost:30080 -L 3000:localhost:30300 -L 9090:localhost:30090 ubuntu@<VM_IP>
# shop http://localhost:8080 · Grafana http://localhost:3000 · Prometheus http://localhost:9090
```

(Add `-L 6443:127.0.0.1:6443` to also tunnel the Kubernetes API. The bootstrap summary prints a ready-made
tunnel command too.)

### Step 7 (optional) — kubectl from your laptop

1. Install kubectl **1.34–1.36** (it must be within one minor version of the server, 1.35).
   Windows: `winget install --id Kubernetes.kubectl -e --version 1.35.4` · macOS: `brew install kubectl` ·
   Linux: see kubernetes.io.
2. Copy the kubeconfig the script generated (it points at the public IP):
   `scp -i <key.pem> ubuntu@<VM_IP>:kubeconfig-public.yaml ./kubeconfig-chaos.yaml`
3. `kubectl --kubeconfig ./kubeconfig-chaos.yaml get pods -n boutique`
   (or copy it to `~/.kube/config`). Port 6443 must be reachable. **Don't commit kubeconfig files** — they contain
   admin credentials (`kubeconfig*` is in `.gitignore`).

---

## 3B. Option B — Local VM with Multipass (Windows, macOS, Linux)

[Multipass](https://multipass.run) creates Ubuntu VMs with one command. Your laptop needs **≥ 16 GB RAM** (the VM
gets 12 GB) and ~50 GB free disk. On Windows it uses Hyper-V (Pro/Enterprise) or VirtualBox (Home).

```bash
# 1. Install Multipass: Windows/macOS installer from multipass.run;  Linux: sudo snap install multipass
# 2. Create the VM
multipass launch 24.04 --name chaos-lab --cpus 4 --memory 12G --disk 50G
# 3. Copy and run the setup script (from a clone of this repo)
multipass transfer infra/vm-bootstrap.sh chaos-lab:/home/ubuntu/
multipass exec chaos-lab -- bash -c "chmod +x ~/vm-bootstrap.sh && sudo GRAFANA_ADMIN_PASSWORD='<choose-one>' ~/vm-bootstrap.sh"
# 4. Find the VM's IP and open http://<IP>:30080 (shop), :30300 (Grafana), :30090 (Prometheus)
multipass info chaos-lab | grep IPv4
# 5. Install the agent: Option 2 of step 4 above, inside the VM
multipass shell chaos-lab
```

Then continue with **Step 5** inside the VM. The VM's IP is reachable from your laptop, so no tunnel is needed.
Stop the VM with `multipass stop chaos-lab` when you're not using it, and start it again with `multipass start chaos-lab`.

Notes: Apple Silicon Macs create ARM64 VMs, which are untested. If an Online Boutique image fails to start there,
use a cloud x86_64 VM instead.

---

## 3C. Option C — Native Ubuntu machine

On an Ubuntu 22.04/24.04/26.04 machine with ≥ 4 cores and 16 GB RAM:

```bash
git clone <repo-url> && cd <repo>
sudo GRAFANA_ADMIN_PASSWORD='<choose-one>' infra/vm-bootstrap.sh
```

Then install the agent (Option 2 of step 4) and continue with step 5. Open `http://localhost:30080` etc.
The script installs k3s system-wide; remove it with the uninstall commands in section 5.

---

## 4. Configuration reference

| What | Where |
|---|---|
| Safety thresholds, limits, probe settings | `agent/chaos_agent/config.py` |
| Fault types and their parameter limits | `agent/chaos_agent/templates.py` (`chaos-agent catalog` prints them) |
| Infrastructure versions and options | top of `infra/vm-bootstrap.sh` |
| Grafana admin password (on the VM) | `~/.chaos-lab/grafana-admin-password` |
| Bootstrap summary / URLs (on the VM) | `~/chaos-lab-info.txt` |
| Experiment results (on the VM) | `~/chaos-agent/results/` (copy to the repo with `scripts/run.sh pull-results`) |
| Laptop helper scripts' target VM | `infra/vm.env` (copy of `infra/vm.env.example`; gitignored) |
| OpenAI API key and models (Day-2 AI agents) | `~/chaos-agent/.env` on the VM: `OPENAI_API_KEY=…`, optional `OPENAI_MODEL=…`, `OPENAI_MODEL_FAST=…`, `OPENAI_EFFORT_PLANNER|ADAPTER|ANALYST=low|medium|high` (gitignored, `chmod 600`; check with `chaos-agent ai-check`) |

Default guard thresholds: abort an experiment if error rate > 20 %, or p95 latency > max(3 × baseline, 1500 ms), or
the frontend is silent for 3 s, or a non-target pod becomes unhealthy — 2 checks in a row, checked every second.

---

## 5. Stop, clean up, uninstall

| Goal | Command |
|---|---|
| Stop all running experiments | `chaos-agent abort` (on the VM) |
| Pause a cloud VM (you still pay for its disk) | stop the instance in the cloud console |
| Delete everything in the cloud | terminate the instance; release the Elastic IP; delete the key pair if no longer needed |
| Delete the Multipass VM | `multipass delete --purge chaos-lab` |
| Uninstall from a native Ubuntu machine | `/usr/local/bin/k3s-uninstall.sh` (removes k3s and everything running in it), then `rm -rf ~/chaos-agent ~/.chaos-lab /usr/local/bin/k6 /usr/local/bin/helm` |

---

## 6. Troubleshooting

| Problem | Fix |
|---|---|
| Script stops with `[FAIL] Ubuntu 22.04/24.04/26.04 required …` | Use one of those Ubuntu versions (or `FORCE_OS=true`, unsupported) |
| Script fails while downloading | Re-run it — it is idempotent and continues where it stopped |
| Smoke test "frontend …" fails right after install | Pods were still starting; wait a minute and re-run the script |
| `kubectl: permission denied` on the VM | Log out and back in (the script adds `KUBECONFIG` to `~/.bashrc`) |
| kubectl from the laptop: `certificate is valid for …, not <IP>` | Re-run the script with `K3S_TLS_SAN=<public IP>`, then fetch `kubeconfig-public.yaml` again |
| Network-delay experiments have no effect | The `sch_netem` kernel module is missing; the script installs `linux-modules-extra-$(uname -r)` — check the smoke test "kernel module sch_netem loaded" |
| Browser can't reach ports 30080/30300/30090 | Open them in the cloud firewall for your IP, or use the SSH tunnel (step 6) |
| Public IP changed after stop/start | Attach an Elastic IP; or re-run the script (updates the API certificate and `kubeconfig-public.yaml`) and update `infra/vm.env` |
| `chaos-agent: command not found` | `source ~/.bashrc` or use `~/chaos-agent/.venv/bin/chaos-agent` |
| `chaos-agent run` says another experiment is active | `chaos-agent status`, then `chaos-agent abort` if it's a leftover |
| `PRECHECK_FAILED` | The shop wasn't healthy before injecting (often right after another run); wait a minute and retry |
| Grafana dashboard missing | `chaos-agent grafana` (needs `~/.chaos-lab/grafana-admin-password` or `GRAFANA_PASSWORD` env) |

---

## 7. Security checklist (before pushing a fork or sharing a VM)

- [ ] No `.pem`/`.key` files, kubeconfigs, `.env` files, or passwords committed (`.gitignore` covers the usual
      names; check with `git status` and `git grep -n <your-password>` before pushing).
- [ ] Firewall ports limited to your IP where possible; SSH password login only with a strong password.
- [ ] Grafana password changed from anything simple.
- [ ] Chaos Mesh is restricted to the `boutique` namespace (default in this setup) — don't annotate other
      namespaces with `chaos-mesh.org/inject=enabled`.
- [ ] Terminate cloud VMs you no longer need.
