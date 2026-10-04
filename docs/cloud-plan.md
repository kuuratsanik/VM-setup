# Cloud plan: developing in the cloud and running in the cloud

This plan covers two related tracks:

- **Track A, cloud development.** Work on this repo in Claude Code cloud sessions, using the frontend/backend/adversary agent team, with checks that match CI.
- **Track B, the platform in the cloud.** Let vm-setup create k3s clusters on a cloud provider alongside the existing KVM/libvirt fleet. Keep the same GitOps, monitoring, AI operator and guardrails.

Track A comes first. It is cheap and it makes Track B safer to build.

## 1. Starting point (as of `main` @ a8bb19c)

| Area | Today | Matters for cloud because |
|---|---|---|
| Infra | `terraform/main.tf` only uses the `dmacvicar/libvirt` provider. One NAT network is fixed at `10.10.10.0/24`, and node IPs and sizes come from `profile.generated.json` | There is no provider abstraction. Node creation, network and cloud-init are mixed together in one file |
| Terraform state | Local only (`*.tfstate*` is gitignored), with no backend block | A cloud fleet needs shared, locked, encrypted state |
| Bootstrap | `cloud-init.yaml.tftpl` installs k3s and, on primary servers, Argo CD plus a `root` Application that pulls `gitops/clusters/<name>` | This is portable as is. Cloud VMs can use the same template |
| GitOps | Argo CD app-of-apps per cluster (`hub`, `dev`, `prod`). Clusters pull from Git, nothing pushes to them | Works over any network that can reach GitHub. No inbound access is needed |
| Sizing | `detect.py` reads host hardware into a `min`/`std`/`max` profile | A cloud cluster has no host to detect. It needs a declared profile |
| Operator | `infra-mcp/` has allowlisted libvirt tools, gated by `agents/policy.py` and `autonomy.yaml` | Cloud actions need the same gate, plus a cost dimension |
| Compute | Jarvis rents RunPod pods and runs Kaggle notebooks, with price caps, a lifetime limit and a reaper | This is the pattern to copy for cloud VMs |
| Guardrails | `agents/reviewer.py`: agent PRs may never touch `terraform/`, `ansible/` or `gitops/` (IMMUTABLE). Human PRs under `jarvis/`, `agents/` and others need the `human-approved` label | `terraform/` and `gitops/` are **not** in `PROTECTED`, so a human-authored PR changing them merges without that label |
| CI | `ci.yml`: `terraform validate`, ansible-lint, yamllint, py_compile, `node --check`, pytest, and `kubectl kustomize` for each cluster | There is no browser test of Jarvis and no live sync test of GitOps |
| Cloud session | 4 vCPU, 15 GB RAM, Docker available, **no `/dev/kvm`**. terraform, kubectl, helm, ansible-lint, yamllint, k3d and kind are not installed | Libvirt VMs can never run in a cloud session. Containers can |

---

## Track A: developing in the cloud

### A1. Environment (one-time setup in the cloud environment settings)

**Setup script.** Add this in the environment's settings under *Setup script*. It runs when a new session starts.

```bash
set -euo pipefail
# Python deps used by CI
pip install -q -r agents/requirements.txt -r jarvis/requirements.txt pytest pytest-asyncio ansible-lint yamllint
ansible-galaxy collection install -r ansible/requirements.yml
# Pinned CLI tools (bump in one place; Renovate can track these later)
TF=1.9.8; KUBECTL=v1.31.4; HELM=v3.16.3; K3D=v5.7.4
curl -fsSL https://releases.hashicorp.com/terraform/$TF/terraform_${TF}_linux_amd64.zip -o /tmp/tf.zip && unzip -oq /tmp/tf.zip -d /usr/local/bin
curl -fsSL https://dl.k8s.io/release/$KUBECTL/bin/linux/amd64/kubectl -o /usr/local/bin/kubectl && chmod +x /usr/local/bin/kubectl
curl -fsSL https://get.helm.sh/helm-$HELM-linux-amd64.tar.gz | tar -xz -C /tmp && mv /tmp/linux-amd64/helm /usr/local/bin/
curl -fsSL https://github.com/k3d-io/k3d/releases/download/$K3D/k3d-linux-amd64 -o /usr/local/bin/k3d && chmod +x /usr/local/bin/k3d
```

