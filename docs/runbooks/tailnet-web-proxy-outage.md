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

### Measured window: roughly 15 to 20 seconds

A `kubectl rollout restart` of `tailscale-proxy-ts-web` was measured on the
live cluster. Pod-side timings, counted from new pod creation:

| Event | Δ from new pod creation |
|---|---|
| Container Ready | +9s |
| `serve proxy: applying serve config` — proxy serving | +14s |

Add the old pod's termination, which `Recreate` completes before the new pod
is created, and the client-visible window is roughly **15 to 20 seconds**.
Treat this as the recovery-time budget for any planned change that restarts
the pod.

This figure is one sample, reconstructed from pod events and the
containerboot log rather than measured from a client. Treat it as an
estimate, not a guarantee.

The short window depends on the node key persisting in the
`tailscale-proxy-ts-web-state` Secret. Because the key persists, the proxy
re-registers on restart rather than re-authenticating. If that Secret is
lost or rotated, expect a longer window while the proxy re-authenticates.

## Who is affected

Eighteen hostnames route through ts-web today. **Seventeen of them have no
fallback over the network.** Three of those seventeen have an off-network
repair path — see "The port-forward workaround" below.

`auth` is the eighteenth and it is the only one with a fallback. Its blocky
pin serves one client class: a client that is not on the tailnet. A tailnet
client reaches `auth` through ts-web alone, because an `extra_records` pin
replaces the resolution path. See "authelia login depends on ts-web for a
tailscale-up client" below.

| Hostname | Tier 3 since |
|---|---|
| `prometheus.ts.${SECRET_PUBLIC_DOMAIN}` | #3466. Tailnet-only pilot; never had a fallback. |
| `search.${SECRET_PUBLIC_DOMAIN}` | #3648 |
| `changedetection-io.${SECRET_PUBLIC_DOMAIN}` | #3648 |
| `uptime.${SECRET_PUBLIC_DOMAIN}` | #3648 |
| `zigbee2mqtt.${SECRET_PUBLIC_DOMAIN}` | #3648 |
| `octoprint.${SECRET_PUBLIC_DOMAIN}` | #3648 |
| `seaweedfs.${SECRET_PUBLIC_DOMAIN}` | #3667 |
| `grafana.${SECRET_PUBLIC_DOMAIN}` | #3667 |
| `traefik.${SECRET_PUBLIC_DOMAIN}` | #3719 |
| `longhorn.${SECRET_PUBLIC_DOMAIN}` | #3719 |
| `hubble.${SECRET_PUBLIC_DOMAIN}` | #3719 |
| `auth.${SECRET_PUBLIC_DOMAIN}` | #3720. **Not tier 3.** Dual-bound to `websecure` and `websecurets`. Its LAN fallback survives for a non-tailnet client only. |
| `lidarr.${SECRET_PUBLIC_DOMAIN}` | #3720 |
| `prowlarr.${SECRET_PUBLIC_DOMAIN}` | #3720 |
| `qbittorrent.${SECRET_PUBLIC_DOMAIN}` | #3720 |
| `radarr.${SECRET_PUBLIC_DOMAIN}` | #3720 |
| `sabnzbd.${SECRET_PUBLIC_DOMAIN}` | #3720 |
| `sonarr.${SECRET_PUBLIC_DOMAIN}` | #3720 |

Seventeen of the eighteen are bound to no listener that has a LAN address.
`traefik-ts` is a ClusterIP Service, so a ts-web outage takes those seventeen
down over the network, for every client class. Tier 2 holds no route, and the
`websecurelan` listener serves nothing until #3723 deletes it.

`auth` is bound to `websecure` as well, so the public traefik address still
serves it. A tailnet client does not use that address, for the reason in the
next subsection.

### Why a tailnet client has no fallback even when a public record exists

A tailnet client does not fall back to public DNS, whatever the public zone
contains. headscale's MagicDNS `extra_records` mechanism writes a Hosts-map
entry for the name into the client's resolver. A Hosts pin **replaces** the
resolution path; it does not sit behind the public record as a fallback. The
client returns the Hosts-map answer and never queries public DNS at all. So
for a tailnet client, ts-web being down means the hostname is down — there is
no second path to fall back to. All eighteen hostnames carry an
`extra_records` pin.

