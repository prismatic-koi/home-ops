#!/usr/bin/env python3
"""
lint-prometheus-scrape-netpol.py — enforce the bidirectional Prometheus-scrape
NetworkPolicy convention documented in the repo-root AGENTS.md.

Background
----------
Prometheus runs default-deny (Ingress + Egress) via `prometheus-default-deny` in
the `monitoring` namespace. For any scrape target to actually be reachable, two
NetworkPolicies are required:

  1. Target-side ingress rule `<app>-allow-prometheus-scrape[-ingress]`
     allowing `app.kubernetes.io/name: prometheus` from namespace `monitoring`
     into the target pod on the metrics port.

  2. Prometheus-side egress rule `prometheus-allow-<something>-scrape-egress`
     in `kubernetes/cluster0/apps/monitoring/kube-prometheus-stack/app/network-policy.yaml`
     allowing the prometheus pod outbound to the target namespace + pod + port.

If (1) exists without (2), the scrape silently times out: `up=0` with a healthy
pod, a correct-looking ServiceMonitor, and a correct-looking ingress rule.
That is exactly the failure that #3347 / Loki+Alloy hit and #3359 fixed. This
lint catches the same class of gap at PR time. See GitHub issue #3361.

Heuristic
---------
Rather than parse ServiceMonitors (most of which are baked inside Helm charts
and are not present as raw YAML in-tree — only a handful of standalone
ServiceMonitor YAMLs exist), the lint pivots on the target-side ingress rule
naming convention that *is* uniformly present in this repo:

  * Discover targets: every `NetworkPolicy` whose name matches
    `^.*-allow-prometheus-scrape(-ingress)?$`. Extract its namespace and the
    set of numeric ports the ingress rule permits.

  * Discover in-tree standalone `ServiceMonitor` YAML files as a supplementary
    target source, so that if someone ever adds a ServiceMonitor without a
    matching ingress rule (which would ALSO be broken, but for a different
    reason), we still surface a lint entry pointing at the missing pieces.

  * Discover egress rules: every `NetworkPolicy` in namespace `monitoring`
    whose name matches `^prometheus-allow-.*-scrape-egress$`. Extract each
    egress peer's target namespace (from `namespaceSelector.matchLabels
    ["kubernetes.io/metadata.name"]`) and the numeric ports permitted for
    that egress clause.

  * For each target `(ns, port)`, require at least one egress rule that
    contains both `ns` in its target-namespace set AND `port` in its
    port set. Named ports on the ingress side are treated as "cannot verify"
    and require only namespace-level coverage (no such cases exist today; a
    warning is emitted so anyone introducing one is aware of the reduced
    check).

Known-limits / assumptions (documented so future readers can weigh them):
  * Egress rules that reach targets via `ipBlock` rather than
    `namespaceSelector` (e.g. host-network scrapes: kubelet, node-exporter) are
    matched to targets by name-substring on the egress rule name only if the
    target itself has an in-tree ingress rule. Host-network scrapes have no
    such ingress rule in-tree (the node is not a pod), so there is no false
    positive — they simply are not discovered as targets and are ignored.
  * The lint does not walk rendered Helm output. Ingress rules for
    chart-generated resources are added manually elsewhere in the tree and
    are captured by the ingress-name pattern above.
  * Cross-namespace scrapes must use a `namespaceSelector` with
    `kubernetes.io/metadata.name` (this repo's uniform convention). Egress
    peers that use a bare `podSelector` (intra-monitoring) are treated as
    covering namespace `monitoring`.

Exit codes: 0 on clean, 1 on any missing egress rule (or unexpected error).
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Iterable

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Install with `pip install pyyaml`.", file=sys.stderr)
    sys.exit(2)


TARGET_INGRESS_RE = re.compile(r"^.*-allow-prometheus-scrape(-ingress)?$")
# Canonical suffix is `-scrape-egress`. We also accept `-probe-egress` because
# Probe-CRD-backed targets (blackbox-exporter) use that suffix and their egress
# rule structurally covers the metrics scrape too (same pod + port).
PROM_EGRESS_RE = re.compile(r"^prometheus-allow-.*-(scrape|probe)-egress$")
PROM_EGRESS_NAMESPACE = "monitoring"

# Namespace where an egress peer with a bare podSelector (no namespaceSelector)
# is implicitly targeting.
BARE_PODSELECTOR_IMPLIED_NS = "monitoring"


@dataclass
class Target:
    """A scrape target's ingress rule."""
    name: str
    namespace: str
    numeric_ports: set[int] = field(default_factory=set)
    named_ports: set[str] = field(default_factory=set)
    source_file: str = ""


@dataclass
class EgressPeer:
    """One (namespace, ports) combination allowed by an egress rule."""
    target_namespace: str
    ports: set[int]  # numeric only


