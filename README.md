# VM-setup

Ubuntu Server LTS host (KVM/libvirt) running a fleet of k3s Kubernetes clusters on minimal headless Ubuntu Server LTS VMs. Each cluster runs its own Argo CD, synced from `gitops/`. An AI operator layer manages the host.

## Quick start
```bash
sudo ./bootstrap.sh --detect-only   # detected facts, chosen profile, node sizes
sudo ./bootstrap.sh                 # install host + AI stack + monitoring
terraform -chdir=terraform init
terraform -chdir=terraform apply \
  -var "ssh_public_key=$(cat ~/.ssh/id_ed25519.pub)" \
  -var gitops_repo_url=https://github.com/<you>/VM-setup.git
```
Fetch a kubeconfig: `ssh ops@10.10.10.10 sudo cat /etc/rancher/k3s/k3s.yaml` (replace `127.0.0.1` with the node IP).

## First-boot checklist
Argo CD pulls `gitops/` from Git, so push this repo to `gitops_repo_url` (public, or add repo credentials to Argo CD) before `terraform apply`.

| Step | Command | Expect |
|---|---|---|
| 1. Detect | `sudo ./bootstrap.sh --detect-only` | JSON with `profile` and `nodes`; exits with an error below 4 cores / 24 GB / 300 GB |
| 2. Host + AI | `sudo ./bootstrap.sh`, then add provider keys to `/etc/vmsetup/secrets.env` and `sudo systemctl restart vmsetup-agent` | `systemctl is-active vmsetup-agent libvirtd` both `active`; `podman ps` shows litellm, prometheus, alertmanager, node-exporter, libvirt-exporter, netdata |
| 3. Plan | `terraform -chdir=terraform plan -var ...` (needs `profile.generated.json` from step 2 and your user in the `libvirt` group) | one network, one base volume, and per node: volume, cloud-init disk, domain |
| 4. Apply | `terraform -chdir=terraform apply -var ...` | `virsh list --all` lists every node as running |
| 5. k3s | `ssh ops@10.10.10.10 'sudo k3s kubectl get nodes'` (cloud-init takes a few minutes; `cloud-init status --wait`) | all nodes of the cluster `Ready` |
| 6. Argo CD | `./scripts/kubeconfig.sh && KUBECONFIG=~/.kube/hub.yaml kubectl -n argocd get applications` | `root` plus apps `Synced`/`Healthy` (CRDs may need a couple of sync retries on first boot) |
| 7. Monitoring | `ssh -L 9090:127.0.0.1:9090 host`, open `localhost:9090/targets` | node, libvirt, vms, netdata targets `UP` |
| 8. Operator | `curl -X POST 127.0.0.1:8085 -d '{"alerts":[{"status":"firing","labels":{"alertname":"GuestDown"}}]}'` | a new row in `/var/lib/vmsetup/incidents.jsonl` (mutating tools stay dry-run until `VMSETUP_LIVE=1`) |

If a node never becomes `Ready`: `sudo journalctl -u k3s -u k3s-agent` on the node, and check that `K3S_URL` points at the cluster's first server (primary servers have none, which is expected).

## Layout
- `detect.py`, `profiles/`: auto-select `min|std|max` (min = 4 cores, 24 GB RAM) and size the clusters (`hub`, `dev`, `prod`). Pin values in `profile.override.yaml`.
- `terraform/`: libvirt NAT network `10.10.10.0/24`, Ubuntu minimal cloud-image VMs, cloud-init installs k3s (HA with embedded etcd when `servers_per_cluster > 1`) and Argo CD on each cluster's first server.
- `gitops/`: per-cluster Argo CD app-of-apps (`clusters/<name>`) over shared `apps/`. Enabled on `hub`: cert-manager, sealed-secrets, kube-prometheus-stack, kyverno. `dev`/`prod`: cert-manager, sealed-secrets. Optional, enable by listing in a cluster's `kustomization.yaml`: external-secrets, argo-rollouts, cloudnative-pg, longhorn (needs 3+ nodes for replicas).
- `scripts/kubeconfig.sh`: writes `~/.kube/<cluster>.yaml` for each cluster.
- `renovate.json`: Renovate PRs for chart versions in `gitops/` and Terraform providers.
- Host CLI tools (Ansible `k8s_tools`): helm, kubectl, argocd, k9s.
- Monitoring: host Prometheus (`127.0.0.1:9090`) scrapes the host, libvirt guests, every node VM (node-exporter on `:9100`) and Netdata. Host Netdata UI is `127.0.0.1:19999`; reach both over an SSH tunnel (`ssh -L 9090:127.0.0.1:9090 -L 19999:127.0.0.1:19999 host`). In-cluster, `hub` runs kube-prometheus-stack and Netdata (parent/child, not claimed to Netdata Cloud).
- Also on `hub`: Loki + Alloy (logs, wired into Grafana), Kyverno audit policies. On all clusters: system-upgrade-controller with k3s `stable` channel plans (auto-upgrades servers then agents, one node at a time). Optional: Velero (set bucket and a `velero-credentials` SealedSecret first).
- `agents/`: `runtime.py` (Operator), `capacity.py` (weekly timer, PR proposals), `upgrade.py` (weekly, PR when a newer Ubuntu LTS image exists), `cost.py` (daily, token spend vs budget), `reviewer.py` (PR gate, see `.github/workflows/agent-review.yml`). Protected paths need the `human-approved` PR label.
- `evals/promptfooconfig.yaml`: operator quality and prompt-injection checks (`npx promptfoo eval -c evals/promptfooconfig.yaml`).
- Ansible creates `/etc/vmsetup/secrets.env` (gateway key generated, provider keys blank). Set `langfuse_enabled: true` in `profile.override.yaml` plus the Langfuse keys for tracing.
- Design choices: k3s Traefik and ServiceLB are kept (no MetalLB or ingress-nginx); node OS patching uses unattended-upgrades; agent memory is a JSONL incident file (no vector database).
- `ansible/`: host hardening, AI gateway, monitoring, operator agent service.
- `agents/manifest.yaml`: agent team, autonomy levels, guardrails.
- `infra-mcp/`: allowlisted MCP server for the node VMs (dry-run by default, kill switch at `/etc/vmsetup/AGENTS_PAUSED`).

Put API keys in `/etc/vmsetup/secrets.env` (root-only, never committed). The image URL defaults to the Ubuntu 26.04 LTS minimal cloud image; the Upgrade agent opens a PR when a newer LTS appears.