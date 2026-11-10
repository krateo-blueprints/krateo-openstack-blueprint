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
- `icon` is on a host this organisation controls: this repository's own raw path naming a file
  that exists under docs/icons/, or the organisation's brand logo. Third-party URLs rot without
  anyone noticing: at 0.3.45, 23 of 37 tiles pointed at an openstack.org directory that had moved
  (22 answered 404, and freezer redirected to an HTML page with status 200). Twelve more used the
  pre-rename `krateoplatformops` org and rendered only through GitHub's redirect. This check is
  offline and per pull request. The release fetches every icon before the GHCR push, the network
  half;
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
# Hosts this organisation controls. A chart's own icon lives in this repository and is served
# from main; the release tags merged main, so the file exists when the release fetches it.
ICON_REPO_PREFIX = "https://raw.githubusercontent.com/krateo-blueprints/krateo-openstack-blueprint/main/docs/icons/"
ICON_ORG_LOGO = "https://raw.githubusercontent.com/krateo-platformops/.github/main/brand/logo.svg"
ICON_EXTENSIONS = (".png", ".svg")


def icon_problem(bp, icon):
    icon = str(icon or "")
    if icon == ICON_ORG_LOGO:
        return None
    if icon.startswith(ICON_REPO_PREFIX):
        rel = icon[len(ICON_REPO_PREFIX):]
        if "/" in rel or not rel.endswith(ICON_EXTENSIONS):
            return f"{bp}: icon {icon!r} must name a .png or .svg directly under docs/icons/"
        if not os.path.isfile(os.path.join(ROOT, "docs", "icons", rel)):
            return f"{bp}: icon names docs/icons/{rel}, which does not exist in this repository"
        return None
    return (f"{bp}: icon {icon!r} is not on a host this organisation controls; use "
            f"{ICON_REPO_PREFIX}<file> (committed under docs/icons/) or {ICON_ORG_LOGO}")

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
        p = icon_problem(bp, meta.get("icon"))
        if p:
            problems.append(p)
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
