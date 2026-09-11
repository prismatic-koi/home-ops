#!/usr/bin/env python3
"""
lint-exposure-tier.py — assert every HTTPRoute's listener binding matches its
declared exposure tier.

Background
----------
This cluster has two live exposure tiers (#3635, #3718, #3739). A route is
private or public because of the listener it binds, NOT because of anything
inside the application:

  | Tier | Reachable from            | Listener      | Service front       |
  |------|---------------------------|---------------|---------------------|
  | 1    | internet, LAN, tailnet    | websecure     | traefik (LB)        |
  | 3    | tailnet only              | websecurets   | traefik-ts (ClusterIP)|

`websecurets` sits behind `traefik-ts`, a ClusterIP Service with no LAN
address, so the ts-web tailnet proxy is the only path to a tier-3 route. The
headscale policy grants exactly one identity to that proxy
(`ben@ -> tag:ts-web`). That listener binding is the whole access control for a
tier-3 route.

Every tier-3 route is unauthenticated (or will be after #3730 removes the last
`forwardauth-authelia` filters, and #3724 deletes authelia). So a single wrong
`sectionName` — `websecurets` changed to `websecure` in one file — puts an
unauthenticated administrator interface on the public internet, with no other
signal that anything changed. `forwardauth-authelia` is today an accidental
backstop against that mistake; this lint is the deliberate replacement, and it
must exist before #3724.

The DNS label is not the control. #3635 records that withdrawing a public DNS
record reduces discoverability, not reachability, because traefik routes by the
HTTP `Host` header. So `lint-httproute-dns-decision.py` (the
`dns.home-ops/public` label) does not cover this; it reads no `parentRefs`.
This lint does.

The tier declaration lives in the repository, on the same object that carries
`dns.home-ops/public`, as the label `exposure.home-ops/tier`. That makes a tier
change a visible, reviewable edit rather than an implicit consequence of a
`sectionName`. See AGENTS.md, "Exposure tiers are a labelled, linted decision".

Rules
-----
  * A route that declares a hostname carries a valid `exposure.home-ops/tier`
    label. A missing label fails, in the same fail-closed shape as #3519.
  * The label value is the quoted string "1" or "3". An unquoted number
    (a YAML int), or any other value, fails — the quoting discipline #3519
    enforces for `dns.home-ops/public`.
  * A tier-1 route declares exactly one `parentRefs` entry, `sectionName`
    `websecure`.
  * A tier-3 route declares exactly one `parentRefs` entry, `sectionName`
    `websecurets`.
  * The `auth/authelia` route is the ONE permitted dual-bind: `websecure` AND
    `websecurets`. It is a named exception, with the reason recorded below.
  * A route that declares NO hostname passes (it can never be reached by a
    `Host` header a client controls; there is nothing to expose).
  * A vendored upstream manifest is exempt (see EXEMPT_SOURCE_SUFFIXES).
  * Fail loudly if the render contains no HTTPRoute at all, so a broken or
    empty render never passes vacuously. #3601 records why this matters.

Why render
----------
The chart decides what renders. A route can come from a bjw-s app-template
HelmRelease `route.<name>` block, where the `sectionName` and the label are set
in the chart values, not in a raw manifest. A text scan cannot see the rendered
`parentRefs`; a render can. This lint reads `flate build` output, the same
render the #3519 and #3597 lints use, and maps each rendered route back to its
authored source file so the error names the file and the exact fix.

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


LABEL_KEY = "exposure.home-ops/tier"

# Tier -> the one listener a route on that tier must bind. #3635, #3718, #3739.
# Tier 2 (LAN, `websecurelan`) was retired and its listener deleted (#3739), so
# it is intentionally absent: a route may not declare tier 2.
TIER_LISTENER = {
    "1": "websecure",
    "3": "websecurets",
}
VALID_VALUES = tuple(TIER_LISTENER.keys())

# The ONE permitted dual-bind. `auth/authelia` binds `websecure` AND
# `websecurets`, and no other route may. The reason (#3720): every route that
# carries the `forwardauth-authelia` filter redirects to the `auth` hostname.
# A tailnet client resolves that name to the ts-web proxy, so the redirect
# target must exist on `websecurets`. A LAN client that is not on the tailnet
# resolves `auth` to TRAEFIK_IP through a blocky pin, so `websecure` must stay.
# authelia sits on the public tier, so it carries tier 1; the exception permits
# the extra `websecurets` bind. Do NOT generalise this into a "two listeners
# are allowed" rule — that would permit the exact public-exposure mistake this
# lint exists to catch. #3724 deletes this route with authelia.
AUTH_EXCEPTION_NS = "auth"
AUTH_EXCEPTION_NAME = "authelia"
AUTH_EXCEPTION_TIER = "1"
AUTH_EXCEPTION_LISTENERS = ("websecure", "websecurets")

# Vendored upstream manifests. These are install output, not authored routes,
# and must never be flagged. Matched as a path suffix against a route's source
# file. Kept identical to the #3519 lint's exemption for the same reason.
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

    Covers two authoring styles, the same two the #3519 lint covers:
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
    expected: str
    source: SourceEntry | None


def section_names(doc: dict) -> list[str]:
    """Return the sectionName of every parentRefs entry, in order.

    A parentRef with no sectionName yields the empty string, which never
    matches a real listener, so it is reported as a violation rather than
    silently ignored.
    """
    spec = doc.get("spec") or {}
    prefs = spec.get("parentRefs") or []
    out: list[str] = []
    for p in prefs:
        if isinstance(p, dict):
            out.append(str(p.get("sectionName") or ""))
    return out


def classify_label(labels: dict) -> tuple[bool, str, str]:
    """Return (ok, value, reason). `reason` is empty when ok."""
    if LABEL_KEY not in labels:
        return False, "", f"no `{LABEL_KEY}` label"
    value = labels[LABEL_KEY]
    if isinstance(value, bool):
        return False, "", (f"`{LABEL_KEY}` is a YAML boolean; it must be a "
                           f"quoted string, one of "
                           f"{', '.join(repr(v) for v in VALID_VALUES)}")
    if isinstance(value, int):
        # PyYAML parsed an unquoted number. A label value must be a quoted
        # string, matching the #3519 quoting discipline.
        return False, "", (f"`{LABEL_KEY}` is an unquoted number ({value}); it "
                           f"must be a quoted string, e.g. "
                           f'`{LABEL_KEY}: "{value}"`')
    if value in VALID_VALUES:
        return True, value, ""
    return False, "", (f"`{LABEL_KEY}` is {value!r}; only "
                       f"{', '.join(repr(v) for v in VALID_VALUES)} are allowed")


def fix_source_line(v: Violation) -> str:
    if v.source is None:
        return (f"    Source:    <not found in tree — search for HTTPRoute "
                f"{v.namespace}/{v.name}>")
    return f"    Source:    {v.source.source_file}"


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
                print(f"OK   {ns}/{name}: no hostnames, nothing to expose")
            continue
        checked += 1
        labels = meta.get("labels") or {}
        sections = section_names(doc)

        # The named auth exception: the ONE permitted dual-bind.
        if ns == AUTH_EXCEPTION_NS and name == AUTH_EXCEPTION_NAME:
            ok, value, reason = classify_label(labels)
            expected = "/".join(AUTH_EXCEPTION_LISTENERS)
            if not ok:
                violations.append(Violation(ns, name, list(hostnames), reason,
                                            expected, source))
                continue
            if value != AUTH_EXCEPTION_TIER:
                violations.append(Violation(
                    ns, name, list(hostnames),
                    f"named auth exception must carry tier "
                    f"{AUTH_EXCEPTION_TIER!r} (public tier), not {value!r}",
                    expected, source))
                continue
            if sorted(sections) != sorted(AUTH_EXCEPTION_LISTENERS):
                violations.append(Violation(
                    ns, name, list(hostnames),
                    f"the auth exception must bind exactly "
                    f"{list(AUTH_EXCEPTION_LISTENERS)}, but binds "
                    f"{sections or '[]'}",
                    expected, source))
                continue
            if verbose:
                print(f"OK   {ns}/{name}: named auth dual-bind exception "
                      f"(tier {value}, {sections})")
            continue

        ok, value, reason = classify_label(labels)
        if not ok:
            violations.append(Violation(ns, name, list(hostnames), reason,
                                        "<tier label required>", source))
            continue

        want = TIER_LISTENER[value]
        if sections != [want]:
            violations.append(Violation(
                ns, name, list(hostnames),
                f"tier {value} requires exactly one parentRefs entry with "
                f"sectionName {want!r}, but binds {sections or '[]'}",
                want, source))
            continue

        if verbose:
            print(f"OK   {ns}/{name}: tier {value} -> {sections}")

    if verbose:
        print(f"\nEvaluated {checked} route(s) with a hostname; "
              f"exempted {exempt_nohost} with no hostname and "
              f"{exempt_vendored} vendored.")
    return violations


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--rendered", required=True,
                    help="Path to the `flate build` output (multi-doc YAML). "
                         "Use '-' to read stdin.")
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
              "Did `flate build` run and produce output? A render with no "
              "HTTPRoute must not pass vacuously.", file=sys.stderr)
        return 2

    index = build_source_index(root)
    violations = lint(rendered_docs, index, args.verbose)

    if violations:
        print("")
        print("Exposure tier lint: FAIL")
        print("=" * 60)
        for v in violations:
            print("")
            print(f"  * Route:     {v.namespace}/{v.name}")
            print(f"    Hostname:  {', '.join(v.hostnames)}")
            print(f"    Problem:   {v.reason}")
            print(f"    Expected:  sectionName {v.expected}")
            print(fix_source_line(v))
            print("    A route's tier is a deliberate, labelled decision. Set "
                  "the tier label")
            print("    and bind the matching listener. Publish (public tier):")
            print(f'               {LABEL_KEY}: "1"   # sectionName: websecure')
            print("    Or keep it tailnet-only (private tier):")
            print(f'               {LABEL_KEY}: "3"   # sectionName: websecurets')
        print("")
        print(f"Total failures: {len(violations)} of {len(route_docs)} "
              f"rendered HTTPRoute(s).")
        print("Every HTTPRoute that declares a hostname must carry a valid "
              "`exposure.home-ops/tier` label whose value matches its listener "
              "binding. See AGENTS.md, "
              '"Exposure tiers are a labelled, linted decision".')
        return 1

    print(f"Exposure tier lint: OK "
          f"({len(route_docs)} rendered HTTPRoute(s) checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
