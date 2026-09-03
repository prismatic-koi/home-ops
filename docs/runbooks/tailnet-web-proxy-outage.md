# ts-web outage: the tailnet web proxy is a single point of failure

## Purpose

`tailscale-proxy-ts-web` is a single-replica Deployment with `strategy:
Recreate` (`kubernetes/cluster0/apps/networking/headscale/app/tailscale-proxy-ts-web-deployment.yaml`).
It is a dumb L4 pipe: it TCPForwards inbound tailnet `:80` and `:443` to the
in-cluster traefik gateway, and traefik terminates TLS and routes by
hostname. It has no second replica and no hand-off path. This is a deliberate,
accepted risk, not an oversight — see "Why this is accepted, not fixed"
below. This runbook exists so an operator can diagnose and recover from an
outage without re-opening that decision.

This state lives in this repository (Deployment, Service, ConfigMap,
RBAC). The tailnet node identity (machine key, node key) persists in the
`tailscale-proxy-ts-web-state` Secret and is host state in the sense that it
must survive across pod reschedules, but the manifests that create it are
repo-managed.

## Every rollout is a full outage window

`strategy: Recreate` means Kubernetes terminates the running pod before it
starts the replacement. There is no hand-off. Any change that restarts the
pod — a manifest edit, an image bump, a node drain, a crash — produces a full
outage window between the old pod's termination and the new pod's readiness,
not a rolling replacement. Budget for this window on every planned change.

## Who is affected

Two hostnames route through ts-web today. Each has a different fallback:

