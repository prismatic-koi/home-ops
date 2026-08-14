# Runbook: tailnet path to the Kubernetes control plane

**Issue:** #3477
**Audience:** a human operator with physical or LAN SSH access to node0, node1 and node2.
**Estimated time:** 45 minutes, plus the off-LAN checks.

## Purpose

The k3s API server is reachable from the public internet through a router
port-forward of tcp/6443 to the home WAN address. An unauthenticated request
from off-network gets a `401`, so the port answers strangers.

This procedure replaces that path with a tailnet path, then removes the
port-forward. After the procedure, the control plane is reachable at
`https://10.87.42.2:6443` from tailnet devices only.

## How the path works

`tailscaled` runs on the **hosts** node0/1/2, not in Kubernetes. An in-cluster
proxy would be a circular dependency: headscale runs on this cluster, so a
cluster outage would remove the only path in.

Each host `tailscaled` does two jobs:

1. It registers as its own tailnet node.
2. It advertises `10.87.42.2/32` as a subnet route. `10.87.42.2` is the
   kube-vip control-plane VIP.

kube-vip runs in ARP mode. Only the host that currently owns the VIP answers
ARP for it. All three hosts share one L2 segment, so any of the three can
forward to the owner. headscale elects one primary subnet router for the
prefix at a time and re-elects on failure. Health probing runs on a 10 s
interval with a 5 s timeout.

The policy side of this is already merged. It is in
`kubernetes/cluster0/apps/networking/headscale/app/config/policy.hujson`:

- `tag:ts-cp` in `tagOwners`, owned by `homeops@`.
- `autoApprovers.routes` maps `10.87.42.2/32` to `["tag:ts-cp"]`.
- A grant lets `ben@` reach `10.87.42.2/32` on `tcp:6443`, and nothing else.

## Before you start

Read these six traps first. Each one has caused a failure or a confusing
outcome before.

1. **Do not pass `--advertise-tags`.** headscale 0.28 and later reject a
   registration that uses a pre-auth key **and** `--advertise-tags` together.
   The tag comes from the pre-auth key. This is the same rule the in-cluster
   proxies follow.
2. **Do not pass `--accept-routes` on node0/1/2.** A control-plane host that
   accepts routes can learn `10.87.42.2/32` from a peer and send VIP traffic
   into the tailnet instead of to the local kube-vip. Advertise only.
3. **Advertise exactly `10.87.42.2/32`.** Auto-approval covers a route equal
   to or narrower than the key in `autoApprovers`. A broader route such as
   `10.87.42.0/24` is not approved and stays down until you approve it by
   hand.
4. **Do one host at a time.** These are control-plane nodes. Complete Steps 2
   to 5 on one host and check cluster health before you start the next host.
5. **Keep sshd on the LAN addresses.** LAN SSH is the break-glass path. Read
   Step 7 before you change any sshd setting.
6. **Do the off-LAN checks before you touch the router.** Step 10 is the gate
   for Step 11. Do not reorder them.

### Values you need

Set these in the shell **on the machine that runs `kubectl`**.

```bash
export HEADSCALE_URL=https://hs.tinfoilforest.nz
export WAN_IP=<your public WAN address>
```

- `HEADSCALE_URL` is the `server_url` value in
  `kubernetes/cluster0/apps/networking/headscale/app/config/config.yaml`.
- `WAN_IP` is the home public address. Read it from the router status page, or
  run `curl -s https://api.ipify.org` from a machine on the home LAN.

**Record `WAN_IP` somewhere you can read it later.** Step 11 removes the
port-forward, and the rollback in this runbook needs the address again.

Step 4 sets one more value, `TS_AUTHKEY`, in the shell **on each host**. It is
not the same shell as this one.

## Step 1 — Create the pre-auth key

Run this on the machine that runs `kubectl`.

```bash
kubectl -n networking exec -it sts/headscale -- \
  headscale preauthkeys create --tags tag:ts-cp --reusable --expiration 2h
```

Copy the key that the command prints. You paste it into each host in Step 4.

The key is reusable so that all three hosts share it. It expires in two hours.
If the CLI asks for a user, get the numeric ID from
`kubectl -n networking exec -it sts/headscale -- headscale users list` and add
`--user <id>`. The registered nodes carry the tag and no user either way,
because a tagged node must not have a `user_id`.

---

**Steps 2 to 5 are per-host. Do node0 first. Complete Step 5 before you start
node1. Then do node2.** See trap 4.

---

## Step 2 — Enable IPv4 forwarding on the host

A subnet router must forward packets.

```bash
printf 'net.ipv4.ip_forward = 1\n' | sudo tee /etc/sysctl.d/99-tailscale.conf
sudo sysctl -p /etc/sysctl.d/99-tailscale.conf
```

Check the value:

```bash
sysctl net.ipv4.ip_forward
```

It must report `= 1`.

**IPv6 forwarding is deliberately absent. Do not add it.** The advertised
route is IPv4-only and this cluster is IPv4-only. Setting
`net.ipv6.conf.all.forwarding=1` makes the kernel stop accepting IPv6 router
advertisements. A host that gets its IPv6 address and default route by SLAAC
then loses both. If some later change does need IPv6 forwarding, add
`net.ipv6.conf.all.accept_ra=2` in the same file.

