# Incident: etcd Snapshot I/O Stall — 2026-03-25

**Date:** 2026-03-25 ~06:22 NZDT  
**Duration:** ~38 seconds of API server unresponsiveness  
**Impact:** Mass pod restarts on node0; Prometheus alerts for degraded Longhorn volume and Plex probe failure  
**Root cause:** etcd internal snapshot triggered after 10,000 raft log entries, causing a brief raft apply loop pause that starved the API server and kubelet

---

## Timeline

| Time (NZDT) | Event |
|---|---|
| 06:21:39 | API server begins returning `http: Handler timeout` and `invalid bearer token` / `context canceled` errors — etcd starting to stall |
| 06:21:56 | `dial tcp 10.42.0.55:10250: connect: no route to host` — kubelet on node0 becomes unreachable as etcd I/O starves the network stack |
| 06:22:17 | Mass `client disconnected` — all API watchers dropped simultaneously |
| 06:22:18 | Longhorn admission webhook unreachable |
| 06:22:23 | etcd triggers internal snapshot after applying 10,000 entries (smoking gun) |
| 06:23:04 | Recovery begins; kubelet cleans up stale pod states |
| 06:23:08–06:23:32 | Pods come back up (sonarr ~4s, grafana ~25s, authelia-postgres ~22s, minio ~26s, plex ~28s) |

---

## Root Cause

k3s uses an embedded etcd. By default, etcd takes an internal snapshot every **10,000 raft log entries** (`--snapshot-count`). During this snapshot:

1. etcd serializes the entire in-memory state to disk
2. The raft apply loop **pauses** — no new entries can be applied
3. In k3s's single-process architecture, etcd, the API server, and kubelet all share the same process — so a disk stall starves all of them simultaneously

In an active cluster (Flux reconciliation, Longhorn health checks, pod lease renewals) 10,000 entries can accumulate in minutes.

**k3s was not restarted.** PID 812 was consistent throughout all logs. This was not an upgrade, reboot, or human intervention.

---

## What Was Ruled Out

- PR automerge / Renovate activity (last merge was the day prior)
- system-upgrade-controller (upgrade plans completed March 6)
- OOM / disk pressure / PID pressure (no node conditions)
- Disk hardware (all nodes use NVMe SSDs — latency not the issue)

---

## Recovery

Full recovery within ~90 seconds. All Longhorn volumes returned to healthy. All pods running normally post-incident.

---

## Secondary Finding: Certificate Expiry

All three control-plane nodes have leaf certs expiring **2026-07-22** (~119 days from incident date). k3s auto-rotates these on restart when within 90 days of expiry (~2026-04-23). No action needed now, but ensure k3s restarts at least once between late April and July 22 (e.g. via a k3s upgrade or node reboot).

CAs are long-lived and expire 2033.

---

## Potential Mitigations

### Option 1: Increase `--snapshot-count` (low priority)

Add to `/etc/rancher/k3s/config.yaml` on each server node:

```yaml
etcd-arg:
  - "snapshot-count=50000"
```

Snapshots less frequently, reducing stall frequency at the cost of a larger raft log between snapshots and slightly longer crash recovery. **Not recommended yet** — this was the first observed incident in 245 days of cluster operation.

### Option 2: Monitor for recurrence

Watch for etcd commit duration spikes or API server latency alerts in Prometheus. If stalls recur regularly, revisit tuning.

---

## Notes

- k3s also runs **scheduled etcd backups** (`--etcd-snapshot-schedule-cron`, default every 12h) — this is a separate operation from the internal raft snapshot and is less likely to cause API stalls.
- A dedicated external etcd cluster would isolate this class of problem entirely, but is significant operational overhead for a homelab.
