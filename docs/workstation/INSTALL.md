# Installing VM-setup on the owner workstation

Run every step yourself, in order, and stop at the first failed verification. Nothing here was executed by the agent that wrote it.
Host facts at inspection time (2026-10-05): 4 cores (i5-4690), 30 GB RAM (about 18 GB used, 12 GB available), 23 GB swap (10 GB used),
load 6, no GPU, `/dev/kvm` present, `/` on ZFS rpool (241 GB free), `/ext-pool` 2.9 TB free, libvirt/qemu/podman/terraform installed but
libvirtd inactive, ansible not installed, no `/etc/vmsetup`, no `/opt/vm-setup`.

## 0. Gate: do not start until these are true

1. PRs merged on `main`, in this order (each protected-path PR needs your `human-approved` label first):
   1. #16 guardrails (no infra dependency)
   2. #17 CI pinning
   3. #14 Jarvis compute safety (Jarvis code; must precede #18, which hardens the reaper unit that runs it)
   4. #18 infra hardening (override sizing fix in detect.py, scoped `litellm.env`, narrowed ufw rule, node-exporter bound to 127.0.0.1, state kept out of `/opt/vm-setup`)
   5. #19 gitops hardening (pins k3s and the upgrade controller; adds Kyverno on dev/prod, which only matters if you add those clusters)
   #15 and #20 are not needed for this install.