## Step 3 — Install tailscaled on the host

The hosts run Ubuntu 24.04.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

Check the daemon is enabled and running:

```bash
systemctl is-enabled tailscaled
systemctl is-active tailscaled
```

They must report `enabled` and `active`.

## Step 4 — Join the tailnet

Paste the key from Step 1 into the shell **on this host**:

```bash
export TS_AUTHKEY='<paste the key from Step 1 here>'
export HEADSCALE_URL=https://hs.tinfoilforest.nz
```

Then join. Change `--hostname` to match the host you are on: `node0`, `node1`
or `node2`.

```bash
sudo tailscale up \
  --login-server="${HEADSCALE_URL}" \
  --authkey="${TS_AUTHKEY}" \
  --hostname=node0 \
  --advertise-routes=10.87.42.2/32 \
  --accept-routes=false \
  --accept-dns=false
```

If `--authkey=` is empty, you are in a shell that has no `TS_AUTHKEY`. Set it
on this host and run the command again.

Flag by flag:

- `--advertise-routes=10.87.42.2/32` offers the VIP to the tailnet. This is
  the whole point of the change.
- `--accept-routes=false` stops the host from learning routes from peers. See
  trap 2.
- `--accept-dns=false` stops tailscale from changing the host resolver. The
  in-cluster proxies use the same setting. A control-plane host must keep the
  resolver that k3s expects.
- There is no `--advertise-tags`. See trap 1.
- There is no `--advertise-exit-node`. These hosts are not exit nodes.

Check the host joined:

```bash
tailscale status
```

## Step 5 — Check cluster health, then go to the next host

Run this on the machine that runs `kubectl`.

```bash
kubectl get nodes
```

All three nodes must report `Ready`. If any node is not `Ready`, stop and find
the cause before you touch the next host.

Now go back to Step 2 for the next host. When all three hosts are done,
continue to Step 6.

## Step 6 — Check registration and route approval

Run this on the machine that runs `kubectl`.

```bash
kubectl -n networking exec -it sts/headscale -- headscale nodes list
kubectl -n networking exec -it sts/headscale -- headscale nodes list-routes
```

Expected result:

- `headscale nodes list` shows `node0`, `node1` and `node2`, each with
  `tag:ts-cp`.
- `headscale nodes list-routes` shows `10.87.42.2/32` as **Approved** and
  **Available** on all three, and **Serving (Primary)** on exactly one.

If a route is Available but not Approved, the auto-approver did not match.
Check that the host advertised `10.87.42.2/32` and not a broader prefix. See
trap 3. To approve by hand while you find the cause:

```bash
kubectl -n networking exec -it sts/headscale -- \
  headscale nodes approve-routes --identifier <node-id> --routes 10.87.42.2/32
```

Check the policy self-tests still pass:

```bash
kubectl -n networking logs sts/headscale | grep -i 'policy tests failed'
```

This must print nothing.

## Step 7 — Check sshd still listens on the LAN

**LAN SSH is the break-glass path.** If the tailnet path fails, or headscale
is down, or a host loses its tailnet registration, LAN SSH is how you get back
in. It must keep working.

sshd must keep listening on the LAN addresses `10.87.42.100`, `10.87.42.101`
and `10.87.42.102`. Do **not** bind sshd to the tailnet interface only. Do
**not** add a `ListenAddress` line that names a `100.64.0.0/10` address and
removes the LAN address.

Check on each host:

```bash
sudo ss -lntp | grep ':22 '
sudo grep -i '^ListenAddress' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/* 2>/dev/null
```

Expected result: sshd listens on `0.0.0.0:22`, or on the LAN address of the
host, and no `ListenAddress` line removes the LAN address. If the second
command prints nothing, sshd listens on all addresses. That is correct for
this procedure.

Retiring cloudflared for SSH is a separate change. This procedure does not
grant SSH over the tailnet. The merged policy grants `tcp:6443` only.

## Step 8 — Turn on `--accept-routes` on your machines

A tailscale client ignores every advertised subnet route until you opt in. Do
this on `navi` and on `m4mac`.

```bash
sudo tailscale set --accept-routes
```

On macOS, if the CLI is not on your `PATH`, use the menu-bar toggle for
accepting subnet routes instead.

**`--accept-routes` is required even though the route is a `/32`.** There is
no size threshold and no small-prefix exception. tailscale installs no
advertised route into the client routing table without the opt-in. A `/32`
also gets no fallback: `10.87.42.2` is not the tailnet address of any node, so
the client cannot reach it as a peer. Without this step the connection times
out while `tailscale status` looks completely healthy.

Check the client accepted the route:

```bash
tailscale status --json | jq -r '.Peer[].AllowedIPs' | grep 10.87.42.2
```

Do **not** use `ip route get 10.87.42.2` for this check while the machine is
on the home LAN. It returns the direct LAN route and tells you nothing about
the tailnet path. Step 10 is the real gate.

