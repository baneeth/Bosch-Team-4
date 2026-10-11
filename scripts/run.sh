#!/usr/bin/env bash
# run.sh - run a chaos-agent command on the VM from the laptop, e.g.
#   scripts/run.sh graph
#   scripts/run.sh run --type network_delay --target productcatalogservice -p latency_ms=300 --duration 60
#   scripts/run.sh test            # runs the unit tests on the VM
#   scripts/run.sh pull-results    # copies ~/chaos-agent/results from the VM into ./results
set -Eeuo pipefail
cd "$(dirname "$0")/.."
[[ -f infra/vm.env ]] && source infra/vm.env
: "${VM_HOST:?set VM_HOST or create infra/vm.env}"
VM_USER="${VM_USER:-ubuntu}"
SSH=(ssh -o BatchMode=yes)
[[ -n "${VM_KEY:-}" ]] && SSH+=(-i "$VM_KEY")

case "${1:-}" in
  test)
    exec "${SSH[@]}" "${VM_USER}@${VM_HOST}" 'cd ~/chaos-agent && .venv/bin/python -m pytest -q' ;;
  pull-results)
    mkdir -p results
    "${SSH[@]}" "${VM_USER}@${VM_HOST}" 'tar -C ~/chaos-agent/results -czf - .' | tar -C results -xzf -
    echo "results copied to ./results" ;;
  *)
    args=$(printf '%q ' "$@")
    [[ -t 0 ]] && SSH+=(-t)   # a tty forwards Ctrl-C to the agent, which then aborts the experiment
    exec "${SSH[@]}" "${VM_USER}@${VM_HOST}" \
      "export KUBECONFIG=\$HOME/.kube/config; cd ~/chaos-agent && .venv/bin/chaos-agent ${args}" ;;
esac
