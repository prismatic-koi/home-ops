# Agent Information

This document contains important context for AI agents working with this repository.

## Repository Overview

This is a GitOps repository managing a Kubernetes homelab cluster using Flux CD. All cluster configuration is stored as code, and Flux automatically reconciles the cluster state with the Git repository.

### Key Technologies

- **Flux CD**: GitOps toolkit that continuously reconciles cluster state with Git
- **Kubernetes**: Running on a k3s cluster (cluster name: `cluster0`)
- **Renovate**: Automated dependency updates via GitHub PRs
- **Helm**: Package manager for Kubernetes applications
- **Kustomize**: Template-free way to customize Kubernetes resources

## Repository Structure

```
kubernetes/cluster0/
├── apps/               # Application deployments organized by namespace
├── bootstrap/         # Initial cluster bootstrap configuration
├── flux/              # Flux CD configuration
│   ├── config/        # Cluster-wide settings
│   ├── repositories/  # Helm and Git repositories
│   └── vars/          # Cluster variables and secrets
```

## Flux CD Patterns

### HelmRelease Resources

Applications are deployed using Flux `HelmRelease` resources. After merging PRs:
1. Flux detects the Git change (within ~1 minute by default)
2. Flux reconciles the HelmRelease
3. Helm upgrades the release in the cluster
4. Kubernetes rolls out the new deployment/statefulset

**Force immediate reconciliation:**
```bash
flux reconcile source git home-ops-cluster0
```

**Checking HelmRelease status:**
```bash
# View all HelmReleases
kubectl get helmreleases -A

# Check specific release
kubectl get helmrelease <name> -n <namespace> -o yaml

# Get the chart version
kubectl get helmrelease <name> -n <namespace> -o jsonpath='{.spec.chart.spec.version}'
```

### Kustomization Resources

