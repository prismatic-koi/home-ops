# Agent Information

This document contains important context for AI agents working with this repository.

## Repository Overview

This is a GitOps repository managing a Kubernetes homelab cluster using Flux CD. All cluster configuration is stored as code, and Flux automatically reconciles the cluster state with the Git repository.

Note: This repo is public, don't commit anything that does not belong in a public repository

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
- **blocky**: 1 HelmRelease running 2 replicas (consolidated from separate primary/secondary in #3563)
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

## Public DNS is opt-in (external-dns)

external-dns publishes a public record **only** for an object that carries this
label:

```yaml
dns.home-ops/public: "true"
```

The chart value that enforces it lives in
`kubernetes/cluster0/apps/networking/external-dns/app/helm-release.yaml`:

```yaml
labelFilter: dns.home-ops/public=true
```

The chart renders that value as the `--label-filter` argument on the Deployment
(#3525).

`labelFilter` is a named chart key, not a passthrough like the old `extraArgs`
form. Chart 1.21.1 sets `additionalProperties: true`, so a dropped or renamed
key renders **silently without the flag** — the fail-open regression #3597
guards against.

The CI job `external-dns lint`
(`.github/workflows/external-dns-lint.yaml`, script at
`scripts/lint-external-dns.py`) runs two checks over one `flux-local build`
render. It matches the #3361 and #3519 lints, and each check fails loudly if
the object it needs is absent from the render, so a broken or empty render
never passes vacuously.

1. **label-filter (#3597).** Asserts the rendered external-dns Deployment
   carries exactly one `--label-filter=dns.home-ops/public=true`. If it fails
   after a chart bump, the fix is almost always to restore the `labelFilter`
   value above.
2. **source-to-RBAC (#3601).** For every `--source=<name>` argument on the
   rendered Deployment, asserts the rendered ClusterRole grants that source's
   RBAC exactly once — for example `--source=gateway-httproute` requires
   `httproutes` in `gateway.networking.k8s.io`. The chart generates RBAC from
   its `sources:` value, not from `extraArgs`, and accepts both forms without
   an error (#3600). A source left in `extraArgs` renders a byte-identical
   Deployment argument set with no RBAC, so external-dns cannot list the
   objects it names and the Deployment crash-loops. This is fail-closed, not
   the #3522 fail-open shape: it breaks the deploy, it does not withdraw
   records. The fix is to declare the source in `sources:`, never in
   `extraArgs`. A resource granted more than once (for example a stale manual
   `rbac.additionalPermissions` block overlapping a chart-generated rule) also
   fails. The source-to-resource mapping comes from chart 1.21.1
   `templates/clusterrole.yaml` and the `external-dns.hasGatewaySources`
   helper.

The two checks were consolidated into one script and one workflow (#3601)
because both read the same rendered external-dns objects, and a second
full-tree `flux-local` render on every PR costs real minutes.

#### Rendering `flux-local` output locally

To reproduce the render these lints read (three sessions have rediscovered
this):

- podman on this host has no OCI runtime, so the CI
  `ghcr.io/allenporter/flux-local` image will not run locally.
- Install `flux-local` v8.4.0 from pip instead — the version
  `.github/workflows/flux-local.yaml` pins.
- Render from a non-worktree copy of the tree. GitPython cannot resolve a
  worktree `.git` file, so `flux-local` fails inside a `git worktree`
  checkout. Copy the tree out (for example `git archive HEAD | tar -x -C
  <dir>`, then `git init` in that dir) and render there:
  `flux-local build all --enable-helm --skip-secrets --skip-crds
  --output-file rendered.yaml kubernetes/cluster0/flux`.

An object without the label is invisible to external-dns. It gets **no** public
A/CNAME record and **no** `k8s.` TXT ownership record.

Every HTTPRoute that declares a hostname must carry the label with an explicit
value — `"true"` to publish, `"false"` to keep the hostname private (#3519).
`"false"` changes nothing at runtime: the filter matches `"true"` only, so a
`"false"` route stays invisible to external-dns. A missing label is a defect,
not a private default — CI fails it (see "CI enforces the decision" below). A
route that declares no hostname needs no label.

Before #3518 the default was the reverse: every HTTPRoute was published unless
it carried `external-dns.alpha.kubernetes.io/controller: none`. A route added
without that annotation got a public record silently. That is the defect the
label fixes.

### Withdrawing the record reduces discoverability, not reachability

Removing the label, and so withdrawing the public record, does not make a
service unreachable from the internet. traefik routes by the HTTP `Host`
header. Every hostname bound to a listener is served by that listener,
whether or not a public record exists for it. A client sets its own name
resolution, with `curl --resolve` or an `/etc/hosts` entry. No privilege is
needed, and the change has no effect on our DNS. Hostnames are not secret:
they appear in public certificate transparency logs.

The control is the listener a route binds to, not the DNS record. See
"Three exposure tiers: the listener routes, the Service exposes" below for
the mechanism that removes reachability.

### Same commit is not same time — use expand-then-contract

A change that makes a controller **more selective** — a label filter, an
annotation filter, a selector narrowing, a scope reduction — must land as two
commits, not one:

1. **Expand:** add the labels or annotations. No behaviour change. Wait for
   Flux to reconcile, then verify every target object carries the marker on
   the live cluster.
2. **Contract:** enable the filter, in a later, separately-reconciled commit.

Putting the marker and the filter in the same commit does not make them take
effect at the same time. The controller's HelmRelease and the app
Kustomizations reconcile independently, so the filter can go live before Flux
has finished re-rendering every target object with its marker. In that
window the controller acts on a partial view.

This bit us in PR #3522 (the #3518 external-dns rollout): the label and the
`--label-filter` landed in one commit, and the label took roughly 75-100
seconds to render onto all HTTPRoutes. external-dns restarted with the
filter active inside that gap, saw four routes still unlabelled, and
withdrew 8 records (4 CNAMEs plus their `k8s.` TXT ownership records),
causing a ~5 minute public DNS outage. It self-healed at the next 5-minute
`--interval` reconcile, because `policy: sync` re-creates as readily as it
deletes — a permanent render failure would have caused a permanent outage.

Between the two commits, confirm every target object carries the marker on
the live cluster before enabling the filter — do not rely on the commit
having merged.

### The label means "published", not "should be public"

#3518 changed the **mechanism** and kept the published set identical. Every
hostname that external-dns published at the time carries the label. So the
label on a given route means "external-dns publishes this hostname today". It
does not mean someone decided the hostname belongs on the public internet.

Only five hostnames are *intended* to stay public:

| Hostname | Why |
|---|---|
| `hs` | headscale control plane. **Permanent** — see below. |
| `plex` | family and guest native apps |
| `jellyfin` | family and guest native apps |
| `home-assistant` | mobile app, webhooks, voice |
| `requests` | guest-facing request UI (overseerr) |

Every other labelled route is a **tailnet-migration candidate**. Its comment
says so.

`search` was the first candidate to complete that migration. #3553 added the
internal resolution paths and #3555 withdrew the public record, so the route
now carries `dns.home-ops/public: "false"` and has left the labelled set. See
"Count of unlabelled routes: zero" below for what resolves the hostname now.

`hs.${SECRET_PUBLIC_DOMAIN}` can never be made tailnet-only. A tailscale client
must reach headscale to register and to re-key, so a tailnet-gated control plane
is a circular dependency.

### Removing the label withdraws public DNS — do not do it alone

`policy: sync` is unchanged, so removing the label makes external-dns delete the
record on the next reconcile. **That is not the same as making a service
internal-only. For most hostnames here it makes the service unreachable from
everywhere.**

The reason is the client resolver path, not the cluster:

- headscale pushes `1.1.1.1` and `8.8.8.8` as the global resolvers, so a
  tailnet client resolves app hostnames through **public DNS even on the LAN**.
- blocky pins only four names in `customDNS`
  (`kubernetes/cluster0/apps/networking/blocky/app/config.yaml`): `unifi`,
  `traefik`, `longhorn`, `auth`. Everything else falls through to the public
  upstreams.
- coredns does not serve the public domain, and there is no wildcard, no
  `conditional` upstream block, and no k8s-gateway.

So for any hostname outside those four pins, the public record **is** the only
resolution path today, LAN included.

**Never remove the label as a bulk operation.** Withdraw a hostname one service
at a time, as part of that service's tailnet migration, and land internal
resolution for it in the same change. `prometheus.ts` is the pilot for that
pattern (#3466): a `*.ts.${SECRET_PUBLIC_DOMAIN}` hostname, a tailscale proxy, a
tailnet ACL, and no public record.

### `--label-filter` is global, not per-source

The flag applies to **every** enabled source, not only `gateway-httproute`. The
effective source set is `ingress`, `crd` and `gateway-httproute`. So:

- A `DNSEndpoint` needs the label as well. The repository holds none today, so
  the `crd` source publishes no record. Add the label to the first
  `DNSEndpoint` someone writes, or external-dns never sees it.
- Any future `Ingress` object needs the label to publish a record.

Since #3598 every source sits in one place: the chart value `sources:` in
`kubernetes/cluster0/apps/networking/external-dns/app/helm-release.yaml`. No
`--source=` argument remains in `extraArgs`. Declare a new source in that list.
The chart generates the gateway RBAC from `sources:`, so a source declared
through `extraArgs` has no permission to read the objects it names.

Enumerate every active source before you touch this flag. Do not check
`gateway-httproute` alone. One review did, and it nearly deleted a live record
that came from the `crd` source: `tun.${SECRET_PUBLIC_DOMAIN}`, published by the
cloudflared app (#3518). That app and its record are gone (#3581). The `crd`
and `ingress` sources stay enabled, so the same mistake is still available.

### external-dns deletes only a record it owns

Ownership is a TXT record, not the `dns.home-ops/public` label. Removing the
label does not withdraw a record that external-dns does not own —
`plan.calculateChanges` filters deletes through `FilterEndpointsByOwnerID`, so
an unowned record stays in the zone whatever the label says.

The ownership TXT record uses one of two naming schemes:

- legacy: `k8s.<host>`
- current: `k8s.<type>-<host>`, for example `k8s.cname-seaweedfs`

Check both schemes. A check of only one scheme gives a false negative: it
reports a record as unowned when the ownership TXT uses the other scheme.

Before you rely on a label change to withdraw a record, check ownership for
that hostname in Cloudflare. Most records in this zone hold no ownership TXT
under either scheme, so most label flips are inert, and a manual delete in
Cloudflare is the only way to withdraw the record. external-dns logs an
unowned record each loop as `missing owner label` or `owner id does not
match`. Check the logs before you assume a record is managed here.

### Worked example — publish a new hostname

Most hostnames come from a bjw-s `app-template` HelmRelease. Add `labels` to the
route, beside `hostnames`:

```yaml
# kubernetes/cluster0/apps/<namespace>/<app>/app/helm-release.yaml
  values:
    route:
      app:
        labels:
          # Public DNS opt-in (#3518). State why the hostname must be public.
          dns.home-ops/public: "true"
        hostnames: ["{{ .Release.Name }}.${SECRET_PUBLIC_DOMAIN}"]
        parentRefs:
          - name: traefik-gateway
            namespace: networking
            sectionName: websecure
```

For a raw HTTPRoute manifest, put the label in `metadata.labels`:

```yaml
# kubernetes/cluster0/apps/<namespace>/<app>/app/httproute.yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: <app>
  namespace: <namespace>
  labels:
    # Public DNS opt-in (#3518). State why the hostname must be public.
    dns.home-ops/public: "true"
```

The `grafana` chart uses the same `route.<name>.labels` key as `app-template`.

To keep a new hostname private, set the label to `"false"` and state why in a
comment. `"false"` is a recorded decision; an absent label is a forgotten one,
and CI rejects it. Do not copy the old
`external-dns.alpha.kubernetes.io/controller: none` annotation onto new objects.
One route still carries it — `monitoring/kube-prometheus-stack/app/httproute.yaml`
— and keeps it as deliberate defence in depth.

For a private raw HTTPRoute the label goes in `metadata.labels`:

```yaml
metadata:
  labels:
    # Private DNS decision (#3519). State why the hostname stays private.
    dns.home-ops/public: "false"
```

### CI enforces the decision

`.github/workflows/httproute-dns-decision-lint.yaml`
(`scripts/lint-httproute-dns-decision.py`) fails a pull request when a rendered
HTTPRoute declares a hostname and carries no valid `dns.home-ops/public` label.
It lints `flux-local build` output, so it also catches a route a chart emits
with no repository-side `route:` block. The value must be the string `"true"`
or `"false"`; an unquoted boolean or any other value fails. The failure message
names the namespace, the route, the source file, and the exact label line to
add. The usual fix is to add that one line.

### Count of unlabelled routes: zero

Since #3519 no HTTPRoute is unlabelled. Three routes carry
`dns.home-ops/public: "false"`, a recorded private decision. Two of them are the
routes that once had no label; the third is a hostname withdrawn from public DNS
after its tailnet migration. No two are the same case:

| Route | Declares a hostname? | Value | Why it is private |
|---|---|---|---|
| `monitoring/prometheus-ts-web` | Yes, `prometheus.ts.…` | `"false"` | Tailnet-only pilot (#3466). The `"false"` keeps it off public DNS. **Do not change it to `"true"`.** It also keeps `external-dns.alpha.kubernetes.io/controller: none` as defence in depth. |
| `networking/httpsredirect` | No | `"false"` | It declares no hostnames, so it can never produce a record. The lint exempts a no-hostname route regardless; the label states the decision anyway. |
| `home/searxng` | Yes, `search.…` | `"false"` | Withdrawn from public DNS in #3555, the contract half of an expand-then-contract migration. The headscale `nameservers.split` entry plus the `extra_records` pin (#3553) are the only resolution path left: #3631 removed the blocky `customDNS` pin, and #3648 moved the route to the tier-3 `websecurets` listener, so the hostname has no LAN path at all. **That headscale pin is load-bearing — remove it and no client resolves the hostname.** Change the value to `"true"` only to roll the withdrawal back. |

Do not remove any of the three labels.

### Verify

```bash
# Every labelled route. Expect the full published set, not a subset.
kubectl get httproute -A -l dns.home-ops/public=true

# Labelled DNSEndpoint objects. Expect no results: the repository holds none
# since #3581. A result means someone added one — check that it is deliberate.
kubectl get dnsendpoint -A -l dns.home-ops/public=true

# Every route carries the label since #3519 (value true or false). Expect NO
# route with an empty PUBLIC column — CI fails a missing label.
kubectl get httproute -A -L dns.home-ops/public

# What external-dns actually generates.
kubectl -n networking logs deploy/external-dns --tail=500 \
  | grep "Endpoints generated from"
```


## A blocky customDNS pin is break-glass infrastructure only

A `customDNS` pin in
`kubernetes/cluster0/apps/networking/blocky/app/config.yaml` exists to
bootstrap **critical infrastructure** when the cluster is broken. It is not a
general pattern for the internal resolution of an ordinary application.

Only four hostnames get a pin, because each names a tool you need to repair
the cluster:

- `unifi` — network
- `traefik` — ingress
- `longhorn` — storage
- `auth` — authelia; without it nothing else admits a login

An ordinary application does not get a pin. Reach it through the tailnet, and
let the headscale ACL act as its access control. A pin on an application
widens access to the whole LAN, a wider set than the tailnet ACL admits.

#3609 added pins for four ordinary applications by copying an earlier pin,
without asking why that pin existed. #3629 removed all five. Before you add a
pin, confirm the hostname names one of the four infrastructure tools above.

## Three exposure tiers: the listener routes, the Service exposes

A route becomes unreachable through listener binding, not through DNS
withdrawal. See "Withdrawing the record reduces discoverability, not
reachability" above.

The **listener** decides routing. The **Service** decides exposure. One
entrypoint can have more than one Service in front of it, and the Service type
controls who can reach it. #3635 holds the full design.

| Tier | Reachable from | Service | Listener |
|---|---|---|---|
| 1 Public | internet, LAN, tailnet | `traefik`, LoadBalancer. The gateway forwards 443 to it. | `websecure` |
| 2 LAN fallback | LAN, tailnet. Not the internet. | `traefik-lan`, LoadBalancer. The gateway forwards no port to it. | `websecurelan` **and** `websecurets` |
| 3 Tailnet only | tailnet only | `traefik-ts`, **ClusterIP** | `websecurets` |

All three listeners carry the same wildcard certificates, so a route serves the
same certificates on any of them. The public `websecure` listener holds no
route for a tier-2 or tier-3 hostname, so a `Host`-header request to the public
address returns 404, whatever the client resolves.

### How to select a tier

- **Tier 1** — one `parentRefs` entry, `websecure`.
- **Tier 2** — two `parentRefs` entries, `websecurelan` and `websecurets`.
  Break-glass needs both paths: the LAN one when tailscale is down, the tailnet
  one when the operator is off the LAN.
- **Tier 3** — one `parentRefs` entry, `websecurets`, and no other listener.

**A tier-2 route is not private from the LAN.** `traefik-lan` is a
LoadBalancer, and the Cilium L2 announcement policy matches every node, so its
address answers ARP across the whole LAN. Any LAN host reaches a tier-2 route
with `curl --resolve`. A tier-2 route therefore keeps its `forwardauth-authelia`
filter permanently: the filter is its identity control, not a migration step.

**Tier 3 is private because no LAN address exists for its listener**, not
because a rule denies access. `traefik-ts` is a ClusterIP Service, so the ts-web
tailscale proxy is the only path in. There is no allow-list to misconfigure.
`traefik-ts` must never become a LoadBalancer and must never take an address
from the LB-IPAM pool. `expose.default` must stay `false` on both the
`websecurelan` and `websecurets` entrypoints, or the chart publishes them on
the public address.

Tier 3 is the default. Tier 2 is only for a tool you need to **repair** a broken
cluster — ingress, storage, network, login, connectivity diagnosis. A tool that
is merely useful during an outage fails that test and belongs in tier 3.

### Membership today

Six routes are tier 3: `changedetection-io`, `zigbee2mqtt`, `uptime`,
`octoprint`, `search` and `prometheus.ts` (#3648). **No route binds
`websecurelan` today.** The tier-2 set — `traefik`, `longhorn`, `unifi`,
`auth`, `hubble-ui` — is still on tier 1 and moves in waves 2 and 3 of #3607.

Step 6 of #3635 renamed `traefik-private` to `traefik-lan` and `websecurepriv`
to `websecurelan`, so the object name states the tier (#3654). The rename has
landed, so `traefik-lan` and `websecurelan` are the current names.

### `sectionName` fails silently

A route that names a listener which does not exist is not attached to the
Gateway. It reports `Accepted=False reason=NoMatchingParent`, and nothing else
surfaces the failure. `networking/httpsredirect` stayed in this state for a
long time, masked by a redirect at the entrypoint.

After you change a route's `sectionName`, confirm that the service is still
reachable on its intended path. Do not confirm only that it stopped being
reachable on the old path — that check alone can pass while the route is
attached to no listener at all.

### An egress NetworkPolicy for a tier-2 or tier-3 route must name the container port

The kube-apiserver note above ("Pods accessing the Kubernetes API server")
covers socket-LB address translation. The same mechanism translates the port.
Cilium socket-LB rewrites a ClusterIP and its Service port to the backing
pod's IP and container port, before egress policy evaluation. An egress
`NetworkPolicy` `toPorts` rule must name the container port. A rule that
names the Service port denies the translated traffic when the Service's
`targetPort` differs from its `port`. This broke the tailnet path in #3623
and was fixed in #3625.

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

## Changing an allocatable IP range or address pool

Any change to an allocatable IP range (a `CiliumLoadBalancerIPPool` block, a DHCP
range, a metallb pool, or similar) must enumerate the reserved infrastructure
addresses that fall inside the proposed range and confirm each one is excluded.
At minimum, check the range against:

- the LAN router (`10.87.42.1`)
- the kube-vip control-plane VIP (`10.87.42.2`)
- the cluster nodes' InternalIPs (`10.87.42.100`-`.103`)
- NAS0_IP, the physical Synology (`10.87.42.200`)

A narrowing fix can silently become a widening one at the other end of the range —
see PR #3521, where fixing the lower bound of the Cilium LB-IPAM pool first
introduced a new upper-bound overreach into the node IPs and NAS0_IP, caught only
by a human second pass after two review rounds passed it. State the reserved
addresses you checked, and the result, in the PR description.

## Renaming a LoadBalancer Service orphans its Cilium L2 announcement

Renaming a LoadBalancer Service deletes the old object and creates a new
one. Cilium drops the old `cilium-l2announce-*` lease for that Service and
does not create one for the new Service.

The result is silent, and every ordinary check looks healthy:

- The Service exists, with `type: LoadBalancer`.
- LB-IPAM has assigned the address, and `status.loadBalancer.ingress` shows
  it.
- EndpointSlices are populated and the backend pods are ready.
- The NetworkPolicy permits the port.
- `kubectl get svc` looks entirely normal.

The address answers nothing, because it is never ARP-announced, so no LAN
client can reach it.

**The direct signal is the absence of a lease**, not a failed connection
check:

```bash
kubectl -n kube-system get lease | grep l2announce
```

After you rename a LoadBalancer Service, run this check. Do not rely on a
connection test alone to confirm the rename worked.

**Do not diagnose this with ping.** The traefik ingress NetworkPolicies
permit TCP only, so ICMP fails for a working address as well as a broken
one. In one investigation, `ping` reported 100% loss for a healthy address.
Use a TCP probe instead, and compare against a known-good address:
`curl` returning `000` means unreachable, and `404` means reachable with no
route matched.

**Recovery:** delete the Service and let Flux recreate it. The create event
makes Cilium claim a new lease. In one case, the lease appeared within 30
seconds and the address answered about a minute later, once the router's
ARP entry refreshed. Rolling the cilium agent DaemonSet also works, but it
is far heavier — that action affects the CNI for the whole cluster.

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