| Hostname | Fallback when ts-web is down |
|---|---|
| `prometheus.ts.${SECRET_PUBLIC_DOMAIN}` | None, and never had one. Tailnet-only pilot (#3466). |
| `search.${SECRET_PUBLIC_DOMAIN}` | LAN-not-tailnet clients only, via the blocky `customDNS` pin to `${TRAEFIK_IP}`, which does not traverse ts-web. A tailnet client has no fallback. |

### Why a tailnet client has no fallback even when a public record exists

`search.${SECRET_PUBLIC_DOMAIN}` has a public DNS path in principle, but a
tailnet client does not use it. headscale's MagicDNS `extra_records`
mechanism writes a Hosts-map entry for the name into the client's resolver.
A Hosts pin **replaces** the resolution path; it does not sit behind the
public record as a fallback. The client returns the Hosts-map answer and
never queries public DNS at all. So for a tailnet client, ts-web being down
means `search` is down — there is no second path to fall back to, whatever
the public zone contains.

## Why this is accepted, not fixed

Two rounds of investigation examined every direction that could remove this
single point of failure. All were closed by mechanics, not by preference.

| Direction | Deciding fact |
|---|---|
| Two A records / two tailnet identities behind one name | headscale will store two `extra_records` entries for one name, but the client-side resolver returns only the first `IPv4` match. The second entry is silently discarded before any OS resolver or app sees the answer. Nothing to fail over to. |
| Tailscale Kubernetes Operator / `ProxyGroup` | The operator authenticates via OAuth client credentials against `api.tailscale.com`. headscale serves no tailnet REST admin API for it to talk to — only OIDC user login and a gRPC/CLI plane keyed by API key. |
| Tailscale Services / VIPService | Not implemented in headscale at the pinned version (v0.29.3). |
| HA subnet routers | headscale's control plane supports this (primary election, health probing). The datapath does not — see below. |

### HA subnet routers, in more detail

This is the direction most likely to be re-proposed, so record why it stays
closed even though the control-plane machinery exists.

This cluster already ran a subnet router and retired it deliberately (#3377
design, #3379 retirement). The retirement's root cause was `kubeProxyReplacement`
translating a forwarded ClusterIP packet at the origin socket rather than on
the wire, so a router's forwarded packets never resolved to a pod IP.

Pointing a new router at traefik's **LoadBalancer** IP instead of the Service
CIDR does clear that specific root cause — the `bpf-lb-external-clusterip`
gate is ClusterIP-specific and does not apply to a LoadBalancer IP. But this
cluster runs `loadBalancer.mode: dsr` (confirmed live: `bpf-lb-mode: dsr`).
Under DSR, the traefik backend replies **directly to the source IP**, and
only the proxy pod itself holds a route to `100.64.0.0/10`. A router
forwarding to that LoadBalancer IP from a different node has no such route.
That makes this a placement-dependent silent black-hole — not proven to
fail, not proven to work, because it has never been tested live.

The structural reason holds regardless of the DSR question, and is the one
to remember: **HA subnet routing and the origin-socket property are
mutually exclusive.** The current proxy design works because it
*terminates* the tailnet connection and opens a *fresh local socket* from
the pod's own network namespace — that origin socket is what makes Cilium
socket-LB and DSR both behave correctly. A subnet router, by definition,
*forwards* L3 packets without re-originating them. A design cannot have
route-based HA and re-origination at the same time on this CNI.

### This does not contradict the working example in the other runbook

`docs/runbooks/tailnet-control-plane-access.md` documents node0-2 advertising
`10.87.42.2/32` as an HA subnet route with auto-approval — three subnet
routers, in production, working today. That is not a contradiction. The
target there, `10.87.42.2`, is the kube-vip control-plane VIP: a plain host
IP with no Cilium DNAT and no DSR involved at all. The ts-web case is
different because its target would be a Cilium **LoadBalancer Service** IP,
which is exactly where the origin-socket and DSR mechanics above apply. The
two cases differ on the one fact that decides the outcome.

## Diagnosing an outage

### Distinguish ts-web from a backend or traefik failure

```bash
# Is the proxy pod up at all?
kubectl -n networking get deploy tailscale-proxy-ts-web
kubectl -n networking get pods -l app.kubernetes.io/name=tailscale-proxy-ts-web

# Is the pod registered and connected on the tailnet?
kubectl -n networking logs deploy/tailscale-proxy-ts-web | tail -50
headscale nodes list | grep ts-web

# Is traefik itself healthy, independent of ts-web?
kubectl -n networking get pods -l app.kubernetes.io/name=traefik
kubectl -n networking logs deploy/traefik --tail=50

# From inside the cluster, does traefik answer directly (bypassing ts-web)?
kubectl -n networking run -it --rm debug --image=curlimages/curl --restart=Never \
  -- curl -sk -o /dev/null -w '%{http_code}\n' https://traefik.networking.svc.cluster.local
```

If traefik answers from inside the cluster but the tailnet hostname does
not resolve or does not connect, the fault is in ts-web (the pod, its
tailnet registration, or its `TS_SERVE_CONFIG`). If traefik itself does not
answer, the fault is downstream of ts-web and this runbook does not apply —
follow the normal traefik/backend triage instead.

### What the failure looks like, by client class

- **Tailnet client** (on the tailnet, using MagicDNS): the hostname resolves
  (Hosts-map entry does not depend on ts-web being up), but the TCP
  connection to it times out or is refused. This is the ts-web-down
  signature for a tailnet client.
- **LAN, not-tailnet client**, for `search` only: unaffected, because the
  blocky `customDNS` pin resolves straight to `${TRAEFIK_IP}` and never
  touches ts-web. If a report of `search` being down comes from this client
  class, ts-web is not the cause — look at traefik or the backend instead.
- **LAN, not-tailnet client**, for `prometheus.ts`: no fallback exists.
  Report reads the same as the tailnet case — the hostname is
  `.ts.${SECRET_PUBLIC_DOMAIN}` and is only ever served via ts-web.

## Recovery

There is no failover target. Recovery is: get the single pod healthy again.

1. Identify why the pod is not ready — check `kubectl -n networking describe
   pod` for the current failure (crashloop, unschedulable, image pull, etc.)
   and resolve that underlying cause directly.
2. If the pod is simply stuck, a manual restart triggers a fresh `Recreate`
   cycle:
   ```bash
   kubectl -n networking rollout restart deployment/tailscale-proxy-ts-web
   ```
   This still produces the full outage window described above — do not treat
   it as a low-cost action.
3. If the tailnet node registration itself looks wrong (wrong tag, stuck
   node key), read the "Repair a host that is registered under the wrong
   tag" section of `docs/runbooks/tailnet-control-plane-access.md` — the
   same headscale mechanics apply to this proxy's tailnet identity as to a
   host.
4. Confirm recovery once the pod is `Running`/`Ready`:
   ```bash
   headscale nodes list | grep ts-web
   kubectl -n networking logs deploy/tailscale-proxy-ts-web --tail=20
   ```

### The `search` workaround, and its limit

A LAN client that is not on the tailnet keeps working throughout a ts-web
outage, because blocky's `customDNS` pin resolves `search.${SECRET_PUBLIC_DOMAIN}`
straight to `${TRAEFIK_IP}`, bypassing ts-web entirely. This covers only that
one client class and only that one hostname. It does not help a tailnet
client, and it does not help `prometheus.ts` under any client class — that
hostname has no path other than ts-web.

## When to revisit this decision

Revisit accept-and-document, rather than working around it in the moment,
if either of these becomes true:

- **A third service moves behind ts-web.** The current blast radius (one
  pilot service with no fallback, one service with a partial fallback) was
  the basis for accepting the risk. A third consumer changes that
  calculation and should trigger a fresh design pass, not a repeat of this
  runbook's justification.
- **A live DSR datapath test gets funded.** The HA-subnet-router direction
  above is closed on an untested datapath question (whether LB DNAT, SNAT,
  and DSR source-encoding compose correctly for a forwarded `100.64.0.0/10`
  source). If someone runs that test — a two-pod HA router forwarding to the
  traefik LoadBalancer IP, with traefik replicas pinned across different
  nodes to force the DSR return-path case — the result should update this
  runbook, one way or the other. Frame any such proposal explicitly as
  reintroducing the #3379-retired subnet router pointed at an LB IP rather
  than the Service CIDR, so a future reader does not conclude we forgot our
  own history.