The versions above are examples. Pin the ones CI uses.

**Network access.** Use *Custom*, keep the default package-manager list, and add:
- `releases.hashicorp.com`, `registry.terraform.io`
- `dl.k8s.io`, `get.helm.sh`
- `github.com`, `objects.githubusercontent.com`
- The Helm chart repos used in `gitops/apps/*` (from `grep -rh "repoURL" gitops/apps | sort -u`; re-run that when apps are added):
  - `argoproj.github.io`, `bitnami.github.io`, `charts.external-secrets.io`, `charts.jetstack.io`
  - `charts.k8sgpt.ai`, `charts.longhorn.io`, `cloudnative-pg.github.io`, `grafana.github.io`
  - `kyverno.github.io`, `netdata.github.io`, `prometheus-community.github.io`, `vmware-tanzu.github.io`
- For the k3d smoke test: `ghcr.io`, `docker.io`, `registry-1.docker.io`, `quay.io`, `registry.k8s.io` (container images).

**Secrets.** No provider keys and no real `secrets.env` in cloud sessions. The only credential worth adding is a read-only one, such as a GitHub token with read-only scope if the agents need to read Actions logs. Cloud provider credentials stay out of dev sessions entirely (see B6).

**SessionStart hook** (`.claude/settings.json`). This is an idempotent check that the tools above are present. It prints a one-line warning if they aren't, so a failed setup script is noticed in the first minute, not after an hour of work.

### A2. What runs where

| Check | Cloud session | CI | Real host |
|---|---|---|---|
| Python unit tests, `node --check`, py_compile | ✅ | ✅ | ✅ |
| `terraform validate` / `fmt`, ansible-lint, yamllint, kustomize | ✅ (after A1) | ✅ | ✅ |
| Jarvis UI in a real browser (`python -m jarvis.demo` + Playwright, Chromium is preinstalled) | ✅ **new** | ✅ **new** | n/a |
| GitOps sync smoke test (k3d cluster in Docker, apply Argo CD bootstrap, wait for `Healthy`) | ✅ **new**, slow (about 10 min) | ✅ **new**, nightly or label-triggered | n/a |
| `terraform plan`/`apply` against libvirt | ❌ (no KVM) | ❌ | ✅ human only |
| `terraform plan` against a cloud provider (Track B) | ⚠️ only with a plan-only credential, never in agent sessions | ✅ plan-only job | ✅ |
| `bootstrap.sh`, Ansible against the host | ❌ | lint only | ✅ human only |

The README already says the GitOps tree was verified on k3s in Docker. A2 turns that one-off check into a repeatable test.

### A3. Agent team in cloud sessions

