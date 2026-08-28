#!/usr/bin/env python3
"""
lint-httproute-dns-decision.py — enforce an explicit public/private DNS
decision on every HTTPRoute that declares a hostname.

Background
----------
#3518 made public DNS publication fail-closed. external-dns publishes a record
only for an object that carries `dns.home-ops/public: "true"`, enforced by
`--label-filter=dns.home-ops/public=true` in
`kubernetes/cluster0/apps/networking/external-dns/app/helm-release.yaml`.

That removed the silent-public-record failure mode and introduced a quieter
one. Two different states look identical in the file:

  1. The author decided the name stays private.
  2. The author forgot the label.

Both produce a private name, so a reviewer cannot tell them apart. If the
author forgets the label on a name that must be public, the service breaks and
nothing warns anyone. #3519 fixed that by making the decision explicit:

  | Label value                       | Meaning                                |
  |-----------------------------------|----------------------------------------|
  | `dns.home-ops/public: "true"`     | Publish this hostname.                 |
  | `dns.home-ops/public: "false"`    | Keep this hostname private (a decision).|
  | No label (route has a hostname)   | CI fails — the decision is missing.    |

This lint fails a pull request when an HTTPRoute declares a hostname and does
not carry a valid `dns.home-ops/public` label. See AGENTS.md,
"Public DNS is opt-in (external-dns)".

Why render
----------
The verdict reads a static label, so a text scan of the tree would agree with a
render for every route today. Rendering earns its cost for two narrower reasons
(#3519):

  1. A chart can emit a route that has no repository-side `route:` block. A text
     scan cannot see it; a render can.
  2. The no-hostname exemption is only reliable after render — a hostname is
     often templated (`hostnames: ["{{ .Release.Name }}.${SECRET_PUBLIC_DOMAIN}"]`)
     and a route may inherit hostnames from its chart.

So the lint reads the rendered manifests produced by `flux-local build`. It
maps each rendered route back to its authored source file (a raw HTTPRoute
manifest, or a bjw-s app-template HelmRelease `route.<name>` block) so the error
message can name the file and the exact fix.

Rules
-----
  * A route that declares NO hostnames passes (it can never produce a record).
  * A vendored upstream manifest is exempt (see EXEMPT_SOURCE_SUFFIXES).
  * `dns.home-ops/public` must be the string `"true"` or `"false"`. A missing
    label, an unquoted boolean, or any other value fails.

Exit codes: 0 on clean, 1 on any violation, 2 on usage/IO error.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Iterable

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Install with `pip install pyyaml`.",
          file=sys.stderr)
    sys.exit(2)


LABEL_KEY = "dns.home-ops/public"
VALID_VALUES = ("true", "false")

# Vendored upstream manifests. These are install output, not authored routes,
# and must never be flagged. Matched as a path suffix against a route's source
# file. `standard-install.yaml` is the Gateway API upstream install (CRDs +
# admission policy today; it declares no HTTPRoute, but exempt it defensively
# in case a future upstream bundle ships example routes).
EXEMPT_SOURCE_SUFFIXES = (
    "kubernetes/cluster0/apps/networking/gateway-api/app/standard-install.yaml",
)


@dataclass
class SourceEntry:
    """Where a route is authored in the tree."""
    source_file: str
    kind: str  # "raw" | "helmrelease"
    route_keys: list[str] = field(default_factory=list)  # helmrelease only
    exempt: bool = False


def iter_yaml_docs(text: str) -> Iterable[dict]:
    """Yield every mapping document from a multi-doc YAML string."""
    try:
        for doc in yaml.safe_load_all(text):
            if isinstance(doc, dict):
                yield doc
    except yaml.YAMLError:
        return


def build_source_index(root: pathlib.Path) -> dict[tuple[str, str], SourceEntry]:
    """Index (namespace, route-name) -> SourceEntry across the source tree.

    Covers two authoring styles:
      * a raw `HTTPRoute` manifest, keyed by its own metadata;
      * a bjw-s app-template / grafana `HelmRelease` with a `route:` block,
        keyed by (HelmRelease namespace, HelmRelease name). The rendered
        HTTPRoute takes the release name in this repo.
    """
    index: dict[tuple[str, str], SourceEntry] = {}
    for path in sorted(root.rglob("*.yaml")):
        if path.name.endswith(".sops.yaml"):
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(path)
        exempt = any(rel.endswith(sfx) for sfx in EXEMPT_SOURCE_SUFFIXES)
        for doc in iter_yaml_docs(text):
            kind = doc.get("kind")
            meta = doc.get("metadata") or {}
            name = meta.get("name") or ""
            ns = meta.get("namespace") or ""
            if kind == "HTTPRoute" and name:
                index.setdefault((ns, name),
                                 SourceEntry(rel, "raw", exempt=exempt))
            elif kind == "HelmRelease" and name:
                route = ((doc.get("spec") or {}).get("values") or {}).get("route")
                if isinstance(route, dict) and route:
                    index.setdefault((ns, name),
                                     SourceEntry(rel, "helmrelease",
                                                 route_keys=list(route.keys()),
                                                 exempt=exempt))
    return index


@dataclass
class Violation:
    namespace: str
    name: str
    hostnames: list[str]
    reason: str
    source: SourceEntry | None


def classify_label(labels: dict) -> tuple[bool, str]:
    """Return (ok, reason). `reason` is empty when ok."""
    if LABEL_KEY not in labels:
        return False, f"no `{LABEL_KEY}` label"
    value = labels[LABEL_KEY]
    if isinstance(value, bool):
        # PyYAML parsed an unquoted YAML boolean. A label value must be a
        # quoted string; external-dns matches the string "true" only.
        return False, (f"`{LABEL_KEY}` is an unquoted boolean "
                       f"({str(value).lower()}); it must be a quoted string")
    if value in VALID_VALUES:
        return True, ""
    return False, (f"`{LABEL_KEY}` is {value!r}; only \"true\" or \"false\" "
                   f"are allowed")


def fix_hint(v: Violation) -> list[str]:
    """Lines describing exactly how to fix one violation."""
    lines: list[str] = []
    if v.source is None:
        lines.append(f"    Source:    <not found in tree — search for HTTPRoute "
                     f"{v.namespace}/{v.name}>")
    else:
        lines.append(f"    Source:    {v.source.source_file}")
    lines.append("    Decide, then add ONE line. Publish this hostname:")
    lines.append('               dns.home-ops/public: "true"')
    lines.append("    Or keep it private (a decision, not an omission):")
    lines.append('               dns.home-ops/public: "false"')
    if v.source is not None and v.source.kind == "helmrelease":
        key = v.source.route_keys[0] if v.source.route_keys else "<route>"
        lines.append(f"    Put it under `spec.values.route.{key}.labels` in that "
                     f"HelmRelease.")
    else:
        lines.append("    Put it under `metadata.labels` in that manifest.")
    return lines


def lint(rendered_docs: list[dict],
         index: dict[tuple[str, str], SourceEntry],
         verbose: bool) -> list[Violation]:
    violations: list[Violation] = []
    checked = 0
    exempt_vendored = 0
    exempt_nohost = 0
    for doc in rendered_docs:
        if doc.get("kind") != "HTTPRoute":
            continue
        meta = doc.get("metadata") or {}
        name = meta.get("name") or ""
        ns = meta.get("namespace") or ""
        source = index.get((ns, name))
        if source is not None and source.exempt:
            exempt_vendored += 1
            if verbose:
                print(f"SKIP {ns}/{name}: vendored upstream ({source.source_file})")
            continue
        hostnames = (doc.get("spec") or {}).get("hostnames") or []
        if not hostnames:
            exempt_nohost += 1
            if verbose:
                print(f"OK   {ns}/{name}: no hostnames, cannot produce a record")
            continue
        checked += 1
        labels = meta.get("labels") or {}
        ok, reason = classify_label(labels)
        if ok:
            if verbose:
                print(f"OK   {ns}/{name}: {labels[LABEL_KEY]!r} "
                      f"(hostnames {list(hostnames)})")
        else:
            violations.append(Violation(ns, name, list(hostnames), reason, source))

    if verbose:
        print(f"\nEvaluated {checked} route(s) with a hostname; "
              f"exempted {exempt_nohost} with no hostname and "
              f"{exempt_vendored} vendored.")
    return violations


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--rendered", required=True,
                    help="Path to the `flux-local build` output (multi-doc "
                         "YAML). Use '-' to read stdin.")
    ap.add_argument("--root", default="kubernetes",
                    help="Root of the source manifests tree, used to map a "
                         "rendered route back to its authored file "
                         "(default: kubernetes).")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Print every route and its verdict, not just failures.")
    args = ap.parse_args()

    if args.rendered == "-":
        rendered_text = sys.stdin.read()
    else:
        rp = pathlib.Path(args.rendered)
        if not rp.is_file():
            print(f"ERROR: rendered file {rp} does not exist", file=sys.stderr)
            return 2
        rendered_text = rp.read_text()

    root = pathlib.Path(args.root)
    if not root.is_dir():
        print(f"ERROR: root {root} does not exist or is not a directory",
              file=sys.stderr)
        return 2

    rendered_docs = list(iter_yaml_docs(rendered_text))
    route_docs = [d for d in rendered_docs if d.get("kind") == "HTTPRoute"]
    if not route_docs:
        print("ERROR: no HTTPRoute objects found in the rendered manifests. "
              "Did `flux-local build` run and produce output?", file=sys.stderr)
        return 2

    index = build_source_index(root)
    violations = lint(rendered_docs, index, args.verbose)

    if violations:
        print("")
        print("HTTPRoute DNS decision lint: FAIL")
        print("=" * 60)
        for v in violations:
            print("")
            print(f"  * Route:     {v.namespace}/{v.name}")
            print(f"    Hostname:  {', '.join(v.hostnames)}")
            print(f"    Problem:   {v.reason}")
            for line in fix_hint(v):
                print(line)
        print("")
        print(f"Total failures: {len(violations)} of {len(route_docs)} "
              f"rendered HTTPRoute(s).")
        print("Every HTTPRoute that declares a hostname must record an explicit "
              "public/private DNS decision. See AGENTS.md, "
              '"Public DNS is opt-in (external-dns)".')
        return 1

    print(f"HTTPRoute DNS decision lint: OK "
          f"({len(route_docs)} rendered HTTPRoute(s) checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
