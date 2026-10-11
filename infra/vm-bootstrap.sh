#!/usr/bin/env bash
# vm-bootstrap.sh - one-shot, idempotent setup of the chaos-engineering sandbox on a fresh Ubuntu VM.
#
# Installs:
#   - k3s                    single-node Kubernetes (traefik + servicelb disabled to save resources)
#   - Helm                   package manager for the charts below
#   - Online Boutique        target microservices app (11 services + built-in load generator), namespace "boutique"
#   - Chaos Mesh             failure injection; restricted to namespaces annotated chaos-mesh.org/inject=enabled
#   - kube-prometheus-stack  Prometheus + Grafana + kube-state-metrics + node-exporter, namespace "monitoring"
#   - k6                     load generator CLI for traffic-spike / combined-failure experiments
#   - python3 venv tooling   in case the AI agent runs on the VM
#
# Usage:   sudo ./vm-bootstrap.sh        (safe to re-run; each step skips or upgrades in place)
# Target:  Ubuntu 22.04 / 24.04 / 26.04, >= 4 vCPU, >= 16 GB RAM, >= 40 GB free disk.
#
# All versions are pinned for reproducibility and can be overridden via environment, e.g.:
#   sudo K3S_VERSION=v1.35.9+k3s1 INSTALL_MONITORING=false ./vm-bootstrap.sh

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
K3S_VERSION="${K3S_VERSION:-v1.35.9+k3s1}"
HELM_VERSION="${HELM_VERSION:-v3.22.0}"
BOUTIQUE_VERSION="${BOUTIQUE_VERSION:-v0.10.7}"
CHAOS_MESH_VERSION="${CHAOS_MESH_VERSION:-2.8.4}"
KPS_VERSION="${KPS_VERSION:-92.2.0}"          # kube-prometheus-stack chart
K6_VERSION="${K6_VERSION:-v2.3.0}"

INSTALL_MONITORING="${INSTALL_MONITORING:-true}"
INSTALL_K6="${INSTALL_K6:-true}"
FORCE_OS="${FORCE_OS:-false}"                  # set true to skip the Ubuntu check (unsupported)
SSH_PASSWORD="${SSH_PASSWORD:-}"               # if set: enable SSH password login for the sudo user
GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-}"  # if set: use instead of the generated password
K3S_TLS_SAN="${K3S_TLS_SAN:-}"                 # extra API-cert addresses, comma-separated; auto-detected on AWS

APP_NS="boutique"
CHAOS_NS="chaos-mesh"
MON_NS="monitoring"

# NodePorts (open them in the cloud firewall only to trusted IPs, or reach them through an SSH tunnel)
FRONTEND_NODEPORT=30080
GRAFANA_NODEPORT=30300
PROMETHEUS_NODEPORT=30090

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-y -q -o DPkg::Lock::Timeout=300)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { printf '\n\033[1;34m[%s] ==> %s\033[0m\n' "$(date +%H:%M:%S)" "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN] %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m[FAIL] %s\033[0m\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

trap 'die "line ${LINENO}: command failed: ${BASH_COMMAND}"' ERR

retry() { # retry <attempts> <sleep-seconds> <cmd...>
  local attempts=$1 delay=$2; shift 2
  local i
  for ((i = 1; i <= attempts; i++)); do
    if "$@"; then return 0; fi
    ((i < attempts)) && sleep "$delay"
  done
  return 1
}

arch() {
  case "$(uname -m)" in
    x86_64)  echo amd64 ;;
    aarch64) echo arm64 ;;
    *) die "unsupported CPU architecture: $(uname -m)" ;;
  esac
}

# Prints the pinned chart version if the repo has it, otherwise the latest available one.
resolve_chart_version() {
  local chart=$1 want=$2 found latest
  found=$(helm search repo "$chart" --versions -o json \
          | jq -r --arg c "$chart" --arg v "$want" '[.[] | select(.name == $c and .version == $v)] | length')
  if [[ "$found" -gt 0 ]]; then
    echo "$want"
  else
    latest=$(helm search repo "$chart" -o json | jq -r --arg c "$chart" '[.[] | select(.name == $c)][0].version')
    warn "$chart $want not found in repo; falling back to latest ($latest)"
    echo "$latest"
  fi
}