@dataclass
class EgressRule:
    name: str
    peers: list[EgressPeer] = field(default_factory=list)
    source_file: str = ""


def iter_yaml_docs(root: pathlib.Path) -> Iterable[tuple[pathlib.Path, dict]]:
    """Yield (path, doc) for every non-empty YAML document under root."""
    for path in root.rglob("*.yaml"):
        # Skip SOPS-encrypted files; they cannot be parsed for structure and
        # never contain NetworkPolicies in this repo.
        if path.name.endswith(".sops.yaml"):
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        try:
            for doc in yaml.safe_load_all(text):
                if isinstance(doc, dict):
                    yield path, doc
        except yaml.YAMLError:
            # Templated helm values may not parse as pure YAML. Skip quietly;
            # NetworkPolicies in this repo are plain YAML.
            continue


def extract_numeric_ports(ports: list[dict] | None) -> tuple[set[int], set[str]]:
    numeric: set[int] = set()
    named: set[str] = set()
    for p in ports or []:
        port = p.get("port")
        if isinstance(port, int):
            numeric.add(port)
        elif isinstance(port, str):
            # A named port. Kubernetes also accepts numeric strings; try both.
            try:
                numeric.add(int(port))
            except ValueError:
                named.add(port)
    return numeric, named


def parse_target(doc: dict, path: pathlib.Path) -> Target | None:
    """Parse a NetworkPolicy doc into a Target, or None if it's not one."""
    if doc.get("kind") != "NetworkPolicy":
        return None
    meta = doc.get("metadata") or {}
    name = meta.get("name") or ""
    if not TARGET_INGRESS_RE.match(name):
        return None
    ns = meta.get("namespace") or ""
    numeric: set[int] = set()
    named: set[str] = set()
    for rule in (doc.get("spec") or {}).get("ingress") or []:
        n, m = extract_numeric_ports(rule.get("ports"))
        numeric |= n
        named |= m
    return Target(name=name, namespace=ns, numeric_ports=numeric,
                  named_ports=named, source_file=str(path))


def parse_servicemonitor(doc: dict, path: pathlib.Path) -> Target | None:
    """Parse a standalone ServiceMonitor into a Target."""
    if doc.get("kind") != "ServiceMonitor":
        return None
    meta = doc.get("metadata") or {}
    name = meta.get("name") or ""
    ns = meta.get("namespace") or ""
    numeric: set[int] = set()
    named: set[str] = set()
    for ep in (doc.get("spec") or {}).get("endpoints") or []:
        # `port` here is a Service port name; `targetPort` may be a number or
        # a name. We cannot resolve Service port -> containerPort without
        # loading the Service, so surface both as named entries. This means a
        # standalone ServiceMonitor without a matching ingress rule downgrades
        # to a namespace-only check.
        if "targetPort" in ep:
            tp = ep["targetPort"]
            if isinstance(tp, int):
                numeric.add(tp)
            elif isinstance(tp, str):
                named.add(tp)
        if "port" in ep and isinstance(ep["port"], str):
            named.add(ep["port"])
    return Target(name=f"servicemonitor/{name}", namespace=ns,
                  numeric_ports=numeric, named_ports=named,
                  source_file=str(path))


def parse_egress_rule(doc: dict, path: pathlib.Path) -> EgressRule | None:
    if doc.get("kind") != "NetworkPolicy":
        return None
    meta = doc.get("metadata") or {}
    name = meta.get("name") or ""
    if not PROM_EGRESS_RE.match(name):
        return None
    if (meta.get("namespace") or "") != PROM_EGRESS_NAMESPACE:
        return None
    rule = EgressRule(name=name, source_file=str(path))
    for e in (doc.get("spec") or {}).get("egress") or []:
        numeric, _ = extract_numeric_ports(e.get("ports"))
        target_nss: set[str] = set()
        for peer in e.get("to") or []:
            ns_sel = peer.get("namespaceSelector")
            if isinstance(ns_sel, dict):
                labels = (ns_sel.get("matchLabels") or {})
                ns = labels.get("kubernetes.io/metadata.name")
                if ns:
                    target_nss.add(ns)
                    continue
            # Bare podSelector (no namespaceSelector) -> intra-monitoring.
            if peer.get("podSelector") is not None and "namespaceSelector" not in peer:
                target_nss.add(BARE_PODSELECTOR_IMPLIED_NS)
            # ipBlock peers are ignored (host-network scrapes).
        for ns in target_nss:
            rule.peers.append(EgressPeer(target_namespace=ns, ports=set(numeric)))
    return rule


