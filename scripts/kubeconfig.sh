#!/usr/bin/env bash
# Fetch each cluster's kubeconfig from its primary server into ~/.kube/<cluster>.yaml
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p ~/.kube

python3 - <<'EOF' | while read -r cluster ip; do
import json
for n in json.load(open("profile.generated.json"))["nodes"].values():
    if n["primary"]:
        print(n["cluster"], n["ip"])
EOF
  ssh -n -o StrictHostKeyChecking=accept-new "ops@$ip" sudo cat /etc/rancher/k3s/k3s.yaml \
    | sed "s/127.0.0.1/$ip/;s/default/$cluster/g" > ~/.kube/"$cluster".yaml
  chmod 600 ~/.kube/"$cluster".yaml
  echo "wrote ~/.kube/$cluster.yaml"
done
