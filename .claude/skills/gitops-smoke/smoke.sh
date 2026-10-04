#!/usr/bin/env bash
# GitOps smoke test: k3d + Argo CD bootstrap + wait for Healthy. Always deletes the cluster on exit.
set -uo pipefail
CLUSTER=${1:-dev}
REV=${2:-$(git rev-parse --abbrev-ref HEAD)}
REPO=${SMOKE_REPO_URL:-$(git remote get-url origin | sed -E 's#^git@github.com:#https://github.com/#; s#^https?://[^@/]*@#https://#; s#/*$##')}
NAME=gitops-smoke
TIMEOUT=${SMOKE_TIMEOUT:-900}
export KUBECONFIG
KUBECONFIG=$(mktemp)

for t in docker k3d kubectl; do command -v $t >/dev/null || { echo "missing $t"; exit 2; }; done
docker info >/dev/null 2>&1 || { echo "docker daemon not running (see /tmp/dockerd.log)"; exit 2; }

cleanup() { k3d cluster delete "$NAME" >/dev/null 2>&1; rm -f "$KUBECONFIG"; echo "cluster $NAME deleted"; }
trap cleanup EXIT
trap 'exit 130' INT TERM

echo "cluster=$CLUSTER revision=$REV repo=$REPO"
k3d cluster delete "$NAME" >/dev/null 2>&1
k3d cluster create "$NAME" --wait --timeout 300s --kubeconfig-update-default=false --kubeconfig-switch-context=false || exit 1
k3d kubeconfig get "$NAME" > "$KUBECONFIG"

kubectl apply -f - <<YAML || exit 1
apiVersion: helm.cattle.io/v1
kind: HelmChart
metadata: {name: argocd, namespace: kube-system}
spec:
  repo: https://argoproj.github.io/argo-helm
  chart: argo-cd
  targetNamespace: argocd
  createNamespace: true
  valuesContent: |-
    dex: { enabled: false }
    notifications: { enabled: false }
YAML

echo "waiting for Argo CD CRDs"
end=$((SECONDS+TIMEOUT))
until kubectl get crd applications.argoproj.io >/dev/null 2>&1; do
  [ $SECONDS -gt $end ] && { echo "TIMEOUT: Argo CD CRDs"; exit 1; }
  sleep 5
done
kubectl wait --for=condition=Established crd/applications.argoproj.io --timeout=60s

kubectl apply -f - <<YAML || exit 1
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata: {name: root, namespace: argocd}
spec:
  project: default
  source: {repoURL: "$REPO", targetRevision: "$REV", path: gitops/clusters/$CLUSTER}
  destination: {server: https://kubernetes.default.svc, namespace: argocd}
  syncPolicy:
    automated: {prune: true, selfHeal: true}
YAML

echo "waiting for all Applications Synced+Healthy"
while true; do
  apps=$(kubectl -n argocd get applications.argoproj.io -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.sync.status}{" "}{.status.health.status}{"\n"}{end}' 2>/dev/null)
  total=$(printf '%s\n' "$apps" | grep -c .)
  bad=$(printf '%s\n' "$apps" | grep -vc ' Synced Healthy$')
  [ "$total" -gt 1 ] && [ "$bad" -eq 0 ] && { echo "OK: $total applications Synced/Healthy"; exit 0; }
  if [ $SECONDS -gt $end ]; then echo "TIMEOUT. Application status:"; printf '%s\n' "$apps"; exit 1; fi
  sleep 15
done