# Prints the instance's public IPv4 from the AWS metadata service (IMDSv2); fails elsewhere.
detect_public_ip() {
  local token ip
  token=$(curl -s -m 2 -X PUT http://169.254.169.254/latest/api/token \
          -H "X-aws-ec2-metadata-token-ttl-seconds: 60") || return 1
  ip=$(curl -s -m 2 -H "X-aws-ec2-metadata-token: ${token}" \
       http://169.254.169.254/latest/meta-data/public-ipv4) || return 1
  [[ "$ip" =~ ^[0-9]+(\.[0-9]+){3}$ ]] || return 1
  echo "$ip"
}

# ---------------------------------------------------------------------------
# 0. Preflight
# ---------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
  exec sudo -E bash "$0" "$@"
fi

TARGET_USER="${SUDO_USER:-root}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
STATE_DIR="$TARGET_HOME/.chaos-lab"

log "Preflight checks"
# shellcheck disable=SC1091
source /etc/os-release
if [[ "$ID" != "ubuntu" || ! "$VERSION_ID" =~ ^(22\.04|24\.04|26\.04)$ ]]; then
  [[ "$FORCE_OS" == "true" ]] || die "Ubuntu 22.04/24.04/26.04 required (found $PRETTY_NAME). Set FORCE_OS=true to try anyway."
  warn "Unsupported OS $PRETTY_NAME - continuing because FORCE_OS=true"
fi
info "OS: $PRETTY_NAME, arch: $(arch), kernel: $(uname -r)"

cpus=$(nproc)
mem_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024 + 0.5}' /proc/meminfo)
disk_gb=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
info "CPU: ${cpus} vCPU, RAM: ${mem_gb} GB, free disk on /: ${disk_gb} GB"
((cpus >= 4))     || warn "fewer than 4 vCPU - expect slow pods and noisy latency measurements"
((mem_gb >= 15))  || warn "less than 16 GB RAM - consider INSTALL_MONITORING=false"
((disk_gb >= 30)) || warn "less than 30 GB free disk - image pulls may fail"

if have cloud-init; then
  info "waiting for cloud-init to finish (avoids apt lock races on fresh VMs)"
  cloud-init status --wait >/dev/null 2>&1 || true
fi

mkdir -p "$STATE_DIR"
chown "$TARGET_USER:" "$STATE_DIR"
chmod 700 "$STATE_DIR"

# ---------------------------------------------------------------------------
# 1. OS packages, kernel modules, sysctls
# ---------------------------------------------------------------------------
log "Installing OS packages"
apt-get update -q
apt-get install "${APT_OPTS[@]}" curl ca-certificates jq git tar openssl \
  python3 python3-venv python3-pip

log "Loading kernel modules needed by Chaos Mesh"
# sch_netem: network delay/loss/corruption (NetworkChaos). Often missing from Ubuntu cloud kernels.
if ! modprobe sch_netem 2>/dev/null; then
  info "sch_netem not available - installing linux-modules-extra-$(uname -r)"
  apt-get install "${APT_OPTS[@]}" "linux-modules-extra-$(uname -r)" \
    || die "could not install linux-modules-extra-$(uname -r); network-delay experiments will not work"
  modprobe sch_netem || die "sch_netem still not loadable after installing linux-modules-extra"
fi
for m in br_netfilter overlay ip_set xt_set; do
  modprobe "$m" 2>/dev/null || warn "kernel module $m not loadable (some partition experiments may fail)"
done
cat >/etc/modules-load.d/chaos-lab.conf <<'EOF'
sch_netem
br_netfilter
overlay
ip_set
xt_set
EOF

log "Applying sysctls"
cat >/etc/sysctl.d/99-chaos-lab.conf <<'EOF'
fs.inotify.max_user_instances = 1024
fs.inotify.max_user_watches   = 1048576
net.ipv4.ip_forward           = 1
EOF
sysctl --system >/dev/null

