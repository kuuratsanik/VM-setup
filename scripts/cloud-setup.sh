#!/usr/bin/env bash
# Environment setup script for Claude Code cloud sessions (docs/cloud-dev-plan.md section 3).
# The environment's setup script is the single line: bash scripts/cloud-setup.sh
# Idempotent and lean: the result is only cached if this finishes in about 5 minutes.
# Exits 0 even if steps fail (so the environment still caches); --strict exits 1 instead.
# Does not start dockerd; the SessionStart hook does that (the cache keeps files, not processes).
set -uo pipefail  # no -e: a failing step is recorded and reported, never fatal
cd "$(dirname "$0")/.."

# Pins: bump here only. Keep the Playwright pin equal to CI (.github/workflows/ci.yml).
TF=1.10.5
KUBECTL=v1.31.4
HELM=v3.16.3
K3D=v5.7.4
# SHA256 of each download. When a pin above is bumped, its sum MUST be bumped too.
# The script runs as root behind a TLS-terminating proxy, so downloads are verified.
TF_SHA256=0566a24f5332098b15716ebc394be503f4094acba5ba529bf5eb0698ed5e2a90          # terraform_1.10.5_linux_amd64.zip (releases.hashicorp.com SHA256SUMS)
KUBECTL_SHA256=298e19e9c6c17199011404278f0ff8168a7eca4217edad9097af577023a5620f     # kubectl.sha256 from dl.k8s.io
K3D_SHA256=1ac1da365236736a8df8c32107b54aca208384ab1d9a06771443c85ad698a5eb         # k3d-linux-amd64 (no sums file published; hashed after download)
# HELM: get.helm.sh was not reachable when this was written, so the sum is fetched from
# the .sha256sum file at install time. Once the host is allowlisted, replace that with a
# hardcoded HELM_SHA256 here.
# Playwright 1.56.0 ships Chromium 1194, matching /opt/pw-browsers/chromium-1194.
PLAYWRIGHT=1.56.0

STRICT=0
[ "${1:-}" = "--strict" ] && STRICT=1

BIN=/usr/local/bin
FAILED=()
SUDO=""
NOSUDO=0
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo -n"
  else
    NOSUDO=1
    echo "ERROR: not root and sudo is not available; cannot install into $BIN" >&2
  fi
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# have <tool> <expected version string> <version command...>
have() {
  local tool=$1 want=$2
  shift 2
  command -v "$tool" >/dev/null 2>&1 || return 1
  "$@" 2>/dev/null | grep -qwF -- "$want"
}

install_bin() { [ "$NOSUDO" -eq 0 ] && $SUDO install -m 0755 "$1" "$BIN/$2"; }

# step <name> <function>: run the function, record the name on failure, always continue.
step() {
  if ! "$2"; then
    echo "WARN: $1 failed" >&2
    FAILED+=("$1")
  fi
}

# --- Python packages ---
do_pip() {
  pip install -q -r agents/requirements.txt -r jarvis/requirements.txt \
    pytest pytest-asyncio ansible-lint yamllint
}
do_galaxy() { ansible-galaxy collection install -r ansible/requirements.yml </dev/null 2>&1 | cat; }

# --- Binaries ---
do_terraform() {
  if have terraform "v$TF" terraform version; then
    echo "terraform $TF already installed, skipping"; return 0
  fi
  curl -fsSL "https://releases.hashicorp.com/terraform/$TF/terraform_${TF}_linux_amd64.zip" -o "$TMP/tf.zip" \
    && echo "$TF_SHA256  $TMP/tf.zip" | sha256sum -c - \
    && unzip -oq "$TMP/tf.zip" terraform -d "$TMP" \
    && install_bin "$TMP/terraform" terraform
}
do_kubectl() {
  if have kubectl "$KUBECTL" kubectl version --client; then
    echo "kubectl $KUBECTL already installed, skipping"; return 0
  fi
  curl -fsSL "https://dl.k8s.io/release/$KUBECTL/bin/linux/amd64/kubectl" -o "$TMP/kubectl" \
    && echo "$KUBECTL_SHA256  $TMP/kubectl" | sha256sum -c - \
    && install_bin "$TMP/kubectl" kubectl
}
do_helm() {
  if have helm "$HELM" helm version --short; then
    echo "helm $HELM already installed, skipping"; return 0
  fi
  curl -fsSL "https://get.helm.sh/helm-$HELM-linux-amd64.tar.gz" -o "$TMP/helm.tgz" \
    && curl -fsSL "https://get.helm.sh/helm-$HELM-linux-amd64.tar.gz.sha256sum" -o "$TMP/helm.sum" \
    && echo "$(awk '{print $1}' "$TMP/helm.sum")  $TMP/helm.tgz" | sha256sum -c - \
    && tar -xzf "$TMP/helm.tgz" -C "$TMP" linux-amd64/helm \
    && install_bin "$TMP/linux-amd64/helm" helm
}
do_k3d() {
  if have k3d "$K3D" k3d version; then
    echo "k3d $K3D already installed, skipping"; return 0
  fi
  curl -fsSL "https://github.com/k3d-io/k3d/releases/download/$K3D/k3d-linux-amd64" -o "$TMP/k3d" \
    && echo "$K3D_SHA256  $TMP/k3d" | sha256sum -c - \
    && install_bin "$TMP/k3d" k3d
}
# Last, so a wrong pin can't abort the steps above. Reuses the preinstalled Chromium.
do_playwright() { PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 pip install -q "playwright==$PLAYWRIGHT"; }

step pip do_pip
step ansible-galaxy do_galaxy
step terraform do_terraform
step kubectl do_kubectl
step helm do_helm
step k3d do_k3d
step playwright do_playwright

# --- Summary ---
ver() { "$@" 2>/dev/null || true; }
v_tf=$(ver terraform version | head -n1 | awk '{print $2}')
v_kc=$(ver kubectl version --client | awk '/Client Version/{print $3}')
v_helm=$(ver helm version --short | cut -d+ -f1)
v_k3d=$(ver k3d version | awk 'NR==1{print $3}')
v_pw=$(python3 -c 'import importlib.metadata as m; print(m.version("playwright"))' 2>/dev/null || true)
echo "cloud-setup: terraform ${v_tf:-MISSING} kubectl ${v_kc:-MISSING} helm ${v_helm:-MISSING} k3d ${v_k3d:-MISSING} playwright ${v_pw:-MISSING}"
if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "cloud-setup WARN: failed: ${FAILED[*]} (check network allowlist)" >&2
  [ "$STRICT" -eq 1 ] && exit 1
fi
exit 0
