# kube-vip upgrade runbook

## Scope

This runbook upgrades the kube-vip static pod on the control-plane nodes
from `v0.6.2` to `v1.2.3`. It is a version bump only. It does not change VIP
mode, VIP address, ports, or leader-election tuning.

kube-vip runs as a static pod, not a Flux-managed resource. Flux cannot
manage it: kube-vip must be running before the k3s apiserver is reachable,
and Flux needs a reachable apiserver first. This is why the manifest lives
in `kubernetes/cluster0/bootstrap/kube-vip/kube-vip.yaml` — version
controlled, but placed on each node by hand, and never referenced by any
Flux `Kustomization`.

- **Current version:** `v0.6.2`
- **Target version:** `v1.2.3`
- **Manifest source:** `kubernetes/cluster0/bootstrap/kube-vip/kube-vip.yaml`
- **Nodes affected:** `node0`, `node1`, `node2` (control-plane), one at a
  time
- **VIP:** `10.87.42.2`

## Before you start

Only Ben (the human operator) can run the steps below — agents cannot SSH
to the nodes.

1. Confirm the cluster is healthy and the VIP answers:
   ```sh
   kubectl get nodes
   ```
   Run this against `10.87.42.2` (the VIP), not a node's direct IP, so the
   check also proves the VIP is up.

2. Confirm the on-node static-pod directory before you write anything. Do
   not assume the path — confirm it on each node:
   ```sh
   ssh 10.87.42.100 'ls /var/lib/rancher/k3s/agent/pod-manifests/'
   ```
   `/var/lib/rancher/k3s/agent/pod-manifests/` is the expected k3s default.
   If the directory does not exist or does not contain `kube-vip.yaml`,
   stop and locate the correct directory before continuing (check the k3s
   service args for a `--kube-vip-manifest-dir`-style override, if any).

3. Copy the new manifest to each node ahead of time so the per-node steps
   below are a fast swap, not an edit-in-place:
   ```sh
   scp kubernetes/cluster0/bootstrap/kube-vip/kube-vip.yaml \
     10.87.42.100:/tmp/kube-vip-v1.2.3.yaml
   scp kubernetes/cluster0/bootstrap/kube-vip/kube-vip.yaml \
     10.87.42.101:/tmp/kube-vip-v1.2.3.yaml
   scp kubernetes/cluster0/bootstrap/kube-vip/kube-vip.yaml \
     10.87.42.102:/tmp/kube-vip-v1.2.3.yaml
   ```
   Adjust node IPs/hostnames to match your inventory.

## Per-node procedure

Do one node at a time. Do not start the next node until the previous
node's kube-vip pod is `Running` and the VIP still answers.

Repeat this section for `node0`, then `node1`, then `node2`.

### 1. Back up the existing manifest on the node

```sh
ssh <node-ip> 'sudo cp /var/lib/rancher/k3s/agent/pod-manifests/kube-vip.yaml \
  /var/lib/rancher/k3s/agent/pod-manifests/kube-vip.yaml.bak-v0.6.2'
```

Do this before you overwrite anything. The backup is the rollback path
(see "Rollback" below).

### 2. Write the new manifest

```sh
ssh <node-ip> 'sudo cp /tmp/kube-vip-v1.2.3.yaml \
  /var/lib/rancher/k3s/agent/pod-manifests/kube-vip.yaml'
```

k3s's static-pod controller watches this directory and restarts the pod
automatically on file change — no separate restart command is needed. If
your node runs an older k3s that does not pick up the change, restart the
k3s service on that node only:

```sh
ssh <node-ip> 'sudo systemctl restart k3s'
```

### 3. Verify before moving to the next node

Wait for the new pod to come up, then check all three of the following:

```sh
# The upgraded node's kube-vip pod is Running on the new image
kubectl -n kube-system get pod kube-vip-<node-name> -o jsonpath='{.status.phase}{"\n"}{.spec.containers[0].image}{"\n"}'

# All three kube-vip pods are Running
kubectl -n kube-system get pods -l app.kubernetes.io/name=kube-vip

# The VIP still answers
kubectl --server=https://10.87.42.2:6443 get nodes
```

Confirm:
- The pod you just upgraded shows `Running` and the `v1.2.3` image.
- All three kube-vip pods show `Running` (not `CrashLoopBackOff` or
  `Pending`).
- `kubectl get nodes` via the VIP succeeds and lists all nodes.

Do not proceed to the next node until all three checks pass. If any check
fails, stop and follow "Rollback" below for the node you just changed
before touching the next one.

## Rollback

Rollback is per-node and cheap. If a node fails verification after the
upgrade:

```sh
ssh <node-ip> 'sudo cp /var/lib/rancher/k3s/agent/pod-manifests/kube-vip.yaml.bak-v0.6.2 \
  /var/lib/rancher/k3s/agent/pod-manifests/kube-vip.yaml'
ssh <node-ip> 'sudo systemctl restart k3s'
```

Re-run the verification checks from step 3 against that node before
deciding whether to retry the upgrade or hold at `v0.6.2` for that node.

## After all three nodes are upgraded

Confirm the fleet-wide state:

```sh
kubectl -n kube-system get pods -l app.kubernetes.io/name=kube-vip -o wide
```

All three pods should report the `v1.2.3` image and `Running` status, and
`kubectl get nodes` via `10.87.42.2` should continue to work.
