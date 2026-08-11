#!/usr/bin/env python3
"""Regenerate the umbrella's componentValues typing in
blueprints/openstack/chart/values.schema.json.

For every component in the umbrella's chart/values.yaml, embed that component chart's
values.schema.json under componentValues.properties.<name>, so `componentValues.<name>`
is strictly typed against the component's REAL Composition schema (a component chart's
values ARE its Composition spec). `required` is stripped recursively because
componentValues is a PARTIAL override (a deep-merge), not a full values document.

Adapted from krateo-blueprints/installer hack/gen-componentvalues-schema.py. Difference: this
repo VENDORS the component charts, so each schema is read from blueprints/<name>/chart/
(authoritative, offline) rather than `helm pull`ed; the pinned version is the single
umbrella `chartVersion`. Re-run on every release / whenever a component schema changes.

Usage:  python3 hack/gen-componentvalues-schema.py
Requires: pyyaml.
"""
import json
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHART = os.path.join(ROOT, "blueprints/openstack/chart")


def strip_required(node):
    """Recursively drop the JSON-Schema `required` keyword (partial overrides need none)."""
    if isinstance(node, dict):
        node.pop("required", None)
        for v in node.values():
            strip_required(v)
    elif isinstance(node, list):
        for v in node:
            strip_required(v)
    return node


def main():
    vals = yaml.safe_load(open(os.path.join(CHART, "values.yaml")))
    ver = str(vals["chartVersion"])
    props = {}
    for c in vals["components"]:
        name = c["name"]
        sp = os.path.join(ROOT, f"blueprints/{name}/chart/values.schema.json")
        if not os.path.exists(sp):
            print(f"  WARN {name}: no values.schema.json in chart", file=sys.stderr)
            continue
        s = json.load(open(sp))
        s.pop("$schema", None)
        s.pop("title", None)
        strip_required(s)
        s["description"] = f"Overrides for the {name} Composition (chart {ver}), deep-merged into its spec."
        props[name] = s
        print(f"  typed {name} ({ver})")

    sp = os.path.join(CHART, "values.schema.json")
    schema = json.load(open(sp))
    schema["properties"]["componentValues"] = {
        "type": ["object", "null"],
        "title": "Per-component spec overrides",
        "description": (
            "Per-component Composition spec overrides, STRICTLY TYPED against each "
            "component's chart schema (regenerated per release by "
            "hack/gen-componentvalues-schema.py). The value at componentValues.<name> "
            "becomes that component's Composition spec; components without an entry use "
            "chart defaults."
        ),
        "additionalProperties": False,
        "properties": props,
    }
    json.dump(schema, open(sp, "w"), indent=2)
    open(sp, "a").write("\n")
    print(f"\nwrote componentValues typing for {len(props)} components -> {sp}")


if __name__ == "__main__":
    main()
