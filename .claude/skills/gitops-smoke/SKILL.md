---
name: gitops-smoke
description: Create a k3d cluster, apply the Argo CD bootstrap with targetRevision set to the pushed branch, wait for the apps to be Healthy, then delete the cluster. Slow; run in the background. Needs dockerd.
---
Prerequisites: `docker info` works (the SessionStart hook starts `dockerd`; see /tmp/dockerd.log), `k3d` and
`kubectl` installed, and the branch pushed (Argo CD pulls from GitHub, not from your working tree).

Run in the background (it can take 10+ minutes; foreground commands time out at 10):

    bash .claude/skills/gitops-smoke/smoke.sh [cluster=dev] [revision=<current branch>] > /tmp/gitops-smoke.log 2>&1 &

Then poll `/tmp/gitops-smoke.log`. The script mirrors `terraform/cloud-init.yaml.tftpl`: it applies the Argo CD
HelmChart and the `root` Application (path `gitops/clusters/<cluster>`, `targetRevision` = the branch) to a k3d
cluster named `gitops-smoke`, waits until every Application is Synced and Healthy (timeout `SMOKE_TIMEOUT`,
default 900 s), and deletes the cluster on exit (trap), including on failure. It prints the Application
statuses on failure. Exit 0 = healthy. Afterwards `docker system prune -f` frees disk.

Needs network access to the Helm chart repos and registries (see the cloud network allowlist). If `dockerd`
cannot run in this environment, leave this check to CI. Repo URL defaults to `origin` rewritten to https;
override with `SMOKE_REPO_URL`.

The cluster name `gitops-smoke` is fixed (the permission rules allow only that name), so two concurrent runs
on one machine conflict: the second deletes the first's cluster. Run one at a time.