2. BLOCKER, port conflicts (details in the W1 report): the monitoring and LiteLLM tasks use host networking and need 127.0.0.1:9090, :9100 (taken by the snap node-exporter) and :4000, all already in use here (netdata's :19999 is bound only on the Tailscale IP, so it is not a conflict). The `monitoring` role has no switch to skip them. Step 4 therefore cannot succeed
   until a follow-up infra change adds `monitoring.enabled` and per-service port variables (proposed task W2), or you free those ports yourself. Do not run step 4 before that.
3. You accept the memory budget: the VMs plus the host stack take about 10.5 GB of the 12 GB currently available, so the machine will swap more.
   Free RAM first (for example scale down the kind `fleet` workloads) if you want headroom.

## 1. Pre-flight (read-only)

```bash
nproc; free -g; uptime                         # load should be below 4, swap used below 10 GB
ls -l /dev/kvm; id -nG | tr ' ' '\n' | grep -x -e libvirt -e kvm
zfs list -o name,avail rpool ext-pool          # ext-pool >= 150 GB free
ss -ltn | grep -E ':(4000|9090|9093|9100|19999|8088|5432|8085|9177)\s'   # note which are taken (see gate 2)
virsh -c qemu:///system net-list --all 2>&1 | head   # fails while libvirtd is inactive; that is fine
sudo ufw status verbose > ~/ufw-before.txt     # keep for rollback comparison
```
Verify: `/dev/kvm` accessible, enough pool space, you have `~/ufw-before.txt`.

## 2. Backups

```bash
# Verify the exact names first; the mounted system datasets on this host were:
zfs list -o name,mountpoint,canmount | grep -E '^rpool/(ROOT|var|USERDATA/root)'
D=$(date +%Y%m%d)
sudo zfs snapshot -r rpool/ROOT@pre-vmsetup-$D
sudo zfs snapshot rpool/USERDATA/root_fppk26@pre-vmsetup-$D
# Not rpool/swap (zvol), not ext-pool (its replica datasets are overwritten by the nightly zfs recv -F).
sudo cp -a /etc/ufw ~/etc-ufw-backup
sudo iptables-save > ~/iptables-before.txt; sudo ip6tables-save > ~/ip6tables-before.txt
systemctl list-unit-files --state=enabled > ~/units-before.txt
podman ps -a > ~/podman-before.txt; docker ps -a > ~/docker-before.txt
```
Verify: `zfs list -t snapshot | grep pre-vmsetup` shows both snapshots (`rpool/ROOT` and `rpool/USERDATA/root_fppk26`). Rolling these back also reverts everything else written to `/` and `/var` since the snapshot, so ZFS is the last resort (see Rollback). Once the install is verified, remove them: `sudo zfs destroy -r rpool/ROOT@pre-vmsetup-$D; sudo zfs destroy rpool/USERDATA/root_fppk26@pre-vmsetup-$D`.

## 3. Storage for VM disks on /ext-pool

The Terraform config has no pool setting: `libvirt_volume` resources land in the libvirt pool named `default`, which is `/var/lib/libvirt/images` on rpool.
To put disks on `/ext-pool`, define the `default` pool there before `terraform apply` (no code change needed). Use a dedicated dataset with a VM-friendly record size.

```bash
sudo zfs create -o recordsize=64K -o compression=lz4 -o atime=off ext-pool/vmsetup
sudo mkdir -p /ext-pool/vmsetup/images
```
Define the pool after step 4 installs libvirt (virsh needs the daemon):
```bash
sudo virsh pool-define-as default dir --target /ext-pool/vmsetup/images
sudo virsh pool-build default; sudo virsh pool-start default; sudo virsh pool-autostart default
```
Verify: `virsh -c qemu:///system pool-info default` shows state running and capacity from ext-pool. If a `default` pool already exists on rpool, `pool-destroy`/`pool-undefine` it first (it should be empty).

## 4. Host bootstrap

```bash
cd ~/Projektid/cloud-hybrid/VM-setup            # a clean checkout of main after the merges
git pull --ff-only   # refuses if upstream changed profile.override.yaml while it is skip-worktree: then `git update-index --no-skip-worktree` it, restore, pull, and redo
cp docs/workstation/profile.override.yaml profile.override.yaml
git update-index --skip-worktree profile.override.yaml   # keep it local, out of commits
python3 detect.py --detect-only --storage-path /ext-pool/vmsetup | head -60   # side-effect free
```
Verify: `"profile": "min"`, nodes `hub-s1` (2 vCPU, 3 GB, 30 GB) and `hub-a1` (2 vCPU, 6 GB, 60 GB). Plain `detect.py` without `--storage-path`
measures `/` (241 GB) and aborts below the 300 GB minimum, so always pass the path.

Known issue: `agents/deploy.py` self-deploys hourly with `git reset --hard origin/main` and keeps only `profile.generated.json`, so `/opt/vm-setup/profile.override.yaml`
is replaced by the repo stub. Today that is harmless (the weekly capacity agent runs detect without `--storage-path`, which fails on `/`), but a later successful re-detect would lose these pins. Treat it as open.

Pause the root agents BEFORE bootstrap, because the ai_stack role starts `vmsetup-agent` before the monitoring and jarvis roles run:
```bash
sudo mkdir -p /etc/vmsetup && sudo touch /etc/vmsetup/AGENTS_PAUSED
ls -l /etc/vmsetup/AGENTS_PAUSED          # verify it exists
```

ufw note: it is already enabled with INPUT DROP and sshd binds only 127.0.0.1 and the Tailscale IP, so the added `allow 22/tcp` exposes nothing today, but it would if sshd ever bound the LAN.

Before running: read `ansible/roles/host/tasks/main.yml` once more and decide the ufw and zram points in the conflict table. Then:
```bash
sudo ./bootstrap.sh --storage-path /ext-pool/vmsetup
```
(`--detect-only` must be the first argument to be honoured by bootstrap.sh; do not combine it with other flags there.)
Verify. The playbook can APPEAR to succeed even with port conflicts, because `podman_container state: started` returns OK while the container crash-loops, so check for failures explicitly:
```bash
systemctl is-active libvirtd vmsetup-agent jarvis      # all active
sudo podman ps -a --format '{{.Names}} {{.Status}}'         # litellm prometheus alertmanager node-exporter libvirt-exporter netdata pgvector: all Up; none Restarting/Exited
sudo podman ps -a --format '{{.Names}} {{.Status}}' | grep -E 'Restarting|Exited' && echo PROBLEM
ls -l /etc/vmsetup /etc/vmsetup/AGENTS_PAUSED /opt/vm-setup/profile.generated.json
```
Then:
```bash
sudo systemctl disable --now zramswap 2>/dev/null || true    # zram-tools adds a second swap on an already swapping host
```
Verify: `swapon --show` lists only your existing swap.

## 5. Terraform on the host

Define the pool (section 3) first. Then, as your normal user (member of `libvirt`):
```bash
cd ~/Projektid/cloud-hybrid/VM-setup
terraform -chdir=terraform init
terraform -chdir=terraform plan  -var "ssh_public_key=$(cat ~/.ssh/id_ed25519.pub)" -var gitops_repo_url=https://github.com/<you>/VM-setup.git
```
Verify the plan: 1 network (`k8s`, 10.10.10.0/24, `virbr-k8s`), 1 base volume, and for 2 nodes a volume, cloud-init disk and domain each; nothing else.
```bash
terraform -chdir=terraform apply -var "ssh_public_key=$(cat ~/.ssh/id_ed25519.pub)" -var gitops_repo_url=https://github.com/<you>/VM-setup.git
```
Verify: `virsh -c qemu:///system list --all` shows hub-s1 and hub-a1 running; `ls -lh /ext-pool/vmsetup/images`; `./scripts/kubeconfig.sh && KUBECONFIG=~/.kube/hub.yaml kubectl get nodes`
shows both Ready (allow several minutes); `kubectl -n argocd get applications`. The repo must be reachable by the VMs (public, or add Argo CD repo credentials).
Keep `terraform/terraform.tfstate`: it holds the k3s join token and is not copied to `/opt/vm-setup` after #18.
The hub apps (kube-prometheus-stack, kyverno, loki is off) are sized for more than 9 GB; if pods are Pending or OOMKilled, raise `hub-a1.ram_gb` in `profile.override.yaml`, re-run detect, and re-apply.

## 6. Jarvis

```bash
sudo -u jarvis env JARVIS_STATE_DIR=/var/lib/vmsetup/jarvis PYTHONPATH=/opt/vm-setup /opt/vm-setup/.venv/bin/python -m jarvis.manage set-password
# optional: ... -m jarvis.manage enroll-totp   then   confirm-totp CODE
ssh -L 8088:127.0.0.1:8088 localhost      # or just browse http://127.0.0.1:8088 locally (loopback only)
```
Verify: `curl -sI http://127.0.0.1:8088/ | head -1` returns 200 or a login redirect; log in; Overview lists the two nodes and services.

## 7. Provider and RunPod keys

Preferred: Jarvis Setup tab. Paste each key; Jarvis stages it, `vmsetup-secrets.path` triggers the root applier, which merges only allowlisted names
(OPENAI, ANTHROPIC, OPENROUTER, HF_TOKEN, GH_TOKEN, RUNPOD_API_KEY, KAGGLE_*) into `/etc/vmsetup/secrets.env` and restarts litellm, vmsetup-agent and jarvis.
Manual alternative: edit `/etc/vmsetup/secrets.env` as root (never commit it), then `sudo podman restart litellm; sudo systemctl restart vmsetup-agent jarvis`.
Verify (names only, never print values):
```bash
sudo grep -E '^(OPENAI|ANTHROPIC|RUNPOD)_API_KEY=.+' /etc/vmsetup/secrets.env | cut -d= -f1
sudo systemctl status vmsetup-secrets.service --no-pager | head -5
```
RunPod spend guards default to $1/h and 4 h (`JARVIS_RUNPOD_MAX_*` in `/etc/vmsetup/jarvis.env`); the reaper timer (`jarvis-reaper.timer`) deletes expired `jarvis-*` pods every 10 minutes.
When satisfied with the autonomy policy: `sudo rm /etc/vmsetup/AGENTS_PAUSED`.

## Rollback

Manual uninstall (steps 1-5) is the primary rollback. ZFS (step 6) is the last resort: it discards everything written to the system datasets since the snapshot, including other agents' state.

1. VMs: `cd ~/Projektid/cloud-hybrid/VM-setup && terraform -chdir=terraform destroy -var ssh_public_key=x -var gitops_repo_url=x`
   (or `virsh -c qemu:///system destroy/undefine --nvram <node>`; `virsh net-destroy k8s; virsh net-undefine k8s`).
2. Units and containers:
```bash
sudo systemctl disable --now jarvis.service vmsetup-secrets.path jarvis-reaper.timer vmsetup-agent.service vmsetup-detect.timer \
  vmsetup-upgrade.timer vmsetup-cost.timer vmsetup-librarian.timer vmsetup-models.timer vmsetup-evolve.timer vmsetup-deploy.timer vmsetup-digest.timer
sudo rm -f /etc/systemd/system/{vmsetup-*,jarvis*}.{service,timer,path}; sudo systemctl daemon-reload
sudo podman rm -f litellm prometheus alertmanager node-exporter libvirt-exporter netdata pgvector phoenix localai vllm 2>/dev/null
sudo rm -rf /opt/vm-setup /etc/vmsetup /var/lib/vmsetup /usr/local/src/ollama-install.sh   # only after deciding you do not need the keys
sudo userdel jarvis
```
   (Do not remove `/usr/local/bin/ollama`: it was already yours and the role does not install it with the min profile.)
3. Firewall: compare `sudo ufw status verbose` with `~/ufw-before.txt`; delete additions with `sudo ufw status numbered` then `sudo ufw delete N` (the `22/tcp` allow, `virbr-k8s` rules). Restore from `~/etc-ufw-backup` if needed and `sudo ufw reload`.
4. Packages (optional): `sudo apt-get remove zram-tools unattended-upgrades ansible` (check first that you did not already depend on them); libvirt: `sudo systemctl disable --now libvirtd libvirtd.socket`.
5. Storage: `sudo virsh pool-destroy default; sudo virsh pool-undefine default; sudo zfs destroy -r ext-pool/vmsetup`.
6. Last resort, ZFS, **from a rescue/live boot only (the datasets are mounted and in use otherwise)**. **NEVER roll back `rpool/USERDATA/home_fppk26` or `rpool/var/lib/docker`, and never `rpool` itself** (it is an empty container; rollback is not recursive and a loop over it would destroy /home and docker state).
   Roll back EVERY dataset listed by `zfs list -r -H -o name rpool/ROOT/ubuntu_i9isau` (/ and /var, plus the separate /var/lib, /var/lib/apt, /var/lib/dpkg, /var/log, /usr/local children; none of them is /home or docker), each with `zfs rollback <dataset>@pre-vmsetup-YYYYMMDD`. If it refuses with 'more recent snapshots exist' (the nightly repl-* snapshots), add `-r` and accept losing those newer repl-* snapshots. Rolling back only some leaves the dpkg/apt database inconsistent with `/`. Never touch `rpool/var` or anything under it (canmount=off, it holds `rpool/var/lib/docker`).
Verify: `diff <(systemctl list-unit-files --state=enabled) ~/units-before.txt`, `podman ps -a`, `ss -ltn` match the pre-flight output.
