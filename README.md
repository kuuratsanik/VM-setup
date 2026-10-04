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
| 8. Operator | `curl -X POST 127.0.0.1:8085 -d '{"alerts":[{"status":"firing","labels":{"alertname":"GuestDown"}}]}'` | a new row in `/var/lib/vmsetup/incidents.jsonl` and, with vector memory on, in the `incidents` table (mutating tools never run for real until the autonomy policy allows it: `agents/autonomy.yaml`, default `mode: supervised`, so they are queued for approval in Jarvis) |

Verified without hardware: the Argo CD bootstrap manifests from cloud-init and the `dev` GitOps tree were applied to a real k3s (Docker) and synced to Healthy (Argo CD, root app, cert-manager, sealed-secrets, system-upgrade-controller plans), and the `ai-readonly` token can read pods, nodes, events and Argo CD applications but not secrets, and cannot create or delete anything. The empty `K3S_URL` on first servers is accepted by the k3s installer.

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
- `agents/`: `runtime.py` (Operator, multi-MCP via `mcp_servers.yaml`), `capacity.py`, `upgrade.py`, `cost.py`, `librarian.py`, `models.py`, `evolve.py`, `deploy.py`, `reviewer.py` (PR gate). Prompts live in `agents/prompts/`, runbooks in `agents/runbooks/`.
- `evals/cases.yaml` + `agents/evalgate.py`: tool-choice and prompt-injection cases that gate model, prompt and tuned-model changes. `evals/promptfooconfig.yaml` is an optional promptfoo version.
- `tests/`: unit tests (`python -m pytest -q tests`), run in CI.
- `media-mcp/`: MCP server for image, speech, transcription, vision and video through the gateway.
- `training/`: dataset export, hybrid fine-tuning (cloud or local LoRA) and two-stage promotion.
- `jarvis/`: the Jarvis web dashboard (see below).
- Ansible creates `/etc/vmsetup/secrets.env` (gateway key generated, provider keys blank). Set `langfuse_enabled: true` in `profile.override.yaml` plus the Langfuse keys for tracing.
- Design choices: k3s Traefik and ServiceLB are kept (no MetalLB or ingress-nginx); node OS patching uses unattended-upgrades.

## AI platform
| Capability | How |
|---|---|
| Models | LiteLLM gateway: `default` (local Hermes on `std`/`max`, cloud on `min`), `cloud-small`, `cloud-frontier`, optional `cloud-hermes` (OpenRouter). `agents/models.py` finds newer cloud models weekly, gates them, and opens a PR |
| Local inference | `std`: Ollama `hermes3:8b`. `max`: vLLM `NousResearch/Hermes-4-14B-FP8` (needs NVIDIA CDI) |
| Agent tools (MCP) | `agents/mcp_servers.yaml`: infra (dry-run), Kubernetes (read-only ServiceAccount), Prometheus, Argo CD (read-only, needs `ARGOCD_*`) |
| In-cluster AI | k8sgpt-operator on `hub` (analysis only; the Operator reads its `Result` objects) |
| Memory and RAG | Postgres + pgvector (`agents/memory.py`); the Operator retrieves similar past incidents. `agents/feedback.py mark <id> good|bad` steers retrieval |
| Observability | Phoenix (`127.0.0.1:6006`, `std`/`max`), optional Langfuse cloud, promptfoo, eval gate |
| Media | `media-mcp` tools through LiteLLM aliases `image`, `tts`, `stt` (cloud, or LocalAI on `max`); video needs a gateway/provider with the `/v1/videos` API (untested against a real provider) |
| Training | `training/export_dataset.py` (human-approved incidents, redacted) then `training/train.py --provider openai --yes` or `--provider local --run`; `training/promote.py candidate|default` (default needs the eval gate to pass) |
| Coding PRs | label an issue `agent-fix` (owner only) for the Claude Code action, or assign it to Copilot; `agents/evolve.py` proposes changes to prompts, runbooks and non-critical agent code |

First-boot extras: after the clusters are up and `ai-readonly` has synced, run `sudo ./scripts/ai_kubeconfig.py`. Put a fine-grained `GH_TOKEN` (contents and pull requests write) in `/etc/vmsetup/secrets.env` for the PR-opening agents. Training stays off until `ai.training.enabled: true` is set in `profile.override.yaml`, and `train.py` uploads redacted data only with `--yes` and under the `max_usd` cap.

## Jarvis (web dashboard)
Runs as the unprivileged `jarvis` user on `127.0.0.1:8088`; reach it with `ssh -L 8088:127.0.0.1:8088 host` or a TLS reverse proxy (then set `JARVIS_HTTPS=1` in `/etc/vmsetup/jarvis.env`).

1. Create the owner account (there is no default login): `sudo -u jarvis env JARVIS_STATE_DIR=/var/lib/vmsetup/jarvis PYTHONPATH=/opt/vm-setup /opt/vm-setup/.venv/bin/python -m jarvis.manage set-password`. Optional TOTP: `enroll-totp`, then `confirm-totp CODE`.
2. Tabs: Overview (services, nodes, spend), Chat (streaming, hybrid local/cloud models, tools, voice, image), Compute (RunPod and Kaggle), Setup (provider onboarding), Incidents.
3. Chat tools reuse `agents/mcp_servers.yaml`. Anything that changes state (VM start/stop/snapshot, renting a GPU, running a notebook) is only proposed; you press Confirm. Infra actions follow the autonomy policy (below).