The `frontend`, `backend` and `adversary` agents in `.claude/agents/` (PR #6) cover Jarvis only. Extend the team rather than widen it:

| Agent | Model | Owns | Notes |
|---|---|---|---|
| `frontend` | Sonnet | `jarvis/static/` | add a Playwright smoke test to its definition of done (A2) |
| `backend` | Sonnet | `jarvis/*.py`, `jarvis/compute/`, `tests/test_jarvis_*` | unchanged |
| `infra` **(new)** | Sonnet | `terraform/`, `cloud-init.yaml.tftpl`, `profiles/`, `detect.py`, `gitops/` | may run only `validate`, `fmt`, `kustomize` and the k3d smoke test. Never `plan`/`apply` against real providers |
| `adversary` | Fable | read-only | add infra checks: public exposure, missing state locking, unbounded cost, secrets in tfvars or cloud-init |

Working loop, the same one used on PR #7:
1. The lead turns a request into scoped tasks, one owner per file set.
2. Workers implement and run the checks for their area, without committing.
3. The adversary reviews each diff and gives a verdict.
4. Workers fix the findings, then the adversary re-reviews.
5. The lead commits, pushes and opens a draft PR.

Protected paths still need the owner's `human-approved` label, and nothing in this plan changes that.

Add a short `CLAUDE.md` at the repo root that captures the conventions every session should know:
- the ownership table
- the protected and immutable paths
- "never apply Terraform or Ansible"
- the `human-approved` label
- running the tests before reporting
- keeping fake key strings in tests split, so the PR secret scan stays clean

### A4. CI additions

1. **`jarvis-e2e` job.** Start `python -m jarvis.demo`, then use Playwright to log in, open every tab, send a chat, and confirm and discard a pending card. Assert that there are no console errors. This closes the "not tested in a browser" gap from PR #7.
2. **`gitops-smoke` job** (nightly, plus the `gitops` label). Create a k3d cluster, apply the cloud-init Argo CD manifests pointed at the PR's head, and wait for `root` and its apps to be `Synced`/`Healthy`.
3. **`terraform fmt -check`** next to the existing `validate`, for every root and module (B1).
4. **Guardrail fix.** Add `terraform/`, `gitops/`, `profiles/` and `detect.py` to `PROTECTED` in `agents/reviewer.py`, so human PRs touching infra also need `human-approved`, and update `tests/test_guardrails.py`. This change is itself a protected-path PR.

**Exit criteria for Track A**
- A fresh cloud session runs `pytest`, `terraform validate`, ansible-lint, yamllint, kustomize and the Jarvis e2e test with no manual installs.
- CI runs `jarvis-e2e` on every PR, and `gitops-smoke` nightly.
- `CLAUDE.md` and the four agent definitions are merged.

---

## Track B: the platform in the cloud

### B0. Decisions needed before building (owner)

| Decision | Options | Recommendation |
|---|---|---|
| Provider | Owner's choice. This plan names no vendor | Choose a provider that has a maintained Terraform provider, private networks, a firewall API, object storage for state and backups, a CSI driver and a budget/alert API. The module interface stays provider-neutral, so adding a provider later is one more module |
| Topology | (a) cloud-only replaces the host; (b) hybrid: host keeps `hub`, cloud runs `dev`/`prod`; (c) cloud burst for temporary clusters | **(b) hybrid.** `hub` with the AI stack and Jarvis stays on the host where local models live, and the cloud runs workload clusters |
| Node OS | Ubuntu LTS cloud image (same as today) | Keep it, so the same cloud-init works |
| Kubernetes | k3s on VMs (same as today) vs. the provider's managed Kubernetes | **k3s on VMs.** It keeps the GitOps tree, the upgrade controller and the operator tools identical. Managed Kubernetes can be a later module |
| Monthly cloud budget | | Set a hard number up front, used by B5 |

### B1. Restructure Terraform into modules

```
terraform/
  modules/
    cloud-init/        # renders cloud-init.yaml.tftpl (shared by every target)
    cluster-libvirt/   # today's network + volumes + domains
    cluster-<provider>/ # network, firewall, servers for the chosen provider
  envs/
    host/              # libvirt root (what `terraform/` is today)
    cloud-dev/         # cloud root for the dev cluster
    cloud-prod/        # cloud root for the prod cluster
```

- Module interface, the same for every provider. Inputs: `cluster`, `nodes` (name, role, primary, ha, size), `ssh_public_key`, `gitops_repo_url`, `gitops_revision`, `k3s_token`, `network_cidr`. Outputs: node private IPs and the primary server's private IP.
- Step 1 is a pure refactor: move today's code into `cluster-libvirt` and `envs/host` with `moved {}` blocks, so an existing host sees **no** resource changes in `terraform plan`.
- Give each cloud env its own `profile` file under `profiles/cloud/` that declares nodes explicitly and maps the `min`/`std`/`max` sizes to instance types. `detect.py` stays host-only.
- Replace the `10.10.10.0/24` assumptions with variables, one non-overlapping CIDR per env, for example `10.20.0.0/16` for cloud-dev.

### B2. State and credentials

- Remote state in S3-compatible object storage with state locking (for example the S3 backend's lockfile, or the provider's native state backend if it has one) and encryption at rest. One state per env. The libvirt env can stay local if you prefer, but it gets the same backend block option.
- Provider credentials live **only** on the owner's machine or in a CI environment protected by required reviewers. They never go in `secrets.env` on the host, in cloud dev sessions, or in agent reach.
- CI gets a separate read-only, plan-only credential for a `terraform plan` job that posts the plan to the PR. `apply` stays a manual, human-run step, matching the current "Terraform is never applied by an agent" rule.

### B3. Networking and access

- Each cloud cluster gets a private network with all node-to-node traffic on private IPs. The provider firewall allows **no** inbound traffic except SSH from a bastion or the VPN, and 80/443 to ingress nodes only if prod serves traffic.
- The Kubernetes API (6443) is never public.
- Host to cloud: a WireGuard (or Tailscale/Headscale) tunnel from the host to a small gateway in each cloud network. This lets host Prometheus, the Operator's read-only kubeconfigs and `scripts/kubeconfig.sh` reach cloud clusters on private IPs.
- GitOps needs nothing new. Argo CD in each cluster pulls from GitHub over outbound HTTPS, which is the main reason this design ports cleanly.

### B4. Cluster bootstrap and GitOps

- Reuse `cloud-init.yaml.tftpl` unchanged, with only three new inputs: an MTU suitable for the provider network, `--node-ip` and `--flannel-iface` set to the private interface, and an optional provider CCM manifest so `LoadBalancer` Services and node addresses work. Leave ServiceLB on until a cloud load balancer is chosen.
- Add `gitops/clusters/cloud-dev` and `cloud-prod`, or reuse `dev`/`prod` with an overlay. They hold the same base apps, plus the provider's CSI driver in place of `longhorn`.
- Secrets: sealed-secrets works as is, with a separate key per cluster, backed up offline. Optionally enable `external-secrets` with the provider's secret manager for prod.
- Backups: enable `velero` against provider object storage for cloud clusters from day one. It is still optional on the host.

### B5. Cost guardrails (copy the RunPod pattern)

- Tag or label every cloud resource with `vmsetup=managed`, `env` and `owner`.
- Set a provider budget alert at 50%, 80% and 100% of the B0 budget, sent to the same `AUTONOMY_NOTIFY_URL` the daily digest uses.
- Extend `agents/cost.py` to read provider billing and usage. Add cloud spend next to LLM and RunPod spend in the Jarvis Overview and the daily digest.
- Run a **reaper** on a timer, like the RunPod reaper. It lists `vmsetup=managed` servers that are not in Terraform state, or that are past a `expires=` label on temporary clusters. It **reports** them in Jarvis and never deletes on its own. Deletion is a confirmed action.
- Use no autoscaling at first. Cluster size changes only through a reviewed PR to the env's profile, then a human `apply`.

### B6. Operator, autonomy and Jarvis

- Add cloud tools to `infra-mcp/` behind the existing policy engine:
  - read-only first: `cloud_list_servers`, `cloud_server_status`, `cloud_spend`
  - later, mutating: `cloud_server_reboot`, `cloud_server_poweroff`, `cloud_snapshot`
- Every mutating cloud tool starts at `approve` in `autonomy.yaml`, with per-target hourly limits and the daily budget. The kill switch, circuit breaker and audit chain apply unchanged.
- No cloud tool creates or destroys servers. Creation and destruction stay in Terraform.
- Infra-MCP cloud tools use a scoped API token for one project only, with read-only access until the mutating tools are enabled. It is held in `/etc/vmsetup/secrets.env` and never exposed to Jarvis chat.
- Jarvis: show cloud clusters and nodes on the Overview tab, with cost from B5. Cloud actions appear in the existing confirm flow, which now claims approvals atomically (PR #7).

### B7. Observability

- Host Prometheus scrapes cloud node exporters and kube-state over the WireGuard tunnel (B3), using a per-env scrape job with file service discovery generated from Terraform outputs.
- Alternatively, run Alloy in each cloud cluster and remote-write to the host. Use this if the tunnel is unavailable, or to avoid scrape traffic over it.
- Add alert rules for tunnel down, a cloud node not Ready, budget at 80% or above, and the reaper finding orphans.

### B8. Security baseline for cloud clusters

- SSH is key-only, with no root login (same as today), and only from the bastion or VPN.
- Kyverno policies from `hub` also apply to cloud clusters, in audit mode first and then enforce mode for prod.
- Keep unattended-upgrades and the system-upgrade-controller as they are.
- The adversary agent's infra review (A3) checks every Track B PR for public exposure, missing locking or encryption, credentials in tfvars or cloud-init, and unbounded `count`/`for_each`.

---

## 2. Roadmap

Each phase ends in a merged PR (or a few), green CI and the adversary's approval. Applies are done by the owner.

| Phase | Scope | Exit criteria | Size |
|---|---|---|---|
| **0. Decide** | B0 decisions, budget number | Decisions recorded in this file | owner, ~1 h |
| **1. Cloud dev env** | A1 setup script, network allowlist, SessionStart hook, `CLAUDE.md` | A fresh session runs every CI check locally with no installs | S |
| **2. CI parity** | A4: `jarvis-e2e`, `terraform fmt`, guardrail fix to `PROTECTED` | Jarvis e2e is green on PRs. Infra PRs need `human-approved` | S–M |
| **3. GitOps smoke** | A4: k3d `gitops-smoke` job | Nightly job green for `hub`/`dev`/`prod` trees | M |
| **4. Terraform refactor** | B1 modules plus `moved` blocks, no new provider yet | `terraform plan` on the existing host shows **0 changes** | M |
| **5. State and creds** | B2 remote state, plan-only CI job | PRs touching `terraform/` get a posted plan. State is locked and encrypted | S–M |
| **6. First cloud cluster (dev)** | `cluster-<provider>`, `envs/cloud-dev`, B3 tunnel, B4 bootstrap | `cloud-dev` nodes `Ready`, Argo CD `Healthy`, reachable from the host over the tunnel only | M–L |
| **7. Ops wiring** | B5 cost and reaper (report-only), B7 scraping and alerts, Jarvis Overview | Spend and nodes visible in Jarvis. Budget and orphan alerts fire in a test | M |
| **8. Operator tools** | B6 read-only cloud tools, then mutating tools at `approve` | Tools pass the eval gate. Mutating actions queue in Jarvis and run once on confirm | M |
| **9. Prod in cloud** | `envs/cloud-prod`, Velero, Kyverno enforce, ingress if needed | Restore drill from a Velero backup succeeds. Prod runs for 2 weeks on budget | L |

Phases 1–3 need nothing from the cloud provider, so they can start immediately.

## 3. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Surprise cloud bill | Hard budget alerts (B5), no autoscaling, reaper reports, sizes only through a reviewed PR, a human-only `apply` |
| Credential leak | Provider credentials only on the owner's machine or in a protected CI environment, plan-only token in PRs, nothing in agent reach, the PR secret scan stays on |
| Refactor breaks the existing host | Phase 4 is gated on a zero-change `plan`, using `moved {}` blocks |
| Tunnel outage blinds monitoring | Alloy remote-write as a fallback, plus a "tunnel down" alert. GitOps keeps working without the tunnel |
| Agents gaining infra reach | `terraform/`, `ansible/` and `gitops/` stay IMMUTABLE for agent branches. Cloud tools are `approve` and cannot create or destroy |
| Cloud sessions can't test libvirt | Accepted. The libvirt path is covered by `validate`, the zero-change plan on the host, and the host first-boot checklist |

## 4. Open questions for the owner

1. Provider: which one? It must meet the B0 requirements.
2. Topology: is hybrid (host `hub` plus cloud `dev`/`prod`) right, or should the host be retired eventually?
3. Monthly cloud budget, and who gets budget alerts?
4. Should prod in the cloud serve public traffic (ingress, DNS, TLS through cert-manager), or stay private behind the VPN?
5. Should IP addresses be visible to the Jarvis chat model? This is relevant once cloud IPs matter. PR #7 redacts them.
