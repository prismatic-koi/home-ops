---
description: Weekly Patching Tasks
agent: plan
---

# Weekly Server Patching

This task reviews and merges Renovate PRs for weekly server patching.

## Prerequisites

- Familiarity with the repository structure (see `/AGENTS.md`)
- Understanding of Flux CD and HelmRelease patterns
- Knowledge of Kubernetes resource types and naming conventions

## Task Overview

1. Review all open Renovate PRs
2. Merge approved PRs sequentially
3. Monitor deployment rollouts and verify health

## Step 1: List Open Renovate PRs

```bash
gh pr list --state open --author "app/prismatic-bot" --json number,title,url
```

Create a todo list to track review progress for each PR.

## Step 2: Review Each PR

For each PR, follow this review process:

### 2.1 Get PR Details
```bash
gh pr view <PR_NUMBER> --json title,body,files
```

### 2.2 Review Release Notes
- Check the PR body for release notes and changelogs
- Identify the version bump type (patch/minor/major)
- Look for security fixes (CVE mentions)
- Note any breaking changes or migration requirements

### 2.3 Read Current Configuration
- Identify which files are being changed (usually 1 HelmRelease file)
- Read the current configuration file(s):
  ```bash
  cat kubernetes/cluster0/apps/<namespace>/<app>/<path>/helm-release.yaml
  ```
- Understand the current setup to assess compatibility

### 2.4 Assessment Criteria

**Patch Updates (0.0.x):**
- Generally safe, bug fixes only
- Quick review of release notes
- Safe to merge

**Minor Updates (0.x.0):**
- New features, backward compatible
- Review for new features that might benefit you
- Check configuration compatibility
- Safe to merge

**Major Updates (x.0.0):**
- **REQUIRES CAREFUL REVIEW**
- Check upstream release notes for breaking changes
- Look for migration guides
- Check for deprecated configuration options
- May require configuration updates

**Security Updates:**
- Any CVE fixes should be prioritized
- Priority applications: cert-manager, trust-manager, authelia, prometheus-operator, cilium

### 2.5 Document Assessment
For each PR, note:
- Version change (e.g., v1.2.3 -> v1.2.4)
- Update type (patch/minor/major)
- Key changes (security fixes, new features, breaking changes)
- Safety assessment (SAFE TO MERGE / NEEDS CAUTION)

## Step 3: Merge PRs Sequentially

⚠️ **IMPORTANT**: Merge PRs one at a time, not in parallel. Parallel merges cause base branch conflicts.

```bash
gh pr merge <PR_NUMBER> --squash
```

**Do NOT use `--auto` flag** - branch protection rules are not configured.

Wait a few seconds between merges to allow GitHub to update the base branch.

## Step 4: Monitor Deployments

After merging all PRs, force Flux to reconcile immediately instead of waiting:

```bash
flux reconcile source git home-ops-cluster0
```

This triggers Flux to pull the latest changes from Git and reconcile all resources immediately. Wait 30-60 seconds for the reconciliation to complete.

### 4.1 Check HelmRelease Status
```bash
kubectl get helmreleases -A | grep -E "(app1|app2|app3)"
```

Look for:
- `True` in the READY column
- Recent revision number in the STATUS message
- Correct version in the chart name

### 4.2 Find Actual Resource Names

⚠️ **CRITICAL**: Don't assume resource names match application names. Always verify first.

```bash
# Find the namespace
kubectl get helmreleases -A | grep <app-name>

# List resources in that namespace
kubectl -n <namespace> get deployments,statefulsets,daemonsets | grep <app-name>
```

Refer to `/AGENTS.md` for common resource naming patterns.

### 4.3 Check Rollout Status

Use the exact resource type and name found above:

```bash
# For Deployments
kubectl -n <namespace> rollout status deployment/<exact-name> --timeout=60s

# For StatefulSets
kubectl -n <namespace> rollout status statefulset/<exact-name> --timeout=60s

# For DaemonSets
kubectl -n <namespace> rollout status daemonset/<exact-name> --timeout=60s
```

### 4.4 Check Pod Health

```bash
kubectl get pods -A | grep -E "(app1|app2|app3)" | grep -v Completed
```

Look for:
- `Running` status
- `X/X` in READY column (all containers ready)
- No restart loops (RESTARTS column)

**Note**: Some pods (like home-assistant) have init containers - `Init:0/1` is normal during startup. Wait for init containers to complete.

## Step 5: Report Results

Provide a summary including:

### PRs Merged
List all merged PRs with:
- PR number and title
- Version change
- Update type (patch/minor/major)

### Notable Updates
Highlight:
- Security fixes (CVEs)
- Major version updates
- New features of interest

### Deployment Status
Confirm all applications:
- HelmRelease status (READY = True)
- Pod status (Running, all containers ready)
- Any issues encountered

### Example Output Format

```
## Weekly Server Patching Complete

### Merged PRs:
1. PR #2549 - trust-manager v0.20.3 (patch, CVE fix)
2. PR #2545 - cert-manager v1.19.2 (patch, CVE fix)
3. PR #2551 - kube-prometheus-stack v80.3.0 (major, no breaking changes)
... (continue for all PRs)

### Notable Updates:
- Security fixes for cert-manager and trust-manager (CVE-2025-61727, CVE-2025-61729)
- Major update to kube-prometheus-stack with Prometheus Operator v0.87.0
- Home Assistant updated with bug fixes for multiple integrations

### Deployment Status:
All X services successfully deployed and running:
- cert-manager: Running (v1.19.2)
- trust-manager: Running (v0.20.3)
- prometheus: Running 2/2 replicas (v80.3.0)
... (continue for all apps)

No issues encountered. All patches applied successfully.
```

## Common Issues & Solutions

### Issue: "Base branch was modified" error when merging
**Solution**: Another PR was merged first. Wait a few seconds and retry the merge command.

### Issue: Can't find deployment/statefulset with expected name
**Solution**: The resource name doesn't match the app name. Use `kubectl get all -n <namespace>` to find the actual resource name. Check `/AGENTS.md` for common patterns.

### Issue: Pod stuck in `Init:0/1` state
**Solution**: This is normal for apps with init containers (like home-assistant). Wait 30-60 seconds for the init container to complete.

### Issue: HelmRelease shows old version after merge
**Solution**: Flux hasn't reconciled yet. Force reconciliation with:
```bash
flux reconcile source git home-ops-cluster0
```
Wait 30-60 seconds and check again.

## Resources

- Repository context: `/AGENTS.md`
- Renovate dashboard: Issue #1
- Flux documentation: https://fluxcd.io/docs/
