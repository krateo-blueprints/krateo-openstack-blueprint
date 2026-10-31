#!/usr/bin/env python3
"""Every capability ClusterRole must aggregate to a role that actually exists.

    hack/validate-capability-roles.py [chart-dir]

A capability aggregates into a predefined role via a
`rbac.krateo.io/aggregate-to-<role>=true` label, which the predefined role's
aggregationRule selects on. Nothing validates the `<role>` part: a label naming a role
that does not exist is valid YAML, renders without complaint, and produces a ClusterRole
that is created, listed in the catalog, and **joined to nothing**. The capability looks
present and grants no access, and the only way to notice is for someone to try to use it.

This repo shipped exactly that: `aggregateTo: [viewer, admin]` against predefined roles
named read-only, tenant-reader, auditor, operator, tenant-admin and org-admin -- an
overlap of zero, in four capabilities, for as long as they have existed.

It renders the chart rather than reading values.yaml, because the failure to catch is not
only "a bad role name in values". A template that stops emitting the label at all produces
the same silent outcome, and a values-level check would pass it.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The predefined roles a capability may aggregate into. Adding one here without it
# existing platform-side recreates the bug this script exists to prevent.
PREDEFINED = {
    "read-only",
    "tenant-reader",
    "auditor",
    "operator",
    "tenant-admin",
    "org-admin",
}

LABEL = re.compile(r"^\s*rbac\.krateo\.io/aggregate-to-([a-z0-9][a-z0-9-]*):\s*\"?true\"?\s*$")


def render(chart: Path, workdir: Path) -> str:
    """Render with the version stamped, as the in-tree Chart.yaml carries a placeholder."""
    staged = workdir / "chart"
    shutil.copytree(chart, staged)
    cm = staged / "Chart.yaml"
    cm.write_text(cm.read_text().replace("CHART_VERSION", "0.0.0-validate"))
    toolkit = chart.parent.parent.parent / "charts" / "helm-toolkit"
    if toolkit.is_dir():
        (staged / "charts").mkdir(exist_ok=True)
        shutil.copytree(toolkit, staged / "charts" / "helm-toolkit")
    r = subprocess.run(
        ["helm", "template", "validate", str(staged), "--set", "chartVersion=0.0.0-validate"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        sys.exit(f"helm template failed; cannot validate capability roles:\n{r.stderr[:2000]}")
    return r.stdout


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    chart = root / "blueprints" / "openstack" / "chart"
    if not chart.is_dir():
        sys.exit(f"no chart at {chart}")

    with tempfile.TemporaryDirectory() as td:
        rendered = render(chart, Path(td))

    roles, bad, seen_capability_role = [], [], False
    for doc in rendered.split("\n---"):
        if "kind: ClusterRole" not in doc or "krateo.io/capability:" not in doc:
            continue
        seen_capability_role = True
        name = next((l.split(":", 1)[1].strip() for l in doc.splitlines()
                     if l.strip().startswith("name:")), "<unnamed>")
        found = [m.group(1) for m in (LABEL.match(l) for l in doc.splitlines()) if m]
        roles.append((name, found))
        if not found:
            bad.append((name, "aggregates to nothing: no aggregate-to label at all"))
        for r in found:
            if r not in PREDEFINED:
                bad.append((name, f"aggregates to '{r}', which is not a predefined role"))

    if not seen_capability_role:
        sys.exit("no capability ClusterRoles rendered at all -- the check could not look, "
                 "which is a failed measurement, not a pass")

    for name, found in roles:
        print(f"  {name}: {', '.join(found) if found else '(none)'}")

    if bad:
        print()
        print(f"INVALID CAPABILITY AGGREGATION: {len(bad)}")
        for name, why in bad:
            print(f"  {name}: {why}")
        print()
        print("A label naming a role that does not exist renders fine and joins nothing, so the")
        print("capability appears in the catalog and grants no access. Allowed roles:")
        print("  " + ", ".join(sorted(PREDEFINED)))
        return 1

    print(f"\nall {len(roles)} capability roles aggregate to predefined roles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
