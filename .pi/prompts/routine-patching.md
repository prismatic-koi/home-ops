---
description: Routine Patching Tasks
agent: plan
---

# Routine Server Patching

This task reviews and merges the open Renovate PRs that did not auto-merge.

## Context: what's already landed automatically

Most low-risk Renovate updates auto-merge per `.github/renovate/automerge.json5`
without human involvement:

- All **patch** updates across every package.
- **Minor** updates for Docker images and Helm charts.
- All **GitHub Actions** updates (minor/patch/pin/digest).

Carve-outs that always require a human (i.e. show up here):

- **Tier 1 critical infrastructure** — never auto-merges, any update type:
  `k3s-io/k3s`, `system-upgrade-controller`, `rancher/k3s-upgrade`, `cilium`,
  `coredns`, `traefik`.
- **Tier 2 infrastructure** — major/minor only (patches still auto-merge):
  `cert-manager`, `trust-manager`, `cilium`, `cloudnative-pg`, `coredns`,
  `external-dns`, `traefik`, `kube-prometheus-stack`, `longhorn`.
- **Databases** — `postgres*`, `valkey`, `redis` major/minor.
- **home-assistant** — calendar-versioned, never auto-merges.
- **seaweedfs** — never auto-merges (upstream chart appVersion has shipped
  image refs that don't exist on Docker Hub; see `.github/renovate/automerge.json5`).
- **Major updates** for everything else.

If `.github/renovate/automerge.json5` changes, this list will drift — treat the
config as the source of truth.

## Prerequisites

- Familiarity with the repository structure (see `/AGENTS.md`)
- Understanding of Flux CD and HelmRelease patterns
- Knowledge of Kubernetes resource types and naming conventions

## Task Overview

1. Find the Renovate Dashboard and check for rate-limited PRs
2. List the residue of open Renovate PRs
3. Review each PR and produce an assessment table
4. **STOP** for human approval before merging anything
5. Merge approved PRs sequentially
6. Force Flux reconciliation and verify deployments
7. Report results

## Step 1: Locate the Renovate Dashboard and surface rate-limited PRs

The dashboard issue number changes over time. Look it up dynamically:

```bash
DASHBOARD=$(gh issue list \
  --search "Renovate Dashboard" \
  --author "app/prismatic-bot" \
  --state open \
  --json number \
  --jq '.[0].number')
echo "Dashboard issue: #${DASHBOARD}"
```

Then inspect the body for any rate-limited PRs and pending checkboxes:

```bash
gh issue view "${DASHBOARD}" --json body --jq '.body'
```

Look for sections titled "Rate-Limited" (a checkbox to "Create all rate-limited
PRs at once") and "Pending Approval". If any PRs are rate-limited or pending,
**surface them to the human** in a short bullet list — they may want to tick
the checkbox to release them before this task continues, since otherwise those
updates will never show up in `gh pr list`.

Do not tick the checkbox yourself; offer the list and let the human decide.

## Step 2: List the open Renovate PRs (post-automerge residue)

```bash
gh pr list --state open --author "app/prismatic-bot" \
  --json number,title,url,labels
```

### Zero-open-PR case

If the list is empty, the auto-merge pipeline has already absorbed everything
and there's nothing for a human to review. Report:

> No open Renovate PRs. Auto-merge has handled this round; nothing to do.

Then **exit cleanly** — do not continue to subsequent steps, do not run Flux
reconciliation, do not draft an empty assessment table. You're done.

Otherwise, create a todo list to track review progress for each PR.

## Step 3: Review each PR

For each PR, follow this review process.

### 3.1 Get PR details

```bash
gh pr view <PR_NUMBER> --json title,body,files,labels
```

Do not pipe the PR body through `head`, `tail`, or `sed -n`. Any of these can
cut off a warning that sits below the line count you chose. To read a long
body, search it instead:

```bash
gh pr view <N> --json body --jq '.body' | grep -niE '\[!WARNING\]|breaking|deprecat|migrat|CVE-'
```

### 3.2 Review release notes

Search these four sources in order, and stop at the first one that makes the
impact of the update clear. Record which tier answered.

1. `pr-body` — the release notes in the PR body.
2. `upstream-release` — the upstream release page, for example
   `gh api repos/<owner>/<repo>/releases/tags/<tag>`.
3. `changelog` — a changelog file in the upstream repo, or the commit range
   between the two tags.
4. `source-diff` — the source diff between the two tags, limited to the
   values keys and flags this repository sets.

Some PR bodies carry no release-notes section at all — this is normal for a
ghcr.io image. Do not treat an empty PR body as a safe update. Move to tier 2,
`upstream-release`, and continue down the ladder from there.

- Identify the version bump type (patch/minor/major) — labels include
  `dep/patch`, `dep/minor`, `dep/major`.
- Look for security fixes (CVE mentions).
- Note any breaking changes or migration requirements.

**Stop rule:** if the impact is still unclear after tier 4, the recommendation
is NEEDS CAUTION or DO NOT MERGE. Unclear is never SAFE.

### 3.3 Read current configuration

- Identify which files are being changed (usually one HelmRelease file).
- Read the current configuration:
  ```bash
  cat kubernetes/cluster0/apps/<namespace>/<app>/<path>/helm-release.yaml
  ```
- Understand the current setup to assess compatibility.

### 3.4 Assessment criteria

Most patch updates have already auto-merged. The residue is typically more
interesting:

**Major Updates (x.0.0):**
- **REQUIRES CAREFUL REVIEW** — this is the most common shape here.
- Check upstream release notes for breaking changes.
- Look for migration guides.
- Check for deprecated configuration options.
- May require companion configuration updates.

**Tier 1/2 Infrastructure & Databases (any update type that landed here):**
- Read release notes carefully — these are the load-bearing pieces.
- Check for CRD changes, schema migrations, or upgrade ordering requirements.
- Cross-reference with `kubernetes/cluster0/apps/<namespace>/<app>/` for any
  manifest-level changes that should land alongside the version bump.

**home-assistant (calendar versioning):**
- Skim the release notes for integration breakages relevant to your devices.
- Init container delays are normal during rollout.

**seaweedfs:**
- Verify the `chrislusf/seaweedfs:<tag>` image actually exists on Docker Hub
  before approving. Upstream has shipped phantom appVersions more than once.

**Security Updates:**
- Any CVE fixes should be prioritised.
- Priority surfaces: cert-manager, trust-manager, authelia, prometheus-operator,
  cilium.

### 3.5 Record the assessment

For each PR, capture:

- Version change (e.g. `v1.2.3 → v2.0.0`)
- Update type (patch / minor / major)
- Why it didn't auto-merge (Tier 1, Tier 2 major, database minor, etc.)
- Key changes (security fixes, new features, breaking changes)
- Safety call (SAFE TO MERGE / NEEDS CAUTION / DO NOT MERGE — with a reason)
- Source tier that answered (`pr-body` / `upstream-release` / `changelog` /
  `source-diff` / `none-found`), and the evidence line it gave

## Step 4: 🛑 Human approval gate — STOP HERE

Once every PR has been assessed, present a single findings table to the human
and **stop**. Do not run `gh pr merge` for any PR until the human responds.

Table shape:

```
| PR    | Package           | Change          | Type  | Why surfaced       | Source          | Evidence                                                              | Recommendation  | Notes                          |
|-------|-------------------|-----------------|-------|--------------------|-----------------|------------------------------------------------------------------------|-----------------|--------------------------------|
| #1234 | cert-manager      | v1.19 → v1.20   | minor | Tier 2 minor       | pr-body         | no warnings section                                                     | SAFE TO MERGE   | No breaking changes in notes   |
| #1235 | seaweedfs (chart) | v4.30 → v4.31   | minor | seaweedfs carve-out| upstream-release| `tag chrislusf/seaweedfs:v4.31 confirmed present on Docker Hub`         | NEEDS CAUTION   | Verify image tag on Docker Hub |
| #1236 | rancher/k3s-upgrade | v1.30 → v1.31 | minor | Tier 1             | changelog       | `removes support for the in-tree cloud provider`                       | DO NOT MERGE    | Plan k3s upgrade separately    |
```

`Source` holds one of five values: `pr-body`, `upstream-release`, `changelog`,
`source-diff`, `none-found`. `Evidence` holds a quoted line from the release,
or the exact text `no warnings section`. A boolean column is not acceptable
here — a boolean records a claim about the agent's behaviour, and the
`Evidence` cell records a checkable fact about the release, so the human at
the gate can verify it without repeating the work.

A rollout plan, a settle window, or a drain order is not an assessment. It
does not satisfy Step 3.2, and it does not belong in the `Evidence` cell.

After the table, ask the human something like:

> Awaiting approval. Reply with one of:
> - **"approve all"** — merge every PR above sequentially.
> - **"all except #X[, #Y]"** — merge everything except the listed PRs.
> - **"only #X[, #Y, #Z]"** — merge only the listed PRs.
> - **"skip"** / **"none"** — merge nothing; exit without further action.
> - Or freeform direction if the above shapes don't fit.

Then **wait**. Do not proceed to Step 5 until the human has replied. If the
reply is ambiguous, ask for clarification rather than guessing.

## Step 5: Merge approved PRs sequentially

⚠️ **IMPORTANT**: Merge PRs one at a time, not in parallel. Parallel merges
cause base branch conflicts.

```bash
gh pr merge <PR_NUMBER> --squash
```

**Do NOT use `--auto`** — branch protection rules are not configured, so
`--auto` will just sit waiting forever.

Wait a few seconds between merges to allow GitHub to update the base branch.

## Step 6: Monitor deployments

After merging, force Flux to reconcile immediately instead of waiting:

```bash
flux reconcile source git home-ops-cluster0
```

This triggers Flux to pull the latest changes from Git and reconcile all
resources immediately. Wait 30–60 seconds for reconciliation to complete.

### 6.1 Check HelmRelease status

```bash
kubectl get helmreleases -A | grep -E "(<app1>|<app2>|<app3>)"
```

Replace the placeholders with the actual app names from the merged PRs.

Look for:
- `True` in the READY column
- Recent revision number in the STATUS message
- Correct version in the chart name

### 6.2 Find actual resource names

⚠️ **CRITICAL**: Don't assume resource names match application names. Always
verify first.

```bash
# Find the namespace
kubectl get helmreleases -A | grep <app-name>

# List resources in that namespace
kubectl -n <namespace> get deployments,statefulsets,daemonsets | grep <app-name>
```

Refer to `/AGENTS.md` for common resource naming patterns.

### 6.3 Check rollout status

Use the exact resource type and name found above:

```bash
# For Deployments
kubectl -n <namespace> rollout status deployment/<exact-name> --timeout=60s

# For StatefulSets
kubectl -n <namespace> rollout status statefulset/<exact-name> --timeout=60s

# For DaemonSets
kubectl -n <namespace> rollout status daemonset/<exact-name> --timeout=60s
```

### 6.4 Check pod health

```bash
kubectl get pods -A | grep -E "(<app1>|<app2>|<app3>)" | grep -v Completed
```

Look for:
- `Running` status
- `X/X` in READY column (all containers ready)
- No restart loops (RESTARTS column)

**Note**: Some pods (like home-assistant) have init containers — `Init:0/1` is
normal during startup. Wait for init containers to complete.

## Step 7: Report results

Provide a summary including:

### PRs merged

List each merged PR with:
- PR number and title
- Version change
- Update type (patch / minor / major)

### Notable updates

Highlight:
- Security fixes (CVEs)
- Major version updates
- New features of interest

### Deployment status

Confirm for each app:
- HelmRelease status (READY = True)
- Pod status (Running, all containers ready)
- Any issues encountered

### Skeletal example

```
## Routine Server Patching — Complete

### Merged PRs
1. PR #<N> — <package> <old> → <new> (<patch|minor|major>[, CVE fix])
2. PR #<N> — <package> <old> → <new> (<patch|minor|major>)
...

### Notable updates
- <one-liner per security fix or breaking-change-free major>

### Deployment status
All <N> services healthy:
- <app>: Running (<version>)
- <app>: Running <X>/<X> replicas (<version>)
...

No issues encountered.
```

## Common issues & solutions

### Issue: "Base branch was modified" error when merging
**Solution**: Another PR was merged first. Wait a few seconds and retry.

### Issue: Can't find deployment/statefulset with expected name
**Solution**: The resource name doesn't match the app name. Use
`kubectl get all -n <namespace>` to find the actual resource name. Check
`/AGENTS.md` for common patterns.

### Issue: Pod stuck in `Init:0/1`
**Solution**: Normal for apps with init containers (like home-assistant).
Wait 30–60 seconds for the init container to complete.

### Issue: HelmRelease shows old version after merge
**Solution**: Flux hasn't reconciled yet. Force with:
```bash
flux reconcile source git home-ops-cluster0
```
Wait 30–60 seconds and check again.

## Resources

- Repository context: `/AGENTS.md`
- Renovate dashboard: located dynamically in Step 1
- Auto-merge config: `.github/renovate/automerge.json5`
- Flux documentation: https://fluxcd.io/docs/