if [[ -n "$SSH_PASSWORD" ]]; then
  log "Enabling SSH password login for ${TARGET_USER}"
  echo "${TARGET_USER}:${SSH_PASSWORD}" | chpasswd
  # sshd keeps the first value it reads; this file sorts before the cloud image's
  # 60-cloudimg-settings.conf, which disables password login.
  cat >/etc/ssh/sshd_config.d/10-chaos-lab-password.conf <<'EOF'
# Added by vm-bootstrap.sh (SSH_PASSWORD)
PasswordAuthentication yes
EOF
  sshd -t || die "sshd config is invalid - not reloading"
  systemctl reload ssh 2>/dev/null || systemctl restart ssh
fi

# ---------------------------------------------------------------------------
# 2. k3s
# ---------------------------------------------------------------------------
log "Configuring k3s"
# The API server certificate must list every address kubectl will use. On AWS the public IP is not on
# any interface, so k3s would not include it on its own.
tls_sans=()
if [[ -n "$K3S_TLS_SAN" ]]; then
  IFS=',' read -ra tls_sans <<<"$K3S_TLS_SAN"
elif PUBLIC_IP=$(detect_public_ip); then
  tls_sans=("$PUBLIC_IP")
fi
PUBLIC_IP="${tls_sans[0]:-}"
info "public address for the API certificate: ${PUBLIC_IP:-<none detected; set K3S_TLS_SAN>}"

