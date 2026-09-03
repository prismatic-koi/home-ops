#!/usr/bin/env python3
"""
lint-external-dns.py — assert two invariants of the rendered external-dns
objects, both read from the same `flux-local build` output.

Check 1 — label-filter (#3597)
------------------------------
The rendered external-dns Deployment must carry exactly one
`--label-filter=dns.home-ops/public=true` argument.

Public DNS publication is fail-closed (#3518). external-dns publishes a record
only for an object that carries `dns.home-ops/public: "true"`. The control that
enforces this is the `--label-filter` argument on the Deployment.

#3525 / PR #3595 moved that filter from `extraArgs` (a passthrough list the
chart appends verbatim) to the chart's first-class `labelFilter` value in
`kubernetes/cluster0/apps/networking/external-dns/app/helm-release.yaml`. That
removed a property nobody had named: `extraArgs` was rename-proof. `labelFilter`
is a named key the chart must recognise, and chart 1.21.1 sets
`additionalProperties: true`, so an unrecognised key is NOT rejected — it
renders silently WITHOUT the flag. A patch chart bump auto-merges with no human
review, and `policy: sync` is live, so a lost filter fails open: external-dns
would see every HTTPRoute and publish records for the ones now withheld.

Check 2 — source-to-RBAC (#3601)
--------------------------------
For every `--source=<name>` argument on the rendered Deployment, the rendered
ClusterRole must grant the RBAC that source needs — exactly once.

PR #3600 moved `gateway-httproute` out of `extraArgs` and into the chart's
`sources:` value, because the chart generates RBAC from `.Values.sources` and
NOT from `extraArgs`. A `--source=` argument in `extraArgs` enables the source
but generates no RBAC; the chart accepts both forms without an error. So the
half-done move — source left in `extraArgs`, the manual RBAC block removed —
renders a Deployment argument set byte-identical to the correct change, with a
ClusterRole that grants nothing for that source. external-dns then cannot list
the objects the source names, so the source fails to start and the Deployment
crash-loops.

This is fail-CLOSED, not fail-open. A crash-looping external-dns publishes
nothing and withdraws nothing, so it does NOT repeat the #3522 public-DNS
outage. The cost is a broken deploy and the time spent diagnosing it. Check 1
above guards the fail-open direction; this check guards the crash-loop.

The source-to-resource mapping is taken from chart 1.21.1
`templates/clusterrole.yaml` and the `external-dns.hasGatewaySources` helper in
`templates/_helpers.tpl`. Every gateway source additionally needs the shared
`gateways` and `namespaces` rules the helper emits.

Why render both checks
----------------------
The chart decides what renders. A text scan of the HelmRelease cannot see a
chart that drops an unrecognised key (check 1) or one that generates no RBAC
for a source declared the wrong way (check 2). Both checks read the rendered
manifests produced by `flux-local build`, the same source the #3519 HTTPRoute
DNS decision lint uses. They share one render because a second full-tree
`flux-local` render on every PR costs real minutes; the Python is near-free.

Not vacuous
-----------
A check that greps rendered output for a bad match reports success when the
render contains no external-dns objects at all — the failure mode most likely
to make a guardrail useless at the moment it is needed. So each check asserts
PRESENCE first: check 1 fails loudly if the Deployment is absent; check 2 fails
loudly if the Deployment or the ClusterRole is absent. Absence is a hard
failure, never a pass.

Exit codes: 0 when both checks pass, 1 on any violation, 2 on usage/IO error.
See AGENTS.md, "Public DNS is opt-in (external-dns)".
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from collections import Counter
from typing import Iterable

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Install with `pip install pyyaml`.",
          file=sys.stderr)
    sys.exit(2)


# The rendered external-dns Deployment and ClusterRole. Both must exist.
DEPLOY_NAMESPACE = "networking"
DEPLOY_NAME = "external-dns"
CLUSTERROLE_NAME = "external-dns"
LABEL_FILTER_FLAG = "--label-filter"
SOURCE_FLAG = "--source"
EXPECTED_VALUE = "dns.home-ops/public=true"

# The file an author edits to fix a violation, named in every failure message.
HELM_RELEASE_FILE = (
    "kubernetes/cluster0/apps/networking/external-dns/app/helm-release.yaml"
)

# Source-to-RBAC mapping, from chart 1.21.1 templates/clusterrole.yaml and the
# `external-dns.hasGatewaySources` helper in templates/_helpers.tpl. Each source
# maps to the (apiGroup, resource) pairs its rendered ClusterRole must grant.
# apiGroup "" is the core group. Only the sources this repository can plausibly
# enable are listed; an unmapped source is reported as "cannot verify", never a
# silent pass.
#
# Every gateway-* source additionally needs the shared rules the
# hasGatewaySources helper emits: `gateways` (gateway.networking.k8s.io) and
# `namespaces` (core), because the cluster is not namespaced.
_GATEWAY_SHARED = [
    ("gateway.networking.k8s.io", "gateways"),
    ("", "namespaces"),
]
SOURCE_RBAC: dict[str, list[tuple[str, str]]] = {
    "ingress": [("networking.k8s.io", "ingresses")],
    "crd": [("externaldns.k8s.io", "dnsendpoints")],
    "gateway-httproute": [("gateway.networking.k8s.io", "httproutes")]
    + _GATEWAY_SHARED,
    "gateway-grpcroute": [("gateway.networking.k8s.io", "grpcroutes")]
    + _GATEWAY_SHARED,
    "gateway-tlsroute": [("gateway.networking.k8s.io", "tlsroutes")]
    + _GATEWAY_SHARED,
    "gateway-tcproute": [("gateway.networking.k8s.io", "tcproutes")]
    + _GATEWAY_SHARED,
    "gateway-udproute": [("gateway.networking.k8s.io", "udproutes")]
    + _GATEWAY_SHARED,
}


def iter_yaml_docs(text: str) -> Iterable[dict]:
    """Yield every mapping document from a multi-doc YAML string."""
    try:
        for doc in yaml.safe_load_all(text):
            if isinstance(doc, dict):
                yield doc
    except yaml.YAMLError:
        return


def find_deployment(docs: list[dict]) -> dict | None:
    """Return the external-dns Deployment, or None if it is absent."""
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        meta = doc.get("metadata") or {}
        if meta.get("name") == DEPLOY_NAME and \
                meta.get("namespace") == DEPLOY_NAMESPACE:
            return doc
    return None


def find_clusterrole(docs: list[dict]) -> dict | None:
    """Return the external-dns ClusterRole, or None if it is absent."""
    for doc in docs:
        if doc.get("kind") != "ClusterRole":
            continue
        meta = doc.get("metadata") or {}
        if meta.get("name") == CLUSTERROLE_NAME:
            return doc
    return None


def extract_containers(deploy: dict) -> list[dict]:
    """Return the pod-spec containers of a Deployment."""
    spec = deploy.get("spec") or {}
    template = spec.get("template") or {}
    pod_spec = template.get("spec") or {}
    containers = pod_spec.get("containers") or []
    return [c for c in containers if isinstance(c, dict)]


def _flag_values(args: list, flag: str) -> list[str]:
    """Extract every value of `flag` from a container `args` list.

    Handles both the `--flag=VALUE` joined form (what the chart renders) and the
    split `--flag`, `VALUE` form, so a future chart change to either idiom is
    still verified.
    """
    values: list[str] = []
    i = 0
    prefix = flag + "="
    while i < len(args):
        arg = args[i]
        if not isinstance(arg, str):
            i += 1
            continue
        if arg == flag:
            if i + 1 < len(args) and isinstance(args[i + 1], str):
                values.append(args[i + 1])
                i += 2
                continue
            values.append("")  # flag with no value — a defect we surface
        elif arg.startswith(prefix):
            values.append(arg[len(prefix):])
        i += 1
    return values


def deployment_flag_values(deploy: dict, flag: str) -> list[str]:
    """Collect every value of `flag` across all containers of a Deployment."""
    values: list[str] = []
    for c in extract_containers(deploy):
        values.extend(_flag_values(c.get("args") or [], flag))
    return values


def grant_counts(clusterrole: dict) -> Counter[tuple[str, str]]:
    """Count each (apiGroup, resource) grant across every rule.

    A rule lists apiGroups and resources; it grants the cartesian product of the
    two. Counting the product lets the caller detect both a missing grant (count
    0) and a duplicated grant (count > 1), for example when a stale manual
    `rbac.additionalPermissions` block overlaps a chart-generated rule.
    """
    counts: Counter[tuple[str, str]] = Counter()
    for rule in clusterrole.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        groups = rule.get("apiGroups") or []
        resources = rule.get("resources") or []
        for group in groups:
            for resource in resources:
                counts[(str(group), str(resource))] += 1
    return counts


def print_fail_header(title: str) -> None:
    print("")
    print(f"{title}: FAIL")
    print("=" * 60)


# --------------------------------------------------------------------------
# Check 1 — label-filter (#3597)
# --------------------------------------------------------------------------
def check_label_filter(docs: list[dict], verbose: bool) -> int:
    title = "external-dns label-filter lint"

    def fail(lines: list[str]) -> int:
        print_fail_header(title)
        for line in lines:
            print(line)
        print("")
        print(f"    File:      {HELM_RELEASE_FILE}")
        print(f"    Required:  the rendered Deployment {DEPLOY_NAMESPACE}/"
              f"{DEPLOY_NAME} must carry exactly one")
        print(f"               {LABEL_FILTER_FLAG}={EXPECTED_VALUE}")
        print("    Set the chart value:")
        print(f"               labelFilter: {EXPECTED_VALUE}")
        print("    The filter is the sole fail-closed control on public DNS "
              "publication.")
        print('    See AGENTS.md, "Public DNS is opt-in (external-dns)".')
        return 1

    deploy = find_deployment(docs)
    if deploy is None:
        return fail([
            f"  * The external-dns Deployment {DEPLOY_NAMESPACE}/{DEPLOY_NAME} "
            f"is ABSENT from the rendered output.",
            "    The render produced no Deployment to check, so the "
            "label-filter",
            "    control cannot be verified. This is a hard failure, not a "
            "pass:",
            "    a silent render regression must not report success.",
        ])

    values = deployment_flag_values(deploy, LABEL_FILTER_FLAG)

    if len(values) == 0:
        return fail([
            f"  * The external-dns Deployment {DEPLOY_NAMESPACE}/{DEPLOY_NAME} "
            f"carries NO {LABEL_FILTER_FLAG} argument.",
            "    The public-DNS opt-in filter is missing. external-dns would "
            "see every",
            "    object and publish records for hostnames that are currently "
            "withheld.",
            "    A dropped chart key renders silently without the flag (chart "
            "1.21.1",
            "    sets additionalProperties: true), which is exactly this "
            "failure.",
        ])

    if len(values) > 1:
        rendered = ", ".join(f"{LABEL_FILTER_FLAG}={v!r}" for v in values)
        return fail([
            f"  * The external-dns Deployment {DEPLOY_NAMESPACE}/{DEPLOY_NAME} "
            f"carries {len(values)} {LABEL_FILTER_FLAG} arguments.",
            f"    Found: {rendered}",
            "    There must be exactly one. Multiple filters change which "
            "objects",
            "    external-dns publishes in ways no reviewer can reason about "
            "at a glance.",
        ])

    value = values[0]
    if value != EXPECTED_VALUE:
        return fail([
            f"  * The external-dns Deployment {DEPLOY_NAMESPACE}/{DEPLOY_NAME} "
            f"carries {LABEL_FILTER_FLAG}={value!r},",
            f"    but the only allowed value is {EXPECTED_VALUE!r}.",
            "    A different selector changes which objects external-dns "
            "publishes",
            "    and can republish a hostname that was deliberately withheld.",
        ])

    print(f"{title}: OK ({DEPLOY_NAMESPACE}/{DEPLOY_NAME} carries exactly one "
          f"{LABEL_FILTER_FLAG}={EXPECTED_VALUE}).")
    if verbose:
        print(f"  Deployment found, 1 container arg matched: "
              f"{LABEL_FILTER_FLAG}={value}")
    return 0


# --------------------------------------------------------------------------
# Check 2 — source-to-RBAC (#3601)
# --------------------------------------------------------------------------
def check_source_rbac(docs: list[dict], verbose: bool) -> int:
    title = "external-dns source-RBAC lint"

    def fail(lines: list[str]) -> int:
        print_fail_header(title)
        for line in lines:
            print(line)
        print("")
        print(f"    File:      {HELM_RELEASE_FILE}")
        print("    Declare the source in the chart `sources:` value, NOT in "
              "`extraArgs`.")
        print("    The chart generates RBAC from `sources:` only; a "
              "`--source=` argument")
        print("    in `extraArgs` enables the source and grants it no "
              "permission, so")
        print("    external-dns cannot list the objects it names and the "
              "Deployment")
        print("    crash-loops (fail-closed).")
        print('    See AGENTS.md, "Public DNS is opt-in (external-dns)".')
        return 1

    deploy = find_deployment(docs)
    if deploy is None:
        return fail([
            f"  * The external-dns Deployment {DEPLOY_NAMESPACE}/{DEPLOY_NAME} "
            f"is ABSENT from the rendered output.",
            "    There is no Deployment to read the enabled sources from, so "
            "the",
            "    source-to-RBAC invariant cannot be verified. This is a hard "
            "failure,",
            "    not a pass: a silent render regression must not report "
            "success.",
        ])

    sources = deployment_flag_values(deploy, SOURCE_FLAG)
    if not sources:
        return fail([
            f"  * The external-dns Deployment {DEPLOY_NAMESPACE}/{DEPLOY_NAME} "
            f"carries NO {SOURCE_FLAG} argument.",
            "    external-dns needs at least one source. A render with zero "
            "sources is",
            "    a regression, not a valid state. This is a hard failure, not "
            "a pass.",
        ])

    clusterrole = find_clusterrole(docs)
    if clusterrole is None:
        return fail([
            f"  * The external-dns ClusterRole {CLUSTERROLE_NAME!r} is ABSENT "
            f"from the rendered output.",
            f"    The Deployment enables source(s) {sorted(sources)}, but "
            f"there is no",
            "    ClusterRole to grant them permission. This is a hard "
            "failure, not a",
            "    pass: a render that dropped the RBAC must not report success.",
        ])

    counts = grant_counts(clusterrole)

    # Build the ordered, deduplicated set of required (apiGroup, resource)
    # pairs across every enabled, mapped source. A pair needed by more than one
    # source is still required exactly once — the chart emits the shared gateway
    # rules a single time.
    required: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    source_of: dict[tuple[str, str], list[str]] = {}
    unmapped: list[str] = []
    for src in sources:
        pairs = SOURCE_RBAC.get(src)
        if pairs is None:
            if src not in unmapped:
                unmapped.append(src)
            continue
        for pair in pairs:
            source_of.setdefault(pair, [])
            if src not in source_of[pair]:
                source_of[pair].append(src)
            if pair not in seen:
                seen.add(pair)
                required.append(pair)

    problems: list[str] = []
    ok_lines: list[str] = []
    for group, resource in required:
        count = counts.get((group, resource), 0)
        group_label = group or "core"
        for_sources = ", ".join(source_of[(group, resource)])
        if count == 0:
            problems.append(
                f"  * Source `{for_sources}` needs `{resource}` in apiGroup "
                f"`{group_label}`,")
            problems.append(
                f"    but the ClusterRole {CLUSTERROLE_NAME!r} does NOT grant "
                f"it (found 0 times).")
        elif count > 1:
            problems.append(
                f"  * Source `{for_sources}` needs `{resource}` in apiGroup "
                f"`{group_label}` exactly once,")
            problems.append(
                f"    but the ClusterRole {CLUSTERROLE_NAME!r} grants it "
                f"{count} times (duplicate grant).")
        else:
            ok_lines.append(
                f"  OK  `{resource}` in `{group_label}` x1 (for {for_sources})")

    if problems:
        return fail(problems)

    if unmapped:
        print(f"{title}: NOTE — cannot verify RBAC for source(s) "
              f"{unmapped}; no mapping in SOURCE_RBAC. Add the mapping from "
              f"chart templates/clusterrole.yaml if this source is intended.")

    print(f"{title}: OK ({DEPLOY_NAMESPACE}/{DEPLOY_NAME} enables "
          f"{len(sources)} source(s); every required grant present exactly "
          f"once).")
    if verbose:
        for line in ok_lines:
            print(line)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--rendered", required=True,
                    help="Path to the `flux-local build` output (multi-doc "
                         "YAML). Use '-' to read stdin.")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Print resolved values on success, not just "
                         "failures.")
    args = ap.parse_args()

    if args.rendered == "-":
        rendered_text = sys.stdin.read()
    else:
        rp = pathlib.Path(args.rendered)
        if not rp.is_file():
            print(f"ERROR: rendered file {rp} does not exist", file=sys.stderr)
            return 2
        rendered_text = rp.read_text()

    docs = list(iter_yaml_docs(rendered_text))
    if not docs:
        print("ERROR: no objects found in the rendered manifests. Did "
              "`flux-local build` run and produce output?", file=sys.stderr)
        return 2

    # Run both checks. Each prints its own section. The pull request fails if
    # either check fails.
    rc_label = check_label_filter(docs, args.verbose)
    rc_rbac = check_source_rbac(docs, args.verbose)
    return 1 if (rc_label or rc_rbac) else 0


if __name__ == "__main__":
    sys.exit(main())
