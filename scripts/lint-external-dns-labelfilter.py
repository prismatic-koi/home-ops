#!/usr/bin/env python3
"""
lint-external-dns-labelfilter.py — assert the rendered external-dns Deployment
still carries exactly one `--label-filter=dns.home-ops/public=true` argument.

Background
----------
Public DNS publication is fail-closed (#3518). external-dns publishes a record
only for an object that carries `dns.home-ops/public: "true"`. The control that
enforces this is the `--label-filter` argument on the external-dns Deployment.

#3525 / PR #3595 moved that filter from `extraArgs` (a passthrough list the
chart appends verbatim) to the chart's first-class `labelFilter` value in
`kubernetes/cluster0/apps/networking/external-dns/app/helm-release.yaml`. That
was the right change for legibility, but it removed a property nobody had
named: `extraArgs` was rename-proof. `labelFilter` is a named key the chart
must recognise, and chart 1.21.1 sets `additionalProperties: true`, so an
unrecognised key is NOT rejected — it renders silently WITHOUT the flag.

Proven, not assumed. The PR #3595 negative control: renaming the value to
`labelFilterr` renders zero `--label-filter` occurrences, with no error.

Why this is dangerous, not merely theoretical (#3597):
  1. A patch chart bump for external-dns auto-merges with no human review —
     `.github/renovate/automerge.json5` carves external-dns out for
     major/minor only.
  2. `policy: sync` is live, so a lost filter does not fail safe: external-dns
     would see every HTTPRoute and publish records for the ones now withheld.
  3. A lost filter would republish `search.${SECRET_PUBLIC_DOMAIN}` and
     silently reverse the #3555 withdrawal (`home/searxng` keeps no
     `external-dns.alpha.kubernetes.io/controller: none` fallback).

This lint fails a pull request when the rendered external-dns Deployment does
not carry exactly one `--label-filter` argument with the exact expected value.
See AGENTS.md, "Public DNS is opt-in (external-dns)".

Why render
----------
The chart decides whether the flag renders. A text scan of the HelmRelease
cannot see a chart that drops an unrecognised key, which is exactly the failure
mode here. So the lint reads the rendered manifests produced by
`flux-local build`, the same source the #3519 HTTPRoute DNS decision lint uses.

Not vacuous
-----------
A check that greps rendered output for a bad match reports success when the
render contains no external-dns Deployment at all. That is the failure mode
most likely to make this guardrail useless at the moment it is needed. So the
lint asserts PRESENCE first: it finds the external-dns Deployment, fails loudly
if it is absent, and only then asserts exactly one `--label-filter` with the
exact value. Absence of the Deployment is a hard failure, never a pass.

Exit codes: 0 on clean, 1 on any violation, 2 on usage/IO error.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from typing import Iterable

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Install with `pip install pyyaml`.",
          file=sys.stderr)
    sys.exit(2)


# The rendered external-dns Deployment. It must exist, and its container args
# must carry exactly one --label-filter with EXPECTED_VALUE.
DEPLOY_NAMESPACE = "networking"
DEPLOY_NAME = "external-dns"
LABEL_FILTER_FLAG = "--label-filter"
EXPECTED_VALUE = "dns.home-ops/public=true"

# The file an author edits to fix a violation, named in every failure message.
HELM_RELEASE_FILE = (
    "kubernetes/cluster0/apps/networking/external-dns/app/helm-release.yaml"
)


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


def extract_containers(deploy: dict) -> list[dict]:
    """Return the pod-spec containers of a Deployment."""
    spec = deploy.get("spec") or {}
    template = spec.get("template") or {}
    pod_spec = template.get("spec") or {}
    containers = pod_spec.get("containers") or []
    return [c for c in containers if isinstance(c, dict)]


def label_filter_values(args: list) -> list[str]:
    """Extract every --label-filter value from a container `args` list.

    Handles both the `--label-filter=VALUE` joined form (what the chart
    renders) and the split `--label-filter`, `VALUE` form, so a future chart
    change to either idiom is still verified.
    """
    values: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if not isinstance(arg, str):
            i += 1
            continue
        if arg == LABEL_FILTER_FLAG:
            # Split form: value is the next token, if present.
            if i + 1 < len(args) and isinstance(args[i + 1], str):
                values.append(args[i + 1])
                i += 2
                continue
            values.append("")  # flag with no value — a defect we surface
        elif arg.startswith(LABEL_FILTER_FLAG + "="):
            values.append(arg[len(LABEL_FILTER_FLAG) + 1:])
        i += 1
    return values


def fail(lines: list[str]) -> int:
    print("")
    print("external-dns label-filter lint: FAIL")
    print("=" * 60)
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--rendered", required=True,
                    help="Path to the `flux-local build` output (multi-doc "
                         "YAML). Use '-' to read stdin.")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Print the resolved argument on success, not just "
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

    # Presence assertion FIRST — a render that produced no external-dns
    # Deployment is a hard failure, never a vacuous pass (#3597).
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

    containers = extract_containers(deploy)
    values: list[str] = []
    for c in containers:
        values.extend(label_filter_values(c.get("args") or []))

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

    print(f"external-dns label-filter lint: OK "
          f"({DEPLOY_NAMESPACE}/{DEPLOY_NAME} carries exactly one "
          f"{LABEL_FILTER_FLAG}={EXPECTED_VALUE}).")
    if args.verbose:
        print(f"  Deployment found, 1 container arg matched: "
              f"{LABEL_FILTER_FLAG}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