mkdir -p /etc/rancher/k3s
new_cfg=$(mktemp)
{
  echo "# Managed by vm-bootstrap.sh"
  echo "disable: [traefik, servicelb]"
  if ((${#tls_sans[@]} > 0)); then
    echo "tls-san:"
    for san in "${tls_sans[@]}"; do echo "  - \"${san}\""; done
  fi
} >"$new_cfg"
k3s_cfg_changed=false
if ! cmp -s "$new_cfg" /etc/rancher/k3s/config.yaml; then
  install -m 644 "$new_cfg" /etc/rancher/k3s/config.yaml
  k3s_cfg_changed=true
fi
rm -f "$new_cfg"
# World-readable (no secrets in it): k3s's bundled kubectl reads it and warns "permission denied" otherwise.
chmod 644 /etc/rancher/k3s/config.yaml

log "Installing k3s ${K3S_VERSION}"
if have k3s && k3s --version | grep -qF "${K3S_VERSION}"; then
  info "k3s ${K3S_VERSION} already installed - skipping"
  if [[ "$k3s_cfg_changed" == "true" ]]; then
    info "k3s config changed - restarting k3s (running pods are not affected)"
    systemctl restart k3s
  fi
else
  curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" sh -s - server
fi

info "waiting for the node to become Ready"
retry 60 5 bash -c 'kubectl get nodes --no-headers 2>/dev/null | grep -qw Ready' \
  || die "k3s node did not become Ready"
kubectl -n kube-system wait --for=condition=Available deployment/coredns --timeout=300s
NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
info "node InternalIP: ${NODE_IP}"

info "installing kubeconfig for ${TARGET_USER} at ${TARGET_HOME}/.kube/config"
install -d -m 700 -o "$TARGET_USER" -g "$(id -gn "$TARGET_USER")" "$TARGET_HOME/.kube"
install -m 600 -o "$TARGET_USER" -g "$(id -gn "$TARGET_USER")" /etc/rancher/k3s/k3s.yaml "$TARGET_HOME/.kube/config"
# k3s's bundled kubectl falls back to /etc/rancher/k3s/k3s.yaml (root-only) unless KUBECONFIG is set.
if ! grep -qs 'KUBECONFIG=' "$TARGET_HOME/.bashrc"; then
  echo 'export KUBECONFIG="$HOME/.kube/config"' >>"$TARGET_HOME/.bashrc"
fi

# Copy of the kubeconfig that points at the public address, for use from laptops.
PUBLIC_KUBECONFIG="$TARGET_HOME/kubeconfig-public.yaml"
if [[ -n "$PUBLIC_IP" ]]; then
  sed "s#https://127.0.0.1:6443#https://${PUBLIC_IP}:6443#" /etc/rancher/k3s/k3s.yaml >"$PUBLIC_KUBECONFIG"
  chown "$TARGET_USER:" "$PUBLIC_KUBECONFIG"
  chmod 600 "$PUBLIC_KUBECONFIG"
fi

# ---------------------------------------------------------------------------
# 3. Helm
# ---------------------------------------------------------------------------
log "Installing Helm ${HELM_VERSION}"
if have helm && helm version --short 2>/dev/null | grep -qF "${HELM_VERSION}"; then
  info "helm ${HELM_VERSION} already installed - skipping"
else
  tmp=$(mktemp -d)
  curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-$(arch).tar.gz" | tar -xz -C "$tmp"
  install -m 755 "$tmp/linux-$(arch)/helm" /usr/local/bin/helm
  rm -rf "$tmp"
fi

helm repo add chaos-mesh https://charts.chaos-mesh.org --force-update >/dev/null
if [[ "$INSTALL_MONITORING" == "true" ]]; then
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update >/dev/null
fi
helm repo update >/dev/null

# ---------------------------------------------------------------------------
# 4. Online Boutique (target application)
# ---------------------------------------------------------------------------
log "Deploying Online Boutique ${BOUTIQUE_VERSION} into namespace ${APP_NS}"
kubectl create namespace "$APP_NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
# Opt this namespace in as a chaos target (Chaos Mesh is installed with namespace filtering on).
kubectl annotate namespace "$APP_NS" chaos-mesh.org/inject=enabled --overwrite >/dev/null
kubectl label namespace "$APP_NS" app.kubernetes.io/part-of=chaos-lab --overwrite >/dev/null

kubectl -n "$APP_NS" apply -f \
  "https://raw.githubusercontent.com/GoogleCloudPlatform/microservices-demo/${BOUTIQUE_VERSION}/release/kubernetes-manifests.yaml"

# frontend-external ships as a LoadBalancer; with servicelb disabled we pin it to a fixed NodePort instead.
kubectl -n "$APP_NS" patch service frontend-external --type json -p "[
  {\"op\": \"add\", \"path\": \"/spec/type\", \"value\": \"NodePort\"},
  {\"op\": \"add\", \"path\": \"/spec/ports/0/nodePort\", \"value\": ${FRONTEND_NODEPORT}}
]" >/dev/null

info "waiting for all Online Boutique deployments (first image pull can take several minutes)"
kubectl -n "$APP_NS" wait --for=condition=Available deployment --all --timeout=900s

# ---------------------------------------------------------------------------
# 5. Chaos Mesh (failure injection)
# ---------------------------------------------------------------------------
cm_version=$(resolve_chart_version chaos-mesh/chaos-mesh "$CHAOS_MESH_VERSION")
log "Installing Chaos Mesh ${cm_version}"
# - runtime/socketPath: k3s ships its own containerd at a non-default socket path. Without this,
#   NetworkChaos/StressChaos silently fail to attach to containers.
# - enableFilterNamespace: experiments only take effect in namespaces annotated
#   chaos-mesh.org/inject=enabled -> hard blast-radius boundary (kube-system, monitoring are untouchable).
# - dashboard stays ClusterIP and is reached via `kubectl port-forward`; it is not exposed on the VM.
helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
  --namespace "$CHAOS_NS" --create-namespace \
  --version "$cm_version" \
  --set chaosDaemon.runtime=containerd \
  --set chaosDaemon.socketPath=/run/k3s/containerd/containerd.sock \
  --set controllerManager.enableFilterNamespace=true \
  --set dashboard.securityMode=false \
  --set dashboard.service.type=ClusterIP \
  --wait --timeout 10m

# ---------------------------------------------------------------------------
# 6. Monitoring (Prometheus + Grafana)
# ---------------------------------------------------------------------------
if [[ "$INSTALL_MONITORING" == "true" ]]; then
  pw_file="$STATE_DIR/grafana-admin-password"
  if [[ -n "$GRAFANA_ADMIN_PASSWORD" ]]; then
    printf '%s' "$GRAFANA_ADMIN_PASSWORD" >"$pw_file"
  elif [[ ! -s "$pw_file" ]]; then
    openssl rand -base64 18 | tr -d '/+=' >"$pw_file"
  fi
  chown "$TARGET_USER:" "$pw_file"
  chmod 600 "$pw_file"
  GRAFANA_PASSWORD=$(<"$pw_file")

  kps_version=$(resolve_chart_version prometheus-community/kube-prometheus-stack "$KPS_VERSION")
  log "Installing kube-prometheus-stack ${kps_version}"
  values=$(mktemp --suffix=.yaml)
  cat >"$values" <<EOF
alertmanager:
  enabled: false
# k3s runs these control-plane components inside the k3s binary; their scrape targets would always be down.
kubeControllerManager: { enabled: false }
kubeScheduler:         { enabled: false }
kubeEtcd:              { enabled: false }
kubeProxy:             { enabled: false }
grafana:
  adminPassword: "${GRAFANA_PASSWORD}"
  service:
    type: NodePort
    nodePort: ${GRAFANA_NODEPORT}
prometheus:
  service:
    type: NodePort
    nodePort: ${PROMETHEUS_NODEPORT}
  prometheusSpec:
    scrapeInterval: 5s          # fine-grained enough to measure recovery time
    evaluationInterval: 15s
    retention: 3d
    # Pick up ServiceMonitors/PodMonitors/rules from any namespace, not only ones labelled for this release.
    serviceMonitorSelectorNilUsesHelmValues: false
    podMonitorSelectorNilUsesHelmValues: false
    ruleSelectorNilUsesHelmValues: false
    resources:
      requests: { cpu: 200m, memory: 512Mi }
EOF
  helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
    --namespace "$MON_NS" --create-namespace \
    --version "$kps_version" \
    -f "$values" \
    --wait --timeout 15m
  rm -f "$values"
fi

# ---------------------------------------------------------------------------
# 7. k6 (traffic spikes for combined-failure experiments)
# ---------------------------------------------------------------------------
if [[ "$INSTALL_K6" == "true" ]]; then
  log "Installing k6 ${K6_VERSION}"
  if have k6 && k6 version 2>/dev/null | grep -qF "${K6_VERSION}"; then
    info "k6 ${K6_VERSION} already installed - skipping"
  else
    tmp=$(mktemp -d)
    curl -fsSL "https://github.com/grafana/k6/releases/download/${K6_VERSION}/k6-${K6_VERSION}-linux-$(arch).tar.gz" \
      | tar -xz -C "$tmp"
    install -m 755 "$tmp/k6-${K6_VERSION}-linux-$(arch)/k6" /usr/local/bin/k6
    rm -rf "$tmp"
  fi
fi

# ---------------------------------------------------------------------------
# 8. Smoke tests
# ---------------------------------------------------------------------------
log "Running smoke tests"
failures=0
check() { # check <description> <cmd...>
  local desc=$1; shift
  if "$@" >/dev/null 2>&1; then
    printf '    \033[32m[ OK ]\033[0m %s\n' "$desc"
  else
    printf '    \033[31m[FAIL]\033[0m %s\n' "$desc"
    failures=$((failures + 1))
  fi
}
http_ok() { [[ "$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$1")" == "200" ]]; }

FRONTEND_IP=$(kubectl -n "$APP_NS" get service frontend -o jsonpath='{.spec.clusterIP}')
check "kernel module sch_netem loaded"                 grep -qw sch_netem /proc/modules
check "node Ready"                                     bash -c 'kubectl get nodes --no-headers | grep -qw Ready'
check "all ${APP_NS} deployments Available"            kubectl -n "$APP_NS" wait --for=condition=Available deployment --all --timeout=10s
check "frontend responds 200 (ClusterIP)"              retry 12 5 http_ok "http://${FRONTEND_IP}/"
check "frontend responds 200 (NodePort ${FRONTEND_NODEPORT})" retry 6 5 http_ok "http://${NODE_IP}:${FRONTEND_NODEPORT}/"
check "loadgenerator running (baseline traffic)"      kubectl -n "$APP_NS" wait --for=condition=Available deployment/loadgenerator --timeout=10s
check "Chaos Mesh CRDs installed"                      kubectl get crd podchaos.chaos-mesh.org networkchaos.chaos-mesh.org stresschaos.chaos-mesh.org httpchaos.chaos-mesh.org
check "Chaos Mesh pods running"                        kubectl -n "$CHAOS_NS" wait --for=condition=Ready pod --all --timeout=60s
check "namespace ${APP_NS} opted in as chaos target"   bash -c "kubectl get ns ${APP_NS} -o jsonpath='{.metadata.annotations.chaos-mesh\.org/inject}' | grep -qx enabled"
if [[ "$INSTALL_MONITORING" == "true" ]]; then
  check "Prometheus ready (NodePort ${PROMETHEUS_NODEPORT})" retry 12 5 http_ok "http://${NODE_IP}:${PROMETHEUS_NODEPORT}/-/ready"
  check "Grafana healthy (NodePort ${GRAFANA_NODEPORT})"    retry 12 5 http_ok "http://${NODE_IP}:${GRAFANA_NODEPORT}/api/health"
fi
[[ "$INSTALL_K6" == "true" ]] && check "k6 installed" k6 version
if [[ -n "$PUBLIC_IP" ]]; then
  check "kubectl works via public address ${PUBLIC_IP}" \
    retry 6 5 kubectl --kubeconfig "$PUBLIC_KUBECONFIG" get nodes --request-timeout=5s
fi

# ---------------------------------------------------------------------------
# 9. Summary
# ---------------------------------------------------------------------------
info_file="$TARGET_HOME/chaos-lab-info.txt"
host="${PUBLIC_IP:-<VM_PUBLIC_IP>}"
tunnel="ssh -N -L 6443:127.0.0.1:6443 -L 8080:${NODE_IP}:${FRONTEND_NODEPORT}"
[[ "$INSTALL_MONITORING" == "true" ]] && \
  tunnel+=" -L 3000:${NODE_IP}:${GRAFANA_NODEPORT} -L 9090:${NODE_IP}:${PROMETHEUS_NODEPORT}"
tunnel+=" ${TARGET_USER}@${host}"

mon_versions=""
mon_urls=""
if [[ "$INSTALL_MONITORING" == "true" ]]; then
  mon_versions=" | kube-prometheus-stack ${kps_version}"
  mon_urls="   Grafana           http://${host}:${GRAFANA_NODEPORT}   (user: admin, password in ~/.chaos-lab/grafana-admin-password on the VM)
   Prometheus        http://${host}:${PROMETHEUS_NODEPORT}"
fi

cat >"$info_file" <<EOF
Chaos Lab sandbox - generated by vm-bootstrap.sh on $(date -u '+%Y-%m-%d %H:%M UTC')

Versions: k3s ${K3S_VERSION} | Online Boutique ${BOUTIQUE_VERSION} | Chaos Mesh ${cm_version}${mon_versions}

Direct access (the firewall must allow these ports):
   Online Boutique   http://${host}:${FRONTEND_NODEPORT}
${mon_urls}
   Kubernetes API    scp ${TARGET_USER}@${host}:kubeconfig-public.yaml ./kubeconfig-chaos.yaml
                     kubectl --kubeconfig ./kubeconfig-chaos.yaml get pods -n ${APP_NS}
   Chaos Mesh UI     kubectl --kubeconfig ./kubeconfig-chaos.yaml -n ${CHAOS_NS} port-forward svc/chaos-dashboard 2333
                     then http://localhost:2333

If only port 22 is open, use an SSH tunnel instead and replace ${host}:<port> with localhost:
   ${tunnel}
   (Online Boutique localhost:8080, Grafana localhost:3000, Prometheus localhost:9090; use ~/.kube/config,
    whose server is https://127.0.0.1:6443)

Chaos targets: only namespaces annotated chaos-mesh.org/inject=enabled (currently: ${APP_NS}).
EOF
chown "$TARGET_USER:" "$info_file"

log "Done"
cat "$info_file"
echo
if ((failures > 0)); then
  warn "${failures} smoke test(s) failed - see above. Re-running this script is safe."
  exit 1
fi
printf '\033[1;32mAll smoke tests passed. Summary saved to %s\033[0m\n' "$info_file"