Accounts and sign-in: you create the accounts yourself (RunPod, Kaggle, OpenAI, Anthropic, OpenRouter, Hugging Face, GitHub). Jarvis does not automate registration, CAPTCHA, or email/phone verification, which provider terms generally forbid. The Setup tab opens each sign-up and token page, lists the steps, checks the pasted token with a harmless read-only call, and hands it to a root helper (`jarvis/apply_secrets.py`, allowlisted names and strict value patterns only) that writes it to `/etc/vmsetup` and restarts the consumers. Chat refuses messages that look like keys.

Compute: RunPod pods go through a confirmation, an hourly price cap, a maximum lifetime (default 4 h) and a reaper timer that deletes expired or untracked `jarvis-*` pods every 10 minutes. Kaggle notebooks run private with internet off and use your free weekly GPU quota. Both are tested against mocks only, not against live accounts.

Dashboard security: Argon2id password, optional TOTP, lockout after 5 failures per address, `SameSite=Strict` HttpOnly session, custom header plus Origin check on every write, strict CSP, no secrets ever returned by the API.

## Autonomy: what runs by itself and what waits for you
Every state-changing action passes through one policy engine (`agents/policy.py`, configured in `agents/autonomy.yaml`). The mode is one line:

| Mode | Behaviour |
|---|---|
| `dry_run` | tools only describe what they would do |
| `supervised` (shipped default) | every action is queued; you approve it in Jarvis > Autonomy and get a notification |
| `autonomous` | each action follows its level: `auto` runs inside budgets, `approve` queues, `deny` is refused |

Safety layers that stay on in every mode: kill switch (`/etc/vmsetup/AGENTS_PAUSED`), per-target hourly limits and a daily action budget, a circuit breaker (repeated failures degrade everything to approval until you reset it), snapshot-before-stop, and a hash-chained audit log (`python agents/policy.py verify-audit`). Human-approved successes build a trust ledger; a long clean streak is suggested in the daily digest and Jarvis as evidence for promoting an action to `auto`, which is itself a reviewed PR.

To move toward autonomy: run in `supervised` for a while, then change `mode: autonomous` in a reviewed PR once the ledger and audit log look right. In `autonomous` as shipped, snapshot and start run by themselves; stop needs approval.

Humans stay in the loop for: changing the autonomy policy, guardrail files, infrastructure applies (Terraform/Ansible), spending money (RunPod confirmations), deleting things, account registration, and any code, prompt, or workflow change outside the low-risk tier. Low-risk agent PRs (runbooks under `agents/runbooks/`, text under `proposals/`) auto-merge after `ci` and `agent-review` pass (`agents/automerge.py`, `.github/workflows/auto-merge.yml`; hold one back with a `hold` label). A daily digest (`agents/digest.py`) sends one message to `AUTONOMY_NOTIFY_URL` when something needs you.

## Self-improvement and its limits
Self-healing (Operator, Argo CD `selfHeal`, systemd restarts), self-upgrading (`deploy.py` rolls out approved `main` commits with automatic rollback; Renovate; k3s system-upgrade-controller; unattended-upgrades) and self-coding (`evolve.py`, `agent-fix`) exist, but none of it is autonomous in the sense of unchecked:
- Agents change themselves only through pull requests. Only the low-risk tier (runbooks, proposals) merges itself after the checks pass; everything else needs you, and Terraform/Ansible are never applied by an agent.
- Agent-authored PRs (branch `agent/*`) cannot touch the files in `IMMUTABLE` in `agents/reviewer.py` (reviewer, evolve, deploy, runtime, MCP allowlist, eval cases, manifest, workflows, infrastructure code), even with the label. Other protected paths need `human-approved`.
- `evolve.py` must pass the tests and, for prompt changes, the eval gate (including every prompt-injection case), is limited to 3 files and 200 changed lines, and runs at most once a day.
- `deploy.py` deploys only commits from merged PRs (protected paths need the label), runs the tests first, and rolls back if the new version is unhealthy.
- The kill switch `/etc/vmsetup/AGENTS_PAUSED` stops the Operator and Evolve. Add `agents/` to branch protection with required code-owner review (`.github/CODEOWNERS`).
This is a bounded automation loop with human approval where it matters, not AGI.
- `ansible/`: host hardening, AI gateway, monitoring, operator agent service.
- `agents/manifest.yaml`: agent team, autonomy levels, guardrails.
- `infra-mcp/`: allowlisted MCP server for the node VMs (dry-run by default, gated by the autonomy policy, kill switch at `/etc/vmsetup/AGENTS_PAUSED`).

Put API keys in `/etc/vmsetup/secrets.env` (root-only, never committed). The image URL defaults to the Ubuntu 26.04 LTS minimal cloud image; the Upgrade agent opens a PR when a newer LTS appears.