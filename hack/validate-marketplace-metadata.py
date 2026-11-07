#!/usr/bin/env python3
"""Every blueprint chart must be one the Marketplace index will accept.

The release publishes each chart to GHCR and then to the helm index at krateo-blueprints/charts,
which is what the portal's Marketplace lists and installs from. That index's gate refuses a chart
without values.schema.json, without a compositiondefinition.yaml in its archive, or without an
absolute https icon. The release puts the CompositionDefinition in the archive. The other two
come from the tree, and a refusal at release time arrives after GHCR already holds the version,
so the index and GHCR would disagree, which is the state that had the Marketplace installing
0.2.0 for weeks. This gate moves that refusal to the pull request.

It also requires `keywords`, which the Marketplace shows as the tile's tags, and `sources:` listing
this repository, which the installer uses to trace a pinned chart to the commit that built it.
The publisher refuses both (krateo-blueprints/charts PUBLISHING.md rule 3).

For each blueprints/<name>/chart/Chart.yaml:

- `name` equals the directory name, because the index is keyed by it;
- `icon` is an absolute https URL;
- `keywords` is a non-empty list;
- `sources` lists https://github.com/krateo-blueprints/krateo-openstack-blueprint;
- chart/values.schema.json exists;
- blueprints/<name>/compositiondefinition.yaml exists, for the release to put in the archive.

Exits non-zero when it finds no charts, because a gate that inspected nothing has said nothing.
"""
import glob
import os
import sys

try:
    import yaml
except ImportError:
    sys.exit("ERROR: PyYAML is required")

SOURCE_REPO = "https://github.com/krateo-blueprints/krateo-openstack-blueprint"

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 \
    else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    charts = sorted(glob.glob(os.path.join(ROOT, "blueprints", "*", "chart", "Chart.yaml")))
    if not charts:
        sys.exit(f"ERROR: no blueprints/*/chart/Chart.yaml under {ROOT}")

    problems = []
    for cf in charts:
        chart_dir = os.path.dirname(cf)
        bp_dir = os.path.dirname(chart_dir)
        bp = os.path.basename(bp_dir)
        with open(cf) as f:
            meta = yaml.safe_load(f) or {}
        if meta.get("name") != bp:
            problems.append(f"{bp}: Chart.yaml name is {meta.get('name')!r}, not {bp!r}")
        icon = meta.get("icon") or ""
        if not str(icon).startswith("https://"):
            problems.append(f"{bp}: icon must be an absolute https URL, got {icon!r}")
        kw = meta.get("keywords")
        if not isinstance(kw, list) or not kw:
            problems.append(f"{bp}: keywords must be a non-empty list (the Marketplace's tags)")
        if SOURCE_REPO not in (meta.get("sources") or []):
            problems.append(f"{bp}: sources must list {SOURCE_REPO}, the repository that builds it")
        if not os.path.isfile(os.path.join(chart_dir, "values.schema.json")):
            problems.append(f"{bp}: chart/values.schema.json is missing")
        if not os.path.isfile(os.path.join(bp_dir, "compositiondefinition.yaml")):
            problems.append(f"{bp}: compositiondefinition.yaml is missing")

    if problems:
        print("The Marketplace index would refuse or mislabel these charts:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(1)
    print(f"OK: {len(charts)} blueprint charts are acceptable to the Marketplace index")


if __name__ == "__main__":
    main()
