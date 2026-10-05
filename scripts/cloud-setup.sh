#!/usr/bin/env bash
# Environment setup script for Claude Code cloud sessions (docs/cloud-dev-plan.md section 3).
# The environment's setup script is the single line: bash scripts/cloud-setup.sh
# Idempotent and lean: the result is only cached if this finishes in about 5 minutes.
# Does not start dockerd; the SessionStart hook does that (the cache keeps files, not processes).
set -euo pipefail
cd "$(dirname "$0")/.."

# Pins: bump here only. Keep the Playwright pin equal to CI (.github/workflows/ci.yml).
TF=1.10.5
KUBECTL=v1.31.4
HELM=v3.16.3
K3D=v5.7.4
# Playwright 1.56.0 ships Chromium 1194, matching /opt/pw-browsers/chromium-1194.
PLAYWRIGHT=1.56.0

BIN=/usr/local/bin
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO=sudo
  else
    echo "ERROR: not root and sudo is not available; cannot install into $BIN" >&2
    exit 1
  fi
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "ERROR: $1 failed" >&2; exit 1; }

# have <tool> <expected version string> <version command...>
have() {
  local tool=$1 want=$2
  shift 2
  command -v "$tool" >/dev/null 2>&1 || return 1
  "$@" 2>/dev/null | grep -qF "$want"
}

install_bin() { $SUDO install -m 0755 "$1" "$BIN/$2"; }

# --- Python packages ---
pip install -q -r agents/requirements.txt -r jarvis/requirements.txt \
  pytest pytest-asyncio ansible-lint yamllint || fail "pip install (requirements, pytest, ansible-lint, yamllint)"

ansible-galaxy collection install -r ansible/requirements.yml \
  || echo "WARN: ansible-galaxy failed (check network allowlist)" >&2

# --- Binaries ---
if have terraform "v$TF" terraform version; then
  echo "terraform $TF already installed, skipping"
else
  curl -fsSL "https://releases.hashicorp.com/terraform/$TF/terraform_${TF}_linux_amd64.zip" -o "$TMP/tf.zip" \
    && unzip -oq "$TMP/tf.zip" terraform -d "$TMP" \
    && install_bin "$TMP/terraform" terraform || fail "terraform $TF"
fi

if have kubectl "$KUBECTL" kubectl version --client; then
  echo "kubectl $KUBECTL already installed, skipping"
else
  curl -fsSL "https://dl.k8s.io/release/$KUBECTL/bin/linux/amd64/kubectl" -o "$TMP/kubectl" \
    && install_bin "$TMP/kubectl" kubectl || fail "kubectl $KUBECTL"
fi

if have helm "$HELM" helm version --short; then
  echo "helm $HELM already installed, skipping"
else
  curl -fsSL "https://get.helm.sh/helm-$HELM-linux-amd64.tar.gz" -o "$TMP/helm.tgz" \
    && tar -xzf "$TMP/helm.tgz" -C "$TMP" linux-amd64/helm \
    && install_bin "$TMP/linux-amd64/helm" helm || fail "helm $HELM"
fi

if have k3d "$K3D" k3d version; then
  echo "k3d $K3D already installed, skipping"
else
  curl -fsSL "https://github.com/k3d-io/k3d/releases/download/$K3D/k3d-linux-amd64" -o "$TMP/k3d" \
    && install_bin "$TMP/k3d" k3d || fail "k3d $K3D"
fi

# --- Last, so a wrong pin can't abort the steps above. Reuses the preinstalled Chromium. ---
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 pip install -q "playwright==$PLAYWRIGHT" \
  || echo "WARN: playwright $PLAYWRIGHT failed" >&2

# --- Summary ---
echo "cloud-setup OK: terraform $(terraform version | head -n1 | awk '{print $2}')" \
  "kubectl $(kubectl version --client 2>/dev/null | awk '/Client Version/{print $3}')" \
  "helm $(helm version --short | cut -d+ -f1)" \
  "k3d $(k3d version | awk 'NR==1{print $3}')" \
  "playwright $(python3 -c 'import importlib.metadata as m; print(m.version("playwright"))' 2>/dev/null || echo MISSING)"
