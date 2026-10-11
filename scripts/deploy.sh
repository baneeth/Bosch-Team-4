#!/usr/bin/env bash
# deploy.sh - sync agent/ to the VM (~/chaos-agent) and install it into a venv there.
# Usage (from Git Bash or Linux): scripts/deploy.sh
# Reads VM_HOST / VM_USER / VM_KEY from the environment or from infra/vm.env (gitignored).
set -Eeuo pipefail
cd "$(dirname "$0")/.."
[[ -f infra/vm.env ]] && source infra/vm.env
: "${VM_HOST:?set VM_HOST or create infra/vm.env}"
VM_USER="${VM_USER:-ubuntu}"
SSH=(ssh -o BatchMode=yes)
[[ -n "${VM_KEY:-}" ]] && SSH+=(-i "$VM_KEY")

echo "==> Syncing agent/ to ${VM_USER}@${VM_HOST}:~/chaos-agent"
tar -C agent --exclude=__pycache__ --exclude='*.egg-info' --exclude=.pytest_cache -czf - . |
  "${SSH[@]}" "${VM_USER}@${VM_HOST}" 'mkdir -p ~/chaos-agent && tar -C ~/chaos-agent -xzf -'

echo "==> Installing into ~/chaos-agent/.venv"
"${SSH[@]}" "${VM_USER}@${VM_HOST}" bash -s <<'EOF'
set -Eeuo pipefail
cd ~/chaos-agent
[[ -x .venv/bin/python ]] || python3 -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check -e '.[dev]'
grep -q 'chaos-agent/.venv/bin' ~/.bashrc || echo 'export PATH="$HOME/chaos-agent/.venv/bin:$PATH"' >> ~/.bashrc
echo "installed: $(.venv/bin/chaos-agent --version)"
EOF