Flux uses `Kustomization` resources (not to be confused with Kustomize's `kustomization.yaml`) to apply sets of manifests. These are defined in `ks.yaml` files throughout the repository.

## Resource Naming Conventions

⚠️ **CRITICAL**: Resource names in Kubernetes often don't match application names. Always verify actual resource names before running commands.

### Common Patterns in This Repository

| Application | Namespace | Resource Type | Actual Resource Name |
|------------|-----------|---------------|---------------------|
| prometheus-operator | monitoring | Deployment | `prometheus-kube-prometheus-operator` |
| prometheus | monitoring | StatefulSet | `prometheus-prometheus-kube-prometheus` |
| grafana | monitoring | Deployment | `grafana` |
| cert-manager | cert-manager | Deployment | `cert-manager` |
| trust-manager | cert-manager | Deployment | `trust-manager` |
| cloudnative-pg | databases | Deployment | `cloudnative-pg` |
| home-assistant | home | Deployment | `home-assistant` |
| intel-gpu-plugin | kube-system | DaemonSet | `intel-gpu-plugin-intel-gpu-plugin` |
| valkey (authelia) | auth | StatefulSet | `valkey` |
| valkey (searxng) | home | StatefulSet | `searxng-valkey` |
| valkey (blocky) | networking | StatefulSet | `blocky-valkey` |
| reloader | kube-system | Deployment | `reloader` |

### Safe Approach for Finding Resources

Always use this pattern to avoid mistakes:

```bash
# 1. Find the HelmRelease
kubectl get helmreleases -A | grep <app-name>

# 2. List resources in that namespace
kubectl -n <namespace> get all | grep <app-name>

# 3. Check specific resource type
kubectl -n <namespace> get deployments,statefulsets,daemonsets

# 4. Then check rollout with exact names
kubectl -n <namespace> rollout status deployment/<exact-name>
```

## Multiple Instances

Some applications have multiple instances deployed:
- **valkey**: 3 instances (auth, home/searxng, networking/blocky)
- **blocky**: 2 instances (primary, secondary)
- **postgres clusters**: Multiple managed by cloudnative-pg operator

Always check all instances when reviewing updates.

## Working with GitHub CLI

This repository uses `gh` for PR management:

```bash
# List PRs (Renovate's app handle in this repo)
gh pr list --state open --author "app/prismatic-bot"

# View PR details
gh pr view <number> --json title,body,files

# Merge PR (do not use --auto; branch protection isn't configured, so --auto
# would just sit waiting forever)
gh pr merge <number> --squash

# Find the Renovate Dashboard issue dynamically (number is not stable)
gh issue list --search "Renovate Dashboard" --author "app/prismatic-bot" \
  --state open --json number --jq '.[0].number'

# View / edit an issue (e.g. to tick Renovate dashboard checkboxes)
gh issue view <number>
gh issue edit <number>
```

## Renovate Configuration

- **Dependency Dashboard**: An issue titled "Renovate Dashboard" authored by
  `app/prismatic-bot` tracks all detected dependencies. The issue number is
  not stable — look it up dynamically (see the `gh issue list --search` snippet
  in "Working with GitHub CLI" above).
- **Rate-Limited PRs**: Renovate can hold PRs back when many are open at once.
  The dashboard surfaces them under a "Rate-Limited" section with a checkbox
  to release them all.
- **Auto-merge**: Enabled for most low-risk updates via
  `.github/renovate/automerge.json5`. Patches auto-merge across all packages;
  minor updates auto-merge for Docker images and Helm charts; all GitHub
  Actions updates auto-merge. Carve-outs that always require a human:
  Tier 1 infrastructure (k3s, system-upgrade-controller, rancher/k3s-upgrade,
  cilium, coredns, traefik, flux) on any update type; Tier 2 infrastructure
  (cert-manager, trust-manager, cloudnative-pg, external-dns,
  kube-prometheus-stack, longhorn, plus the Tier 1 list) on major/minor;
  databases (postgres*, valkey, redis) on major/minor; home-assistant
  (calendar versioning); seaweedfs (phantom appVersions upstream); and all
  major updates. Treat `.github/renovate/automerge.json5` as the source of
  truth if this list drifts.
- **Schedule**: Runs on the schedule configured by Renovate (Pacific/Auckland
  timezone). The routine-patching task is run by Ben occasionally, not on a
  fixed cadence.
- **Labels**: PRs are labelled with `dep/patch`, `dep/minor`, `dep/major`, and
  package type.

### Renovate Special Checkboxes

The Renovate Dashboard issue contains checkboxes that trigger Renovate actions:
- Creating all rate-limited PRs at once
- Forcing Renovate to run again

## Update Strategy

### Patch Updates (0.0.x)
- Generally safe, contain bug fixes only
- Review release notes briefly
- Can be merged in batches

### Minor Updates (0.x.0)
- New features, backward compatible
- Review release notes for new features
- Check configuration compatibility

### Major Updates (x.0.0)
- **Potential breaking changes**
- Must review upgrade guides
- Check for deprecated configuration options
- Test carefully in monitoring stack

### Security Updates
Priority updates that should be merged quickly:
- Any CVE fixes
- cert-manager / trust-manager (TLS infrastructure)
- authelia (authentication)
- Prometheus operator (monitoring)
- Cilium (networking)

## Adding a new monitored app (Prometheus scrape targets)

Prometheus runs with a full default-deny NetworkPolicy (`prometheus-default-deny` in
`monitoring`, blocking both Ingress and Egress). Because of that, **every scrape target
needs TWO NetworkPolicies, not one** — an ingress rule on the target namespace *and*
a reciprocal egress rule on the Prometheus pod. If only the ingress half exists, the
scrape silently times out: `up=0` with a healthy pod, a correct-looking ServiceMonitor,
and a correct-looking ingress rule. This bit us during the Loki/Alloy rollout and was
fixed in #3359; see #3361 for the guardrail that now catches it at PR time.

### The two rules

1. **Ingress on the target**, in the app's own `network-policy.yaml`:
   - Name: `<app>-allow-prometheus-scrape`
   - Selects the target pod, allows ingress from `app.kubernetes.io/name: prometheus`
     in namespace `monitoring`, on the metrics port.
2. **Egress on Prometheus**, in
   `kubernetes/cluster0/apps/monitoring/kube-prometheus-stack/app/network-policy.yaml`:
   - Name: `prometheus-allow-<app>-scrape-egress`
   - Selects `app.kubernetes.io/name: prometheus`, allows egress to the target
     namespace + pod selector, on the same metrics port.

Both rules must reference the **same port**. Common gotcha: the ingress rule uses the
container port name (e.g. `metrics`) resolved via the Service; the egress rule must
use the numeric port (NetworkPolicy `to.ports` does not resolve named ports across
namespaces).

### Checklist when adding a new monitored app

- [ ] App enables its ServiceMonitor (or a raw `ServiceMonitor` YAML is added).
- [ ] Target-side ingress rule `<app>-allow-prometheus-scrape` exists in the app's
      `network-policy.yaml`, allowing `monitoring/prometheus` on the metrics port.
- [ ] **Reciprocal** egress rule `prometheus-allow-<app>-scrape-egress` is added to
      `kubernetes/cluster0/apps/monitoring/kube-prometheus-stack/app/network-policy.yaml`,
      targeting the app's namespace + pod selector + numeric metrics port.
- [ ] After deploy, verify `up{job=~".*<app>.*"} == 1` in Prometheus. `up=0` with a
      healthy pod almost always means the egress half is missing.

The CI job `Prometheus scrape NetworkPolicy lint`
(`.github/workflows/prometheus-scrape-netpol-lint.yaml`, script at
`scripts/lint-prometheus-scrape-netpol.py`) enforces this at PR time. If it fails on a
new app, the fix is almost always "add the missing `prometheus-allow-<app>-scrape-egress`
rule".

## Pods accessing the Kubernetes API server

Any pod that communicates with the Kubernetes API server from a pod on this cluster
(e.g. external-dns, cert-manager, tailscale proxies reading/writing node state Secrets)
needs an **explicit CiliumNetworkPolicy egress allow**, because the cluster enforces
default-deny egress. On this k3s cluster with Cilium `kubeProxyReplacement=true`, the
standard `NetworkPolicy` `ipBlock` rules are silently bypassed -- the kube-apiserver
runs in the host network namespace and Cilium assigns it the `kube-apiserver` entity
identity, not a pod identity. The correct pattern is a bare `toEntities: [kube-apiserver]`
rule with **no `toPorts` restriction**. The reason: Cilium socket-LB translates the
ClusterIP (10.43.0.1:443) to the real apiserver backend (port 6443) **before** egress
policy evaluation, so a `:443`-only `toPorts` rule silently denies the translated traffic
and the pod times out.

**Do:**
```yaml
# kubernetes/cluster0/apps/<namespace>/<app>/app/cilium-network-policy.yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: <app>-allow-kube-apiserver
  namespace: <namespace>
spec:
  endpointSelector:
    matchLabels:
      # Pod selector for the app
  egress:
    - toEntities:
        - kube-apiserver
```

**Don't:**
- Use bare `NetworkPolicy` `ipBlock` rules (silently bypassed on kubeProxyReplacement=true).
- Add `toPorts: [443]` to the `toEntities: [kube-apiserver]` rule (breaks socket-LB translation).

Working in-tree examples: `coredns-allow-kube-apiserver` in
`kubernetes/cluster0/apps/kube-system/coredns/policy/cilium-network-policy.yaml` and
`cert-manager-allow-kube-apiserver` in
`kubernetes/cluster0/apps/cert-manager/cert-manager/app/cilium-network-policy.yaml`.
Prior occurrences: #2829 (external-dns), #2947 (cnpg), #3381 / #3382 (tailscale).

## Cilium Helm chart minor/major upgrades

Cilium CRDs (especially `ciliumnodeconfigs.cilium.io`) track deprecated apiVersions in their
`status.storedVersions` field **independently** of live objects. When upgrading Cilium to a
version that removes an apiVersion from the CRD spec, the Kubernetes API will block that
change if any version still exists in `status.storedVersions` — even if zero objects of
that version exist on the cluster.

**Real incident**: cilium Helm chart 1.19.6 -> 1.20.0 (PR #3414) caused `cilium-operator`
CrashLoopBackOff (`createCRDs` hook failed) because `ciliumnodeconfigs.cilium.io` had
`v2alpha1` in its `status.storedVersions` leftover from a prior install. The 1.20 release
removed v2alpha1 from spec.versions, triggering the conflict. The dataplane (cilium agents)
remained healthy throughout.

### Preflight checklist before merging a Cilium minor/major chart bump

**1. Check for stale apiVersions in Cilium CRDs:**
```bash
# Check the main CRD that most often has stale versions
kubectl get crd ciliumnodeconfigs.cilium.io -o jsonpath='{.status.storedVersions}'

# Also check the full CRD for spec.versions to compare
kubectl get crd ciliumnodeconfigs.cilium.io -o jsonpath='{.spec.versions[*].name}'
```

If `storedVersions` contains any versions NOT in `spec.versions`, investigate the new
Cilium chart version's CRD to see if it drops those versions.

**2. Verify zero live objects of deprecated versions** (safety gate):
```bash
# If storedVersions lists v2alpha1, for example:
kubectl get ciliumnodeconfigs.v2alpha1 2>&1 | grep -i 'No resources'
```

**3. If you find a stale version AND the new Cilium release removes it from spec.versions:**

Before merging, patch the CRD to prune the stale version from status:
```bash
kubectl patch crd ciliumnodeconfigs.cilium.io --subresource=status --type=merge \
  -p '{"status":{"storedVersions":["v2"]}}'
```

Adjust the version list to match the new Cilium chart's `spec.versions`. Wait for the
patch to complete, then proceed with the merge.

### Key insight

**Zero live objects of a deprecated version does NOT guarantee a safe upgrade.** The
`status.storedVersions` field is managed independently by Kubernetes and is not cleaned
up automatically just because no objects use that version. Always check both `spec.versions`
(what the CRD declares) and `status.storedVersions` (what Kubernetes thinks is in use).

If upgrade fails with `Unable to update CRD ... storedVersions` or similar, the operator
crashed during the createCRDs hook — check operator pod logs and rerun the patch command above.

## Init Containers

Some applications use init containers that must complete before the main container starts:
- **home-assistant**: Uses `git-sync` init container to pull config from Git
- Check pod status carefully - `Init:0/1` is normal during startup

## Best Practices

1. **Never assume resource names** - Always verify with `kubectl get`
2. **Check all namespaces** - Some apps have multiple instances
3. **Read existing configs** - Understanding current setup helps assess safety
4. **Merge sequentially** - Multiple PRs merged in parallel cause conflicts
5. **Wait for Flux reconciliation** - Allow 1-2 minutes after merge for Flux to act, or force with `flux reconcile source git home-ops-cluster0`
6. **Verify HelmRelease status** - Don't just check pods, check the HelmRelease
7. **Check init containers** - Some pods take longer to start due to init steps
8. **Review major updates carefully** - Check upstream docs for breaking changes

## Cluster Timezone

The cluster is configured for **Pacific/Auckland** timezone. Consider this when reviewing scheduled jobs and maintenance windows.