Thirteen of them have no public record. Five do: `auth`, and four of the six
*arr hostnames. external-dns holds no ownership TXT for `lidarr`,
`qbittorrent`, `radarr` or `sonarr` under either naming scheme, so the
`dns.home-ops/public: "false"` flip in #3720 leaves those records in
Cloudflare, and only a manual delete withdraws them. external-dns owns the
`prowlarr` and `sabnzbd` records and withdraws both.

None of the five is a fallback. A tailnet client never queries public DNS for
a pinned name. For a client that is not on the tailnet, the four *arr records
are Cloudflare-proxied: the client reaches the Cloudflare edge, the edge
forwards to the origin, and traefik holds no route for those hostnames on the
public listener. The client gets a 404.

A `dig` mid-outage returns a Cloudflare address, not `TRAEFIK_IP`. That is
normal for a proxied record and it is not evidence of a fault.

### authelia login depends on ts-web for a tailscale-up client

`auth.${SECRET_PUBLIC_DOMAIN}` carries an `extra_records` pin to the ts-web
proxy (#3720). A MagicDNS Hosts entry is exact, and it wins over the pushed
resolver. So **any client with tailscale up resolves `auth` to `100.64.0.3`,
including a client that is sitting on the LAN.**

The pin exists because every route that keeps the `forwardauth-authelia`
filter redirects to this hostname. Without the pin, blocky answers with
`TRAEFIK_IP`, a LAN address that no tailnet subnet route covers, so the
redirect dead-ends for a client that is off the LAN.

**Symptom during a ts-web outage:** you are on the LAN, `10.87.42.10` is
reachable, and the authelia login page still does not load. Every route
behind the filter then fails at the redirect, not at the route itself.

**Recovery: disconnect tailscale.** That drops the MagicDNS pin, restores the
blocky answer of `TRAEFIK_IP`, and the tier-1 route on `websecure` serves the
login page again. A LAN client that was never on the tailnet is unaffected
throughout.

This is a real narrowing of a break-glass path. #3724 deletes authelia and
removes the pin with it.

## Why this is accepted, not fixed

Two rounds of investigation examined every direction that could remove this
single point of failure. All were closed by mechanics, not by preference.

| Direction | Deciding fact |
|---|---|
| Two A records / two tailnet identities behind one name | headscale will store two `extra_records` entries for one name, but the client-side resolver returns only the first `IPv4` match. The second entry is silently discarded before any OS resolver or app sees the answer. Nothing to fail over to. |
| `RollingUpdate` with `maxSurge` (instead of `Recreate`) | Both pods would mount the same `TS_KUBE_SECRET` and load the same tailnet node key, so they resolve to a **single** headscale node, not two. The result is one flapping node — connection resets from two pods racing to overwrite each other's endpoints and DERP home — not a clean hand-off. This is a worse failure mode than the plain outage window `Recreate` already gives, not a milder one. |
| Tailscale Kubernetes Operator / `ProxyGroup` | The operator authenticates via OAuth client credentials against `api.tailscale.com`. headscale serves no tailnet REST admin API for it to talk to — only OIDC user login and a gRPC/CLI plane keyed by API key. |
| Tailscale Services / VIPService | Not implemented in headscale at the pinned version (v0.29.3). |
| blocky `customDNS` pin for a tier-3 hostname | Rejected on security posture, not mechanics: blocky is the LAN resolver, so a pin would make the hostname LAN-reachable off-tailnet, voiding the tailnet-only premise. `search` once carried such a pin. #3631 removed it and #3629 recorded the rule: a pin is break-glass bootstrap for an infrastructure name only, never for an ordinary application. Two pins remain, `unifi` and `auth`, and both name the tier-1 public listener address. |
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
- **LAN, not-tailnet client**, for any of the seventeen: no path exists, and
  none existed before the outage either. The hostname either does not
  resolve, or resolves to a public address that answers 404. `curl --resolve`
  does not help: `traefik-ts` is a ClusterIP Service, so no LAN address
  serves the listener. This client class cannot tell a ts-web outage from
  normal operation, so a report from it is not evidence either way. `auth` is
  the exception: this client keeps its blocky pin and reaches the login page
  throughout.
- **LAN, tailnet client**, for `auth`: the login page does not load, even
  though `10.87.42.10` answers. See "authelia login depends on ts-web for a
  tailscale-up client" above. Disconnect tailscale to recover.

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

### The port-forward workaround

Seventeen of the eighteen hostnames are served through ts-web and through
nothing else, so no client class keeps working over the network during an
outage. There is no per-hostname network workaround for those seventeen: no
blocky pin and no listener with a LAN address. Four of them keep an unowned
public CNAME, and it reaches an address that answers 404, so it is not a
workaround either.

`auth` is the exception. Its workaround is to disconnect tailscale, which
drops the `extra_records` pin and restores the blocky answer.

`kubectl port-forward` is the one path that survives, because it needs a
working kubeconfig and nothing else — no name resolution, no ts-web, and no
traefik. It reaches the backing Service directly:

```bash
# The three repair tools. Reach these first during a cluster fault.
kubectl -n networking port-forward svc/traefik-dashboard 8080:80
kubectl -n longhorn-system port-forward svc/longhorn-frontend 8081:80
kubectl -n kube-system port-forward svc/hubble-ui 8082:80
```

Browse the traefik dashboard at `http://localhost:8080/dashboard/`. The route
rewrites that prefix and a port-forward does not.

The same command works for any of the other fifteen hostnames — name that
service's own Service instead. For `auth` that is `svc/authelia` in the
`auth` namespace. It is the standard path for a convenience service, not a
documented break-glass step, because none of the fifteen sits on a repair
path.

Recovery of ts-web itself is still the path back to normal service.
port-forward restores access to one service at a time for one operator.

## The trigger has fired, and the risk is still accepted

Every rollout of this single-replica `Recreate` Deployment is a full outage
for eighteen hostnames. #3648 was the step that made it so: it took ts-web
from two consumers to six and removed the last LAN path. State that plainly:
**step 4 of #3635 removed a fallback that existed.** It did not discover that
the fallback was absent. #3667, #3719 and #3720 each widened the same blast
radius further, to eight, then to eleven, then to eighteen.

The risk is still accepted, and the reason is the repair path, not the count:

**No repair path depends on ts-web.** Three of the break-glass set —
`traefik`, `longhorn` and `hubble-ui` — sit behind ts-web alone since #3719,
which retired tier 2. The other two, `unifi` and `auth`, stay on the public
`websecure` listener and keep their blocky pins.

The `auth` pin no longer serves every client. Since #3720 it serves a client
that is not on the tailnet, and no other class: the `extra_records` Hosts
entry is exact, so a client with tailscale up resolves `auth` to ts-web even
on the LAN. See "authelia login depends on ts-web for a tailscale-up client"
above. The recovery is to disconnect tailscale, which needs no kubeconfig and
no name resolution.

That does not change the sentence in bold. No repair tool sits behind the
`forwardauth-authelia` filter: `unifi` never carried it, and `traefik`,
`longhorn` and `hubble-ui` lost theirs in wave A. The six routes that still
carry it are the *arr applications, and none of them repairs a cluster.

The three that moved did not lose their repair path; they changed it.
`kubectl port-forward` reaches each one directly, and it depends on a working
kubeconfig alone — not on ts-web, not on traefik, and not on name resolution.
See "The port-forward workaround" above for the commands. The operator
accepted that tradeoff in #3718 and #3719, in exchange for removing a
LAN-reachable LoadBalancer path from three unauthenticated admin UIs.

So a ts-web outage still costs convenience services and still costs nothing on
the recovery path of any cluster fault, including a ts-web fault itself. That
conclusion is unchanged; only the mechanism behind it changed.

`zigbee2mqtt` is the case worth stating, because the name suggests otherwise.
Zigbee automation runs over MQTT and does not traverse the web UI. A ts-web
outage costs administration of the Zigbee network, not its operation. Devices
keep working.

So this is an availability question about convenience services, not a
recovery-path question. That is why the fired trigger did not block #3648,
#3667 or #3719.

The design pass the trigger asks for is tracked in #3651. It is a design pass,
not a bug: HA is one option, a second replica under a distinct hostname is
another, and accepting the outage with a measured, documented recovery time is
a third.

The trigger will fire again. Waves D and E of #3718 move `feed`, `nas0` and
`unifi` to tier 3, which takes ts-web to twenty-one consumers. `unifi` is the
one to watch: it is a repair tool, so its move leaves `auth` as the only
break-glass name on a listener with a LAN address, and only for a client that
is not on the tailnet.

## When to revisit this decision

Revisit accept-and-document, rather than working around it in the moment,
if either of these becomes true:

- **A third service moves behind ts-web. This has FIRED — see "The trigger
  has fired, and the risk is still accepted" above.** The original blast
  radius (one pilot service with no fallback, one service with a partial
  fallback) was the basis for accepting the risk. Eighteen consumers changes
  that calculation.
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
