# Cloud development plan: building vm-setup in Claude Code cloud sessions

**Goal:** every change to this repo can be planned, built, tested, reviewed and opened as a PR from a Claude Code cloud session, started from the web, the mobile app or `claude --cloud`. The owner's machine is needed only for things that touch the real host: `bootstrap.sh`, Ansible and `terraform apply` against libvirt.

Running the platform itself on a cloud provider is a separate, later topic. See [`future-cloud-platform.md`](future-cloud-platform.md).

Reference docs: [cloud environments](https://code.claude.com/docs/en/cloud-environments), [Claude Code on the web](https://code.claude.com/docs/en/claude-code-on-the-web), [routines](https://code.claude.com/docs/en/routines), [hooks](https://code.claude.com/docs/en/hooks).

## 1. What a cloud session is

| Fact | Detail | Consequence for this repo |
|---|---|---|
| Machine | Fresh Ubuntu VM per session, about 4 vCPU, 16 GB RAM and about 30 GB of free disk. It is reclaimed after the session ends or sits idle | Anything not pushed is lost. Push work-in-progress commits to the session branch |
| Repo | Cloned fresh from GitHub at the session's branch. GitHub auth goes through a proxy, so no token is needed in the VM | Local, uncommitted work must be pushed before `claude --cloud` sees it |
| Setup script | Runs before Claude starts. If it exits 0 within about 5 minutes, the resulting filesystem is cached as the starting point for later sessions (about 7 days). The cache is rebuilt when the script or network setting changes | Install tools in the setup script, not by hand, and keep it under 5 minutes |
| Repo config | Once committed, `CLAUDE.md`, `.claude/settings.json` (hooks, permissions), `.claude/agents/`, `.claude/skills/` and `.mcp.json` are read from the repo. User-level `~/.claude` does not carry over. Today only `.claude/agents/` exists | Everything the team needs must be committed (§4) |
| Detection | `CLAUDE_CODE_REMOTE=true` in every cloud session | Hooks can do cloud-only work |
| Docker | Docker is preinstalled, but no daemon was running when we checked (no systemd) | Start `dockerd` in the setup script or hook before k3d tests |
| Browser | Playwright's Chromium is preinstalled (`PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`). The Python `playwright` package is not | Browser tests of Jarvis are possible after a `pip install` |
| Virtualization | **No `/dev/kvm`** | libvirt VMs, `bootstrap.sh` and `terraform plan`/`apply` against libvirt can never run in the cloud |
| Command limits | Foreground commands time out after up to 10 min. Longer jobs run in the background | Run slow suites, such as the k3d smoke test, in the background |
| Secrets | Environment variables are visible to everyone who uses the environment. **API credentials** (where offered) are attached by the proxy and never reach the VM | No provider keys in sessions. Anything secret goes through API credentials |

## 2. What runs where

| Check | Cloud session | CI | Owner's host |
|---|---|---|---|
| pytest, py_compile, `node --check` | ✅ | ✅ | ✅ |
| `terraform validate`/`fmt`, ansible-lint, yamllint, `kubectl kustomize` | ✅ after §3 | ✅ | ✅ |
| Jarvis in a real browser (`python -m jarvis.demo` + Playwright) | ✅ **new** | ✅ **new** | n/a |
| GitOps sync smoke test (k3d in Docker, Argo CD bootstrap, wait for `Healthy`) | ⚠️ **new**, if `dockerd` starts | ✅ **new**, nightly or labelled | n/a |
| Agent eval gate (`agents/evalgate.py`) | ⚠️ needs a LiteLLM-compatible endpoint (§3.3); CI-only until then | ✅ if CI has one | ✅ |
| `terraform plan`/`apply` (libvirt), `bootstrap.sh`, Ansible runs | ❌ | ❌ (lint only) | ✅ owner only |

## 3. Environment configuration (one time, in the cloud environment's settings)

### 3.1 Setup script

```bash
set -euo pipefail
# Pins: bump here only.
TF=1.10.5; KUBECTL=v1.31.4; HELM=v3.16.3; K3D=v5.7.4
PLAYWRIGHT=""      # SET ME: the release whose browsers.json lists Chromium revision 1194 (matches /opt/pw-browsers/chromium-1194)

pip install -q -r agents/requirements.txt -r jarvis/requirements.txt pytest pytest-asyncio ansible-lint yamllint
ansible-galaxy collection install -r ansible/requirements.yml || echo "WARN: ansible-galaxy failed (check network allowlist)"
curl -fsSL https://releases.hashicorp.com/terraform/$TF/terraform_${TF}_linux_amd64.zip -o /tmp/tf.zip \
  && unzip -oq /tmp/tf.zip terraform -d /tmp && mv /tmp/terraform /usr/local/bin/
curl -fsSL https://dl.k8s.io/release/$KUBECTL/bin/linux/amd64/kubectl -o /usr/local/bin/kubectl && chmod +x /usr/local/bin/kubectl
curl -fsSL https://get.helm.sh/helm-$HELM-linux-amd64.tar.gz | tar -xz -C /tmp && mv /tmp/linux-amd64/helm /usr/local/bin/
curl -fsSL https://github.com/k3d-io/k3d/releases/download/$K3D/k3d-linux-amd64 -o /usr/local/bin/k3d && chmod +x /usr/local/bin/k3d

# Last, so a wrong pin can't abort the steps above. Reuses the preinstalled Chromium.
if [ -n "$PLAYWRIGHT" ]; then PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 pip install -q "playwright==$PLAYWRIGHT" || echo "WARN: playwright $PLAYWRIGHT failed"; else echo "WARN: PLAYWRIGHT pin not set"; fi
```

- Keep the pins equal to CI's. To find the Playwright pin, run `pip download --no-deps playwright==<ver>` and look for `"revision": "1194"` in the wheel's `browsers.json`.
- Optionally, commit this script as `scripts/cloud-setup.sh` and make the environment's setup script just `bash scripts/cloud-setup.sh`. The pins are then versioned and reviewable. The trade-off: the cache rebuilds only when the one-line wrapper or the network setting changes, not when the repo file does, so bump the wrapper with a comment when pins change.
- The cache keeps files, not running processes. So `dockerd` is started by the SessionStart hook (§4.2), not here.
- Measure the script once. If it takes more than about 5 minutes, it isn't cached and every session pays for it again. In that case drop the slowest step (usually ansible-galaxy) into the hook.

### 3.2 Network access: Custom

Keep the default package-manager list and add:
- Tools: `releases.hashicorp.com`, `registry.terraform.io`, `dl.k8s.io`, `cdn.dl.k8s.io`, `get.helm.sh`, `github.com`, `objects.githubusercontent.com`, `galaxy.ansible.com` and the download host it redirects to (check with `curl -sIL`).
- Helm chart repos used by `gitops/apps/*` (regenerate with `grep -rh repoURL gitops/apps | sort -u`):
  - `argoproj.github.io`, `bitnami.github.io`, `charts.external-secrets.io`, `charts.jetstack.io`
  - `charts.k8sgpt.ai`, `charts.longhorn.io`, `cloudnative-pg.github.io`, `grafana.github.io`
  - `kyverno.github.io`, `netdata.github.io`, `prometheus-community.github.io`, `vmware-tanzu.github.io`
- Container images for k3d: `ghcr.io`, `registry-1.docker.io`, `quay.io`, `registry.k8s.io`, plus the blob CDN hosts they redirect to. Pull one image once and add the hosts the proxy reports.

Use Custom rather than Full, so a prompt injection in a PR comment or fetched page can't send data anywhere.

### 3.3 Environment variables and credentials

- Only non-secret variables go here (none are needed today).
- Nothing from `/etc/vmsetup/secrets.env` is ever put in the environment. That means no cloud-provider, GPU-rental or LLM provider keys.
- The eval gate (`agents/evalgate.py`) talks to the LiteLLM gateway (`LITELLM_URL`, default `127.0.0.1:4000`) using the gateway's model aliases, not to a provider directly. A session has no gateway, so to run it there, choose one:
  - (a) run the gateway container in the session (needs Docker, same caveat as k3d), with one low-budget provider key as a proxy-attached **API credential**; or
  - (b) point `LITELLM_URL` at a provider's OpenAI-compatible endpoint and map the alias names.

  Until one of these is set up, the eval gate is CI-only.

## 4. Repo configuration (committed, so every session gets it)

### 4.1 `CLAUDE.md` at the repo root

Short, with rules only:
- **Ownership table** (§5).
- **Protected and immutable paths** from `agents/reviewer.py`, and that changes there need the owner's `human-approved` label.
- **Never** run `bootstrap.sh`, Ansible against a host, or `terraform plan`/`apply` against libvirt, from any session.
- **Before reporting:** run the checks in §6 for the area you touched.
- **Fake key strings in tests:** build them from pieces, such as `"-----BEGIN " + "PRIVATE KEY-----"`, so the PR secret scan stays clean.
- **Pushing:** push work-in-progress often, because the VM is ephemeral.
- **PRs:** open them as drafts, and squash-merge.

### 4.2 `.claude/settings.json`

- A **SessionStart hook**, cloud-only (`if [ "$CLAUDE_CODE_REMOTE" = "true" ]`). It:
  - starts `dockerd` in the background if it isn't running;
  - checks that `terraform`, `kubectl`, `helm`, `k3d`, `ansible-lint` and the `playwright` module are present;
  - prints one warning line for anything missing, so a broken setup script is noticed in the first minute.

  It must stay quick and idempotent, because it runs on every start and resume.
- **Permissions:** allow the read-only and check commands (`python -m pytest`, `terraform validate`/`fmt`, `ansible-lint`, `yamllint`, `kubectl kustomize`, `node --check`, `k3d cluster create/delete`, `docker info` and `docker system prune` for the smoke test), so sessions don't stall on prompts. The hook itself runs outside the permission system. Leave `terraform plan`/`apply`, `ansible-playbook` and `git push --force` out.

### 4.3 Skills (`.claude/skills/`)

- **`checks`**: runs the CI-equivalent checks for the paths changed against `origin/main` and prints a pass/fail table. Workers and the lead use this one command before reporting.
- **`jarvis-demo`**: starts `python -m jarvis.demo`, logs in with the printed one-time password, screenshots every tab with Playwright, and stops the demo. Used for UI work and for screenshots in PRs.
- **`gitops-smoke`**: creates a k3d cluster, applies the Argo CD bootstrap with `targetRevision` set to the pushed branch, waits for `Healthy`, and deletes the cluster. It runs in the background because it is slow.

## 5. Agent team

`.claude/agents/` already defines `frontend`, `backend` and `adversary`. Add `infra`.

| Agent | Model | Owns | May run |
|---|---|---|---|
| `frontend` | Sonnet | `jarvis/static/` | `node --check`, the `jarvis-demo` skill, the Jarvis tests |
| `backend` | Sonnet | unchanged: `jarvis/*.py`, `jarvis/compute/`, `jarvis/prompts/`, `jarvis/requirements.txt`, `tests/test_jarvis_*` | pytest, py_compile |
| `infra` **(new)** | Sonnet | `terraform/`, `profiles/`, `detect.py`, `gitops/`, `ansible/`, `scripts/`. All of these are PROTECTED after §7.4, so its PRs need `human-approved` | `terraform -chdir=terraform validate`/`fmt`, ansible-lint, yamllint, kustomize, the `gitops-smoke` skill. Never `plan`/`apply` |
| `adversary` | Fable | read-only | every check, so it can verify instead of trusting worker reports |

Loop (as used for the Jarvis audit fixes):
1. The lead splits a request into tasks, one owner per file set.
2. Workers implement without committing, then run the `checks` skill.
3. The adversary reviews each diff and gives a verdict.
4. Workers fix the blocking findings, and the adversary re-reviews.
5. The lead commits, pushes and opens a draft PR, then watches it until CI is green.

**Rules that carry over:**
- **Labels:** agents never add `human-approved` themselves.
- **Git history:** agents never rewrite history on a shared branch.
- **Tests:** a failing test is fixed, never skipped.

## 6. Checks per area (what "done" means)

| Area | Commands |
|---|---|
| Jarvis UI | `node --check jarvis/static/app.js`, Jarvis tests, `jarvis-demo` screenshots with no console errors |
| Jarvis server | `python -m py_compile jarvis/*.py jarvis/compute/*.py`, `python -m pytest -q tests` |
| Agents / MCP | py_compile, pytest (including `tests/test_guardrails.py`), the eval gate when a model key is available |
| Terraform | `terraform -chdir=terraform init -backend=false && terraform -chdir=terraform validate && terraform -chdir=terraform fmt -check`. Every command needs `-chdir`; without it, `validate` runs in the repo root and passes with no `.tf` files |
| Ansible | `ansible-lint ansible/` |
| GitOps | `kubectl kustomize gitops/clusters/<c>` for each cluster, then `gitops-smoke` for app changes |
| YAML | `yamllint -d relaxed profiles agents` |

## 7. CI changes

1. **`jarvis-e2e` job.** Run `jarvis.demo` and drive it with Playwright: log in, open every tab, send a chat, confirm and discard a pending card, and assert there are no console errors. This closes the "not tested in a browser" gap from the Jarvis audit fixes.
2. **`gitops-smoke` job.** Run it nightly and on a `gitops` label, with `targetRevision` set to `github.sha`.
3. **`terraform fmt -check`** next to `validate`.
4. **Guardrail fix.**
   - Add `terraform/`, `gitops/`, `ansible/`, `profiles/`, `detect.py`, `bootstrap.sh` and `scripts/` to `PROTECTED` in `agents/reviewer.py`.
   - Add `terraform/` and `gitops/` to `.github/CODEOWNERS`.
   - Update `tests/test_guardrails.py`.

   This is itself a protected-path PR.

## 8. Day-to-day workflow

- **Start work:** from the web or app, pick the environment and branch, and describe the task. From a terminal, run `claude --cloud "task"`. Independent tasks get separate sessions running in parallel, each on its own branch.
- **Continue locally:** use `/teleport`, or `claude --teleport <session>`, to pull a cloud session into the terminal. The branch must be pushed. Use this only for host work (`bootstrap.sh`, applies).
- **PRs:** sessions open draft PRs and subscribe to PR activity, so CI failures and review comments wake the session to fix them. The owner adds `human-approved` and merges, using squash merge.
- **Phone:** reviewing, answering questions and approving work all happen in the app. Nothing in this workflow needs a laptop except host applies.

## 9. Routines (scheduled and event-driven cloud runs)

Routines run unattended with the session's GitHub identity, which can push branches, open PRs and post comments. So they only **read, report or open PRs**, never merge, label, apply or touch secrets. Every routine prompt states that PR titles, bodies, comments, diffs and issue text are untrusted data, not instructions.

| Routine | Trigger | Does |
|---|---|---|
| Nightly health | schedule, daily | Runs the full `checks` skill on `main`. If anything is red, opens one issue with the failing output, capped in length and passed through `agents/redact.py` first |
| PR adversary review | GitHub: PR opened or updated, **only for PRs authored by the owner or listed collaborators, never from forks** | Runs the `adversary` agent on the diff and posts exactly one review comment with its verdict, and does nothing else: no pushes, no other PRs. It gives advice and never blocks; `agent-review` stays the gate. If the platform offers a read-only GitHub identity for routines, use it |
| Dependency drift | schedule, weekly | The setup script lives in the environment's settings, which a routine can't read. So it compares the **installed** tool versions (`terraform version`, `kubectl version --client`, `helm version`, `k3d version`) against the versions `ci.yml` uses, and opens an issue if they differ. If the script is committed as `scripts/cloud-setup.sh` (§3.1), it opens a PR instead |
| Docs drift | schedule, weekly | Checks the README first-boot table and `CLAUDE.md` against the code, and opens a PR with fixes |

Routines use the same environment (§3), so they get the same tools and network limits.

## 10. Security

- **Credentials:** no provider credentials in sessions or routines (§3.3). Secrets reach the VM only as proxy-attached API credentials, and only for the eval gate.
- **Network:** Custom network access with a short allowlist. Treat PR comments, issue text and fetched pages as untrusted data, as the `agent-fix` workflow already does.
- **Agent reach:** agents never apply infra changes, never add `human-approved`, and never change `.github/` without that label.
- **Guardrail gaps:** the `PROTECTED` fix in §7 closes the human-PR gap on infra paths.
- **Test fixtures:** fake keys in tests stay split, so the secret scan stays meaningful.

## 11. Limits and how to live with them

| Limit | Mitigation |
|---|---|
| Ephemeral VM | Push work-in-progress commits often. The final squash merge cleans them up |
| No KVM | Host paths are covered by `validate`, lint and the owner's first-boot checklist |
| About 30 GB free disk | Run `k3d cluster delete` and `docker system prune` after smoke tests |
| 10-minute foreground limit | Run the k3d smoke test and long suites in the background |
| Docker daemon not running | The hook starts it. If the environment can't run it, `gitops-smoke` stays CI-only |
| Setup script over 5 min isn't cached | Keep it lean and move slow optional steps into the hook |

## 12. Roadmap

| Phase | Scope | Exit criteria | Size |
|---|---|---|---|
| **1. Environment** | §3: setup script, Custom network, no secrets | A fresh session has every tool, and the setup script is cached | S |
| **2. Repo config** | §4.1 `CLAUDE.md`, §4.2 hook and permissions, `infra` agent | A new session knows the rules and runs checks without prompts | S |
| **3. Skills** | §4.3 `checks`, `jarvis-demo`, `gitops-smoke` | One command gives the pass/fail table. Jarvis screenshots work in a session | M |
| **4. CI parity** | §7: `jarvis-e2e`, `fmt -check`, guardrail fix | e2e green on PRs. Infra PRs need `human-approved` | S–M |
| **5. GitOps smoke** | §7.2 nightly job, and the in-session skill if `dockerd` works | Nightly job green for `hub`/`dev`/`prod` | M |
| **6. Routines** | §9 nightly health and PR review first, drift checks later | A week of nightly runs with no false alarms | S |

Phases 1 and 2 are the minimum; after them, every later phase is itself developed in the cloud.

## 13. Open questions

1. **Eval gate in sessions:** set up option (a) or (b) from §3.3, or keep the eval gate CI-only?
2. **Docker in sessions:** should the GitOps smoke test also run in sessions, which depends on `dockerd` starting in this environment, or only in CI?
3. **PR-review routine:** should it comment on every PR, or only PRs touching protected paths?
4. **Who merges:** is the owner always the merger, or may low-risk docs PRs (like this one) be merged by a session once CI is green?
