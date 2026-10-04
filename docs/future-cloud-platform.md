# Future: running the platform on a cloud provider

> **Status: future option, not current work.** The current cloud work is developing this repo in Claude Code cloud sessions; see [`cloud-dev-plan.md`](cloud-dev-plan.md). This document is kept so the platform-in-the-cloud design, which has already been reviewed against the repo, isn't lost.

It covers letting vm-setup create k3s clusters on a cloud provider (Hetzner Cloud is the candidate for later) alongside the existing KVM/libvirt fleet, keeping the same GitOps, monitoring, AI operator and guardrails.

## 1. Starting point (as of `main` @ a8bb19c)

| Area | Today | Matters for cloud because |
|---|---|---|
| Infra | `terraform/main.tf` only uses the `dmacvicar/libvirt` provider. One NAT network is fixed at `10.10.10.0/24`, and node IPs and sizes come from `profile.generated.json` | There is no provider abstraction. Node creation, network and cloud-init are mixed together in one file |
| Terraform state | Local only (`*.tfstate*` is gitignored), with no backend block | A cloud fleet needs shared, locked, encrypted state |
| Bootstrap | `terraform/cloud-init.yaml.tftpl` installs k3s and, on primary servers, Argo CD plus a `root` Application that pulls `gitops/clusters/<name>`. `K3S_URL` comes from the static IP plan in `main.tf` | The Argo CD bootstrap is portable. The k3s join inputs are not: on a cloud provider the primary's private IP is only known after it is created |
| GitOps | Argo CD app-of-apps per cluster (`hub`, `dev`, `prod`). Clusters pull from Git, nothing pushes to them | Works over any network that can reach GitHub. No inbound access is needed |
| Sizing | `detect.py` reads host hardware into a `min`/`std`/`max` profile | A cloud cluster has no host to detect. It needs a declared profile |
| Operator | `infra-mcp/` has allowlisted libvirt tools. Confirmation is keyed on a hard-coded name set, `MUTATING` in `agents/runtime.py`, which `jarvis/chat.py` reuses, plus the `infra` allow-regex in `agents/mcp_servers.yaml` and entries in `autonomy.yaml` | A new mutating tool missing from `MUTATING` would be treated as read-only and run without confirmation (see B6) |
| Compute | Jarvis rents GPU pods and runs hosted notebooks (`jarvis/compute/`), with price caps, a lifetime limit and a reaper | This is the pattern to copy for cloud VMs |
| Guardrails | `agents/reviewer.py`: agent PRs may never touch `terraform/`, `ansible/` or `gitops/` (IMMUTABLE). Human PRs under `jarvis/`, `agents/` and others need the `human-approved` label | `PROTECTED` leaves most infra paths open for human PRs: `terraform/`, `gitops/`, `profiles/`, `detect.py`, `bootstrap.sh`, `scripts/`, and every Ansible role except `ai_stack` and `jarvis`. `.github/CODEOWNERS` lists neither `terraform/` nor `gitops/` |
| CI | `ci.yml`: `terraform validate`, ansible-lint, yamllint, py_compile, `node --check`, pytest, and `kubectl kustomize` for each cluster | There is no browser test of Jarvis and no live sync test of GitOps |
| Cloud dev session | See [`cloud-dev-plan.md` §1](cloud-dev-plan.md#1-what-a-cloud-session-is) | Libvirt VMs can never run in a cloud session, so applies stay on the owner's host |

---

### B0. Decisions needed before building (owner)

| Decision | Options | Recommendation |
|---|---|---|
| Provider | Hetzner Cloud is the candidate for later; final choice is the owner's | Whichever is chosen must have a maintained Terraform provider, private networks, a firewall API, object storage for state and backups, and a CSI driver. A budget/alert API is a plus (see B5). The module interface stays provider-neutral, so adding a provider later is one more module |
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
- Step 1 is a pure refactor: move today's code into `cluster-libvirt` and `envs/host` with `moved {}` blocks, so an existing host sees **no** resource changes in `terraform plan`. That exit criterion is reachable only if the same PR also:
  - moves the existing `terraform/terraform.tfstate` to `envs/host/`, or the owner runs the documented `terraform state mv` steps;
  - fixes `var.profile_file`'s default path (`../profile.generated.json` becomes `../../../profile.generated.json`);
  - updates every `terraform -chdir=terraform` caller: the `ci.yml` validate step (a protected path) and the README quick start.
- Give each cloud env its own `profile` file under `profiles/cloud/` that declares nodes explicitly and maps the `min`/`std`/`max` sizes to instance types. `detect.py` stays host-only.
- Replace the `10.10.10.0/24` assumptions with variables, one non-overlapping CIDR per env, for example `10.20.0.0/16` for cloud-dev. The network is hard-coded in more places than Terraform: `main.tf` (the `/24` and the `10.10.10.1` gateway), `detect.py` (`NETWORK_PREFIX`), `scripts/kubeconfig.sh` and the README.

### B2. State and credentials

- Remote state in object storage with state locking and encryption at rest, using whichever Terraform backend the chosen provider supports. One state per env. The libvirt env can stay local if you prefer, but it gets the same backend block option.
- **Read access to state is the same as cluster admin.** `random_password.k3s_token` is stored in state in plaintext, and anyone holding a join token can join a node. Only the owner and the plan-only CI credential can read state. No agent, reaper or Jarvis component ever reads it (see B5).
- Provider credentials live **only** on the owner's machine or in a CI environment protected by required reviewers. They never go in `secrets.env` on the host, in cloud dev sessions, or in agent reach.
- CI gets a separate read-only, plan-only credential for a `terraform plan` job that posts the plan to the PR. `apply` stays a manual, human-run step, matching the current "Terraform is never applied by an agent" rule.

### B3. Networking and access

- Each cloud cluster gets a private network with all node-to-node traffic on private IPs. The provider firewall allows **no** inbound traffic except SSH from a bastion or the VPN, and 80/443 to ingress nodes only if prod serves traffic.
- The Kubernetes API (6443) is never public.
- The provider firewall is the **only** control in front of node public addresses. Both k3s ServiceLB and Traefik (80/443) bind on every node's addresses. Terraform creates the firewall before any server and attaches it at creation time (`depends_on`), so a server is never up without it. Prefer nodes without public IPs where the provider allows it.
- Host to cloud: a WireGuard tunnel from the host to a small gateway in each cloud network. This lets host Prometheus, the Operator's read-only kubeconfigs and `scripts/kubeconfig.sh` reach cloud clusters on private IPs. `kubeconfig.sh` currently reads node IPs from `profile.generated.json`, so extend it to read `terraform -chdir=envs/<env> output -json` for cloud envs.
- GitOps needs nothing new. Argo CD in each cluster pulls from GitHub over outbound HTTPS, which is the main reason this design ports cleanly.

### B4. Cluster bootstrap and GitOps

- Move `terraform/cloud-init.yaml.tftpl` into `modules/cloud-init` and keep its Argo CD bootstrap as is. Add one new variable, `k3s_extra_args`, carrying the MTU, `--node-ip`, `--flannel-iface` and `--tls-san` for the private interface. Feed `k3s_url` from the provider's primary-server private IP (a resource attribute), not from the static profile. Optionally add the provider's cloud-controller manifest so `LoadBalancer` Services and node addresses work. Leave ServiceLB on until a cloud load balancer is chosen, behind the firewall from B3.
- Add `gitops/clusters/cloud-dev` and `cloud-prod`, or reuse `dev`/`prod` with an overlay. They hold the same base apps, plus the provider's CSI driver. `longhorn` stays optional and isn't needed there.
- Secrets: sealed-secrets works as is, with a separate key per cluster, backed up offline. Optionally enable `external-secrets` with the provider's secret manager for prod.
- Backups: enable `velero` against provider object storage for cloud clusters from day one. It is still optional on the host.

### B5. Cost guardrails (copy the existing GPU-rental pattern)

- Tag or label every cloud resource with `vmsetup=managed`, `env` and `owner`.
- Alert at 50%, 80% and 100% of the B0 budget, sent to the same `AUTONOMY_NOTIFY_URL` the daily digest uses. If the provider offers budget alerts, route those there. Otherwise `agents/cost.py` computes spend from price × uptime per server and raises the alerts itself, as it already does at 80% of the LLM budget.
- Extend `agents/cost.py` to read provider billing and usage. Add cloud spend next to LLM and GPU-rental spend in the Jarvis Overview and the daily digest.
- Run a **reaper** on a timer, like the existing GPU-pod reaper (`jarvis/reaper.py`). It **never reads Terraform state** (B2). Terraform labels every server `vmsetup=managed`, `env=<env>` and `tf=1`. The reaper compares the provider inventory against a non-secret node list: `terraform output -json nodes`, written by the owner after each apply. It flags managed servers missing from that list, unlabelled servers older than N hours, and servers past an `expires=` label on temporary clusters. It **reports** them in Jarvis and never deletes on its own. Deletion is a confirmed action.
- Use no autoscaling at first. Cluster size changes only through a reviewed PR to the env's profile, then a human `apply`.

### B6. Operator, autonomy and Jarvis

- Add cloud tools to `infra-mcp/` behind the existing policy engine:
  - read-only first: `cloud_list_servers`, `cloud_server_status`, `cloud_spend`
  - later, mutating: `cloud_server_reboot`, `cloud_server_poweroff`, `cloud_snapshot`
- Every mutating cloud tool starts at `approve` in `autonomy.yaml`, with per-target hourly limits and the daily budget. The kill switch, circuit breaker and audit chain apply unchanged.
- Gating is a hard-coded name list, so each mutating cloud tool must, **in the same PR**:
  1. keep `dry_run: bool = True` as its default;
  2. be added to `MUTATING` in `agents/runtime.py`, which both the Operator and Jarvis chat use to decide what needs confirmation;
  3. be added to the `infra` allow-regex in `agents/mcp_servers.yaml`;
  4. get an entry in `autonomy.yaml`, because the policy denies unknown actions;
  5. name its target argument exactly `domain`, because `gate` reads `args["domain"]` literally (`agents/runtime.py`, `jarvis/chat.py`). Any other name means changing both files.

  Add a guardrail test asserting that every infra-mcp tool with a `dry_run` parameter is in `MUTATING`. All these files are IMMUTABLE, so this is a human-authored PR with `human-approved`.
- No cloud tool creates or destroys servers. Creation and destruction stay in Terraform.
- Infra-MCP cloud tools use a scoped API token for one project only, with read-only access until the mutating tools are enabled. It is held in `/etc/vmsetup/secrets.env` and never exposed to Jarvis chat. Do **not** add it to the Setup tab or to `jarvis/apply_secrets.py`'s allowlist.
- Jarvis: show cloud clusters and nodes on the Overview tab, with cost from B5. Cloud actions appear in the existing confirm flow, which claims approvals atomically since the Jarvis audit fixes.

### B7. Observability

- Host Prometheus scrapes cloud node exporters and kube-state over the WireGuard tunnel (B3), using a per-env scrape job with file service discovery generated from Terraform outputs.
- Alternatively, run Alloy in each cloud cluster and remote-write to the host. Use this if the tunnel is unavailable, or to avoid scrape traffic over it.
- Add alert rules for tunnel down, a cloud node not Ready, budget at 80% or above, and the reaper finding orphans.

### B8. Security baseline for cloud clusters

- SSH is key-only, with no root login (same as today), and only from the bastion or VPN.
- Kyverno policies from `hub` also apply to cloud clusters, in audit mode first and then enforce mode for prod.
- Keep unattended-upgrades and the system-upgrade-controller as they are.
- cloud-init user-data carries `K3S_TOKEN` and the Argo CD bootstrap in plaintext, and the provider's metadata endpoint (usually `169.254.169.254`) serves user-data to any process on the node. Block pod access to the metadata endpoint with a network policy or a Kyverno policy. Rotate the join token after the cluster is formed (`k3s token rotate`).
- The adversary agent's infra review (see the dev plan's agent team) checks every PR for this work for public exposure, missing locking or encryption, credentials in tfvars or cloud-init, and unbounded `count`/`for_each`.

---

## 2. Roadmap

Each phase ends in merged PRs, green CI and the adversary's approval. Applies are done by the owner. Start only after the development plan's phases are done.

| Phase | Scope | Exit criteria | Size |
|---|---|---|---|
| **0. Decide** | B0 decisions, budget number | Decisions recorded in this file | owner, ~1 h |
| **1. Terraform refactor** | B1 modules plus `moved` blocks, no new provider yet | `terraform plan` on the existing host shows **0 changes** | M |
| **2. State and creds** | B2 remote state, plan-only CI job | PRs touching `terraform/` get a posted plan. State is locked and encrypted | S–M |
| **3. First cloud cluster (dev)** | `cluster-<provider>`, `envs/cloud-dev`, B3 tunnel, B4 bootstrap | `cloud-dev` nodes `Ready`, Argo CD `Healthy`, reachable from the host over the tunnel only | M–L |
| **4. Ops wiring** | B5 cost and reaper (report-only), B7 scraping and alerts, Jarvis Overview | Spend and nodes visible in Jarvis. Budget and orphan alerts fire in a test | M |
| **5. Operator tools** | B6 read-only cloud tools, then mutating tools at `approve` | Tools pass the eval gate. Mutating actions queue in Jarvis and run once on confirm | M |
| **6. Prod in cloud** | `envs/cloud-prod`, Velero, Kyverno enforce, ingress if needed | Restore drill from a Velero backup succeeds. Prod runs for 2 weeks on budget | L |

## 3. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Surprise cloud bill | Hard budget alerts (B5), no autoscaling, reaper reports, sizes only through a reviewed PR, a human-only `apply` |
| Credential leak | Provider credentials only on the owner's machine or in a protected CI environment, plan-only token in PRs, nothing in agent reach, the PR secret scan stays on |
| Refactor breaks the existing host | Phase 1 is gated on a zero-change `plan`, using `moved {}` blocks |
| Tunnel outage blinds monitoring | Alloy remote-write as a fallback, plus a "tunnel down" alert. GitOps keeps working without the tunnel |
| Agents gaining infra reach | `terraform/`, `ansible/` and `gitops/` stay IMMUTABLE for agent branches. Cloud tools are `approve` and cannot create or destroy |
| Cloud sessions can't test libvirt | Accepted. The libvirt path is covered by `validate`, the zero-change plan on the host, and the host first-boot checklist |

## 4. Open questions for the owner

1. Provider: confirm Hetzner Cloud when this starts, against the B0 requirements.
2. Topology: is hybrid (host `hub` plus cloud `dev`/`prod`) right, or should the host be retired eventually?
3. Monthly cloud budget, and who gets budget alerts?
4. Should prod in the cloud serve public traffic (ingress, DNS, TLS through cert-manager), or stay private behind the VPN?
5. Should IP addresses be visible to the Jarvis chat model? This is relevant once cloud IPs matter. Tool output to the model is currently redacted, which hides IPs.
