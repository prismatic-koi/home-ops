# Tailnet control-plane access for node0/1/2

## Purpose

The k3s control-plane hosts node0, node1, and node2 join the headscale tailnet
and advertise the control-plane VIP `10.87.42.2/32`. This gives operators a
tailnet path to the Kubernetes API server without a public port-forward.

This state lives on the bare-metal Ubuntu hosts. This repository does not
manage it. `tailscale up` is host state, not IaC. Use this runbook when you
rebuild a host or re-register it on the tailnet.

## The `tailscale up` invocation

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
  the next section before you remove it.

The host registers under the `tag:ts-cp` tag. The tag rides on the preauth key,
not on `--advertise-tags`. headscale 0.28+ rejects a registration that uses a
preauth key and also passes `--advertise-tags`. Mint the key with the tag:

```bash
headscale preauthkeys create --user homeops@ --tags tag:ts-cp --reusable
```

## Why `--accept-dns=false` is load-bearing

headscale pushes `100.64.0.14` (the blocky-tailnet resolver) as the global
resolver to every client that accepts DNS. blocky-tailnet runs on this cluster.

If a control-plane host accepted that resolver, the host would resolve DNS
through blocky. A cluster outage would then take blocky down. The host would
lose the DNS it needs to recover the cluster. This is a circular dependency:
the hosts that recover the cluster must not depend on the cluster for DNS.

`--accept-dns=false` keeps each host on its own local resolver. The host
resolves DNS during an outage and can recover the cluster.

## How to verify

Confirm that the host does not accept the pushed resolver:

```bash
tailscale debug prefs | jq .CorpDNS
```

The output must be `false`. If it is `true`, the host accepts the pushed
resolver and the circular dependency is live. Re-run `tailscale up` with
`--accept-dns=false`.

## Route approval

The `10.87.42.2/32` route is auto-approved. The `autoApprovers` block in
`kubernetes/cluster0/apps/networking/headscale/app/config/policy.hujson` names
`tag:ts-cp` as the approver for this prefix. No operator step approves the route
by hand. The route is live as soon as the host registers with the tag and
advertises the prefix.
