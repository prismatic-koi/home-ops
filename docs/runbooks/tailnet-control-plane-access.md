# Tailnet access to the k3s hosts node0-3

## Purpose

The four k3s hosts join the headscale tailnet. This gives an operator two paths
that do not depend on a public port-forward or a cloudflared tunnel:

- The Kubernetes API server, through the control-plane VIP `10.87.42.2`.
- SSH to each host, on port 22 (issue #3579).

This state lives on the bare-metal Ubuntu hosts. This repository does not manage
it. `tailscale up` is host state, not IaC. Use this runbook when you rebuild a
host or you register it again on the tailnet.

## Which host takes which tag

| Hosts | Tag | Advertises a route | Approves a route |
|---|---|---|---|
| node0, node1, node2 | `tag:ts-cp` | `10.87.42.2/32` | Yes, automatically |
| node3 | `tag:ts-node` | No | No |

node3 is the worker node. It is not a control-plane host, and it advertises no
route. It takes a different tag for that reason. `tag:ts-cp` is the
auto-approver identity for the control-plane VIP. A worker host with that tag
can advertise the VIP and approve it. `tag:ts-node` is absent from the
`autoApprovers` block, and it must stay absent.

Both tags are grant destinations for `tcp:22`. Only `tag:ts-cp` carries the
route that serves `tcp:6443`.

## The `tailscale up` invocation — node0, node1, node2

Run this on the host, as root:

```bash
sudo tailscale up \
  --login-server=https://hs.${SECRET_PUBLIC_DOMAIN} \
  --advertise-routes=10.87.42.2/32 \
  --accept-dns=false
```

Each flag is load-bearing:

- `--login-server` points the host at the headscale control plane, not the
  public tailscale service.
- `--advertise-routes=10.87.42.2/32` advertises the kube-vip control-plane VIP.
  A tailnet client reaches the Kubernetes API server through this route.
- `--accept-dns=false` stops the host from accepting the pushed resolver. Read
  "Why `--accept-dns=false` is load-bearing" before you remove it.

The host registers under the `tag:ts-cp` tag. The tag rides on the preauth key,
not on `--advertise-tags`. headscale 0.28+ rejects a registration that uses a
preauth key and also passes `--advertise-tags`. Mint the key with the tag:

```bash
headscale preauthkeys create --user homeops@ --tags tag:ts-cp --reusable
```

## The `tailscale up` invocation — node3

Mint a preauth key that carries `tag:ts-node`:

```bash
headscale preauthkeys create --user homeops@ --tags tag:ts-node --reusable
```

Then run this on node3, as root:

```bash
sudo tailscale up \
  --login-server=https://hs.${SECRET_PUBLIC_DOMAIN} \
  --accept-dns=false
```

The differences from node0/1/2 are deliberate:

- **There is no `--advertise-routes` flag.** node3 is the worker node. It
  advertises no route. Do not add the flag. If node3 advertises
  `10.87.42.2/32`, the route stays unapproved, because `tag:ts-node` is not an
  auto-approver for that prefix.
- **The tag is `tag:ts-node`, not `tag:ts-cp`.** The tag comes from the preauth
  key above. Do not reuse a `tag:ts-cp` key on node3.
- `--accept-dns=false` is the same on all four hosts, for the same reason. The
  next section states it.

## Why `--accept-dns=false` is load-bearing

headscale pushes `100.64.0.14` (the blocky-tailnet resolver) as the global
resolver to every client that accepts DNS. blocky-tailnet runs on this cluster.

If a k3s host accepted that resolver, the host would resolve DNS through blocky.
A cluster outage would then take blocky down. The host would lose the DNS it
needs to recover the cluster. This is a circular dependency: the hosts that
recover the cluster must not depend on the cluster for DNS.

`--accept-dns=false` keeps each host on its own local resolver. The host
resolves DNS during an outage and can recover the cluster.

This applies to node3 as well as to node0/1/2. The comment at
`kubernetes/cluster0/apps/networking/headscale/app/config/config.yaml` names all
four hosts. Keep that claim true.

## SSH over the tailnet

The policy grants `ben@` SSH to both tags:

```
{ "src": ["ben@"], "dst": ["tag:ts-cp"],   "ip": ["tcp:22"] }
{ "src": ["ben@"], "dst": ["tag:ts-node"], "ip": ["tcp:22"] }
```

Each grant opens port 22 only. SSH terminates on the host itself, so the
destination is the tailnet address of the host, in `100.64.0.0/10`. Connect to
that address. `tailscale status` reports it:

```bash
tailscale status | grep node3
ssh <user>@100.64.0.<n>
```

Do not connect to `10.87.42.2` on port 22. That address is the floating
control-plane VIP. A connection to it lands on the host that holds the VIP at
the time, so the target is not predictable. The policy denies port 22 on the
VIP, and the tests block asserts that deny.

## Order of operations after a policy change

The policy names `tag:ts-node`. A headscale policy test fails when its
destination resolves to no IP addresses. So the `tag:ts-node` assertions fail
until node3 registers.

On the boot path — the path a ConfigMap edit takes, because Reloader restarts
the pod — headscale logs `policy tests failed at boot; server starting anyway`
and **applies the policy anyway**. Every grant goes live. This includes the SSH
grant for `tag:ts-cp`. Register node3 to clear the warning.

Do **not** run `headscale policy reload` (SIGHUP) while `tag:ts-node` has no
registered node. That path rejects the write and keeps the previous policy live.

## How to verify

Confirm that the host does not accept the pushed resolver:

```bash
tailscale debug prefs | jq .CorpDNS
```

The output must be `false`. If it is `true`, the host accepts the pushed
resolver and the circular dependency is live. Run `tailscale up` again with
`--accept-dns=false`.

Confirm that each host carries the correct tag:

```bash
headscale nodes list
```

node0, node1 and node2 must show `tag:ts-cp`. node3 must show `tag:ts-node`.

Confirm that the policy tests pass:

```bash
kubectl -n networking logs statefulset/headscale | grep "policy tests failed at boot"
```

The command must return no line. A line names the failed assertions. Read the
previous section before you treat it as an outage.

## Route approval

The `10.87.42.2/32` route is auto-approved. The `autoApprovers` block in
`kubernetes/cluster0/apps/networking/headscale/app/config/policy.hujson` names
`tag:ts-cp` as the approver for this prefix. No operator step approves the route
by hand. The route is live as soon as a `tag:ts-cp` host registers and
advertises the prefix.

node3 needs no route approval. It advertises no route.