## Step 9 — Point kubeconfig at the VIP

On `navi` and `m4mac`, change the cluster server URL in your kubeconfig to the
VIP.

```bash
kubectl config set-cluster <cluster-name> --server=https://10.87.42.2:6443
```

You need no certificate work. The API server serving certificate already
carries `10.87.42.2` as an IP SAN. It expires 2027-03-24.

## Step 10 — Check from off-LAN. This is the gate.

**Do not skip this step. Do not do Step 11 first.**

Take each machine off the home LAN. A phone hotspot or mobile tethering is
enough. Then run:

```bash
kubectl get nodes
```

The command must succeed and list node0, node1 and node2.

Run it on **both** machines, `navi` and `m4mac`, each one off-LAN. Both must
succeed. If either fails, stop. Do not remove the port-forward. Go to
"Troubleshooting".

Optional extra check, still off-LAN:

```bash
curl -sk -o /dev/null -w '%{http_code}\n' https://10.87.42.2:6443/
```

A `401` is the correct answer. It shows the API server answered.

## Step 11 — Remove the tcp/6443 port-forward. Final step.

Do this only after Step 10 succeeded on both machines.

In the router admin interface, remove the port-forward rule that maps
**tcp/6443** to the LAN. That rule is the public exposure this issue closes.

**Leave every other port-forward in place.** The Traefik forwards and the Plex
forward stay. Change the tcp/6443 rule only.

Check the port is closed. Run this from off-LAN:

```bash
curl -sk --connect-timeout 10 "https://${WAN_IP}:6443/" ; echo "exit=$?"
```

Expected result: a timeout or a refused connection, and a non-zero exit code.
A `401` means the rule is still live.

Then check the tailnet path still works, still off-LAN:

```bash
kubectl get nodes
```

## Rollback

Use this if the tailnet path fails after Step 11, or at any point where you
need the old path back.

1. **Add the router port-forward again.** In the router admin interface,
   create a port-forward for **tcp/6443** to the control plane. It is the same
   rule you removed in Step 11.
2. **Revert the kubeconfig.** On each machine, point the server URL back to
   the public address:

   ```bash
   kubectl config set-cluster <cluster-name> --server=https://${WAN_IP}:6443
   ```

   `WAN_IP` is the home public address you recorded in "Values you need".

3. **Check.** Run `kubectl get nodes` from off-LAN. It must succeed.

The rollback needs no change to headscale, to the policy, or to the hosts. The
tailnet path and the port-forward path can both be live at once. Leave
`tailscaled` running on the hosts while you diagnose.

To remove the host tailnet nodes as well, run `sudo tailscale down` on each
host, then remove the nodes:

```bash
kubectl -n networking exec -it sts/headscale -- \
  headscale nodes delete --identifier <node-id>
```

## Troubleshooting

**`kubectl get nodes` times out off-LAN, and `tailscale status` looks healthy.**
The client did not accept the route. Repeat Step 8. This is the most common
cause.

**`headscale nodes list-routes` shows the route as Available but not Approved.**
The advertised prefix does not match the `autoApprovers` key. Check the host
advertised `10.87.42.2/32` exactly. See trap 3.

**Registration fails with "requested tags are invalid or not permitted".**
The `tailscale up` command included `--advertise-tags` together with a
pre-auth key. Remove `--advertise-tags` and repeat Step 4. See trap 1.

**The route is Approved on all three hosts but traffic still fails.**
Find which host serves the route as Primary, then check that the same host can
reach the VIP:

```bash
kubectl -n networking exec -it sts/headscale -- headscale nodes list-routes
# then, on the Primary host:
curl -sk -o /dev/null -w '%{http_code}\n' https://10.87.42.2:6443/
```

If the Primary host cannot reach the VIP, the fault is kube-vip or L2, not the
tailnet.

**A host lost its tailnet registration.**
Use LAN SSH to get in. Repeat Step 4 with a fresh pre-auth key. The other two
hosts keep serving the route while you do this, because headscale re-elects a
Primary on failure.

## Reference

Verified facts behind this procedure:

- The API server serving certificate has IP SANs for `10.43.0.1`, `10.87.1.0`,
  `10.87.1.1`, `10.87.1.2`, `10.87.42.0`, `10.87.42.2`, `10.87.42.100`,
  `10.87.42.101`, `10.87.42.102`, the public WAN address, `127.0.0.1` and
  `::1`. It expires 2027-03-24. There is no `100.64.0.0/10` SAN, so a tailnet
  address is not a usable target.
- kube-vip runs in ARP mode with `address=10.87.42.2` and `port=6443`.
- headscale v0.29.3 supports HA subnet routers with automatic failover. Health
  probing runs at a 10 s interval with a 5 s timeout. If every advertiser is
  unhealthy, headscale leaves the prefix unmapped rather than pointing it at a
  bad node.
- `k8s.tinfoilforest.nz` has no public A record. Use the IP. This procedure
  makes no DNS change.

Out of scope for this procedure: any DNS change, retiring cloudflared for SSH,
kube-vip version work, and any port-forward other than tcp/6443.