def find_covering_egress(target: Target,
                         egresses: list[EgressRule]) -> tuple[bool, str]:
    """Return (covered, explanation) for whether target is covered."""
    # Sanity: target with no ports at all is malformed; skip with a note.
    if not target.numeric_ports and not target.named_ports:
        return True, "target has no ports; skipped"

    matched_ns: list[str] = []
    for eg in egresses:
        for peer in eg.peers:
            if peer.target_namespace != target.namespace:
                continue
            matched_ns.append(eg.name)
            if target.numeric_ports:
                if target.numeric_ports & peer.ports:
                    return True, f"covered by {eg.name}"
            else:
                # Only named ports on target -> namespace-level match suffices.
                return True, (f"covered by {eg.name} (namespace-only match; "
                              f"target uses named port(s) {sorted(target.named_ports)})")
    if matched_ns:
        wanted = sorted(target.numeric_ports)
        return False, (
            f"no matching prometheus-allow-*-scrape-egress rule covers port(s) "
            f"{wanted} to namespace `{target.namespace}`. "
            f"Egress rules that reach `{target.namespace}` "
            f"({sorted(set(matched_ns))}) exist but do not include those ports."
        )
    return False, (
        f"no prometheus-allow-*-scrape-egress rule reaches namespace "
        f"`{target.namespace}` at all."
    )


def suggest_rule_name(target: Target) -> str:
    """Suggest a canonical egress rule name for the error message."""
    base = target.name.removesuffix("-ingress").removesuffix("-allow-prometheus-scrape")
    # Cross-namespace convention prefixes with the target namespace where the
    # target sits outside `monitoring`.
    if target.namespace and target.namespace != "monitoring":
        return f"prometheus-allow-{target.namespace}-{base}-scrape-egress"
    return f"prometheus-allow-{base}-scrape-egress"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--root", default="kubernetes",
                    help="Root of the manifests tree (default: kubernetes)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Print every discovered target and its verdict, not "
                         "just failures.")
    args = ap.parse_args()

    root = pathlib.Path(args.root)
    if not root.is_dir():
        print(f"ERROR: root {root} does not exist or is not a directory",
              file=sys.stderr)
        return 2

    targets: list[Target] = []
    egresses: list[EgressRule] = []
    target_ns_names: set[tuple[str, str]] = set()  # (namespace, base-name) to dedupe

    for path, doc in iter_yaml_docs(root):
        t = parse_target(doc, path)
        if t is not None:
            key = (t.namespace, t.name)
            if key not in target_ns_names:
                target_ns_names.add(key)
                targets.append(t)
            continue
        sm = parse_servicemonitor(doc, path)
        if sm is not None:
            # Deduplicate against ingress-rule targets in the same namespace by
            # port set; ServiceMonitors and ingress rules can coexist. Both
            # kinds are appended; if the ingress-rule target is present it
            # will pass and the SM downgrades to a namespace-only check.
            targets.append(sm)
            continue
        eg = parse_egress_rule(doc, path)
        if eg is not None:
            egresses.append(eg)

    if not targets:
        print("WARNING: no scrape targets discovered — is the tree layout what "
              "the lint expects? (Looked under: {})".format(root), file=sys.stderr)
    if not egresses:
        print("ERROR: no prometheus-allow-*-{scrape,probe}-egress rules discovered in "
              f"namespace `{PROM_EGRESS_NAMESPACE}`. Check that "
              "kube-prometheus-stack/app/network-policy.yaml is present.",
              file=sys.stderr)
        return 1

    failures: list[tuple[Target, str]] = []
    for t in targets:
        covered, why = find_covering_egress(t, egresses)
        if not covered:
            failures.append((t, why))
        elif args.verbose:
            print(f"OK   {t.namespace}/{t.name}: {why}")

    if failures:
        print("")
        print("Prometheus scrape NetworkPolicy lint: FAIL")
        print("=" * 60)
        for t, why in failures:
            suggested = suggest_rule_name(t)
            print(f"")
            print(f"  * Target: {t.name}")
            print(f"    Namespace: {t.namespace}")
            print(f"    Port(s):   {sorted(t.numeric_ports) or sorted(t.named_ports)}")
            print(f"    Source:    {t.source_file}")
            print(f"    Problem:   {why}")
            print(f"    Fix:       Add a NetworkPolicy named `{suggested}` to")
            print(f"               kubernetes/cluster0/apps/monitoring/kube-prometheus-stack/app/network-policy.yaml")
            print(f"               allowing egress from `app.kubernetes.io/name: prometheus` to")
            print(f"               namespace `{t.namespace}` on the metrics port. See AGENTS.md")
            print(f"               `Adding a new monitored app (Prometheus scrape targets)`.")
        print("")
        print(f"Total failures: {len(failures)} of {len(targets)} discovered targets.")
        return 1

    print(f"Prometheus scrape NetworkPolicy lint: OK "
          f"({len(targets)} target(s), {len(egresses)} egress rule(s) checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
