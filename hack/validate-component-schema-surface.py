#!/usr/bin/env python3
"""A component override that the schema does not expose is silently pruned (#209).

Each component's values.schema.json becomes its Composition CRD (core-provider crdgen), and the
umbrella embeds those schemas as componentValues.<component> (hack/gen-componentvalues-schema.py),
which becomes the Openstack CRD. Kubernetes prunes every field a structural schema does not
declare, at BOTH stages. Measured before this gate: nova's schema declared only `images`, so
`componentValues.nova.pod.useHostNetwork.novncproxy`, `componentValues.keystone.conf.keystone.
DEFAULT.debug` and `componentValues.glance.manifests.job_clean` were all dropped with nothing but
a client-side "unknown field" warning. All 36 component schemas were narrower than their chart.

A curated subset is the defect: a first version of this fix exposed six chosen keys and still
dropped `labels` (node selectors, which decide placement) in 35 charts, plus `bootstrap`,
`extraObjects`, `jobs`, `secrets` and others. So the rule is EVERY top-level key.

This gate fails when, for any component chart:
  1. its values.yaml has a top-level key its schema does not declare;
  2. that key, or any object below it, is not open (x-kubernetes-preserve-unknown-fields: true),
     so a deeper key would still be pruned. An object an author closes on purpose with
     additionalProperties: false is respected; junctor members and $refs are not inspected;
  3. a boolean switch in values.yaml `manifests` is not declared as a boolean;
  4. a top-level scalar is declared with a type its own default in values.yaml does not have;
and when the umbrella's committed values.schema.json is not byte-identical to what
hack/gen-componentvalues-schema.py produces from the component schemas in this tree.

It fails when it finds no component chart, so it cannot pass by measuring nothing.

Usage: hack/validate-component-schema-surface.py [repo-root]
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    sys.exit("FAIL: PyYAML is required, and this gate does not pass without measuring")

MARKER = "x-kubernetes-preserve-unknown-fields"
SCALAR = {bool: "boolean", int: "integer", str: "string"}


def declared_types(node):
    t = node.get("type")
    return set(t) if isinstance(t, list) else ({t} if t else set())


def scalar_mismatch(value, node):
    """The chart's own default for a top-level scalar must satisfy the declared type."""
    types = declared_types(node)
    if not types or not types <= {"boolean", "integer", "string", "number", "null"}:
        return None
    if value is None:
        return None if "null" in types or node.get("nullable") else "null"
    want = SCALAR.get(type(value))
    if want and want not in types and not (want == "integer" and "number" in types):
        return want
    return None


def closed_objects(node, path):
    """Yield the path of every object node below `node` that crdgen would close."""
    if not isinstance(node, dict) or "$ref" in node:
        return
    is_obj = node.get("type") == "object" or (node.get("type") is None and "properties" in node)
    if is_obj and node.get("additionalProperties") is not False and node.get(MARKER) is not True:
        yield path
    for k, child in (node.get("properties") or {}).items():
        yield from closed_objects(child, f"{path}.{k}")
    if isinstance(node.get("items"), dict):
        yield from closed_objects(node["items"], f"{path}[]")


def check_chart(chart, name):
    problems = []
    values = yaml.safe_load(open(os.path.join(chart, "values.yaml"))) or {}
    schema = json.load(open(os.path.join(chart, "values.schema.json")))
    props = schema.get("properties") or {}
    for key, value in values.items():
        node = props.get(key)
        if node is None:
            problems.append(f"{name}: values.yaml has `{key}` but the schema does not declare it, so "
                            f"componentValues.{name}.{key} is pruned")
            continue
        bad = scalar_mismatch(value, node)
        if bad:
            problems.append(f"{name}: `{key}` is declared as {node.get('type')!r} but its default in values.yaml "
                            f"is a {bad}")
        # Helm validates the chart's OWN defaults against this schema before it renders anything,
        # empty ones included, so a container's declared type must accept its default. A first
        # version of this rule exempted empty maps; redis ships `extraObjects: {}`, it was declared
        # an array, and every render of redis failed (hack/validate-values-schema.py caught it).
        want = "object" if isinstance(value, dict) else "array" if isinstance(value, list) else None
        if want and want not in declared_types(node):
            problems.append(f"{name}: `{key}` defaults to a{'n' if want == 'object' else 'n'} {want} in values.yaml "
                            f"but is declared with type {node.get('type')!r}, so helm refuses the chart's own defaults")
            continue
        for p in closed_objects(node, key):
            problems.append(f"{name}: `{p}` is a closed object, so an undeclared key below it is pruned "
                            f"(add {MARKER}: true)")
        if key == "manifests" and isinstance(values.get("manifests"), dict):
            mp = node.get("properties") or {}
            for k, v in values["manifests"].items():
                if isinstance(v, bool) and (mp.get(k) or {}).get("type") != "boolean":
                    problems.append(f"{name}: manifests.{k} is a switch in values.yaml but is not declared "
                                    "as a boolean")
    return problems


def check_umbrella(root):
    """Regenerate componentValues in a scratch copy and compare bytes with the committed file."""
    rel = os.path.join("blueprints", "openstack", "chart", "values.schema.json")
    tmp = tempfile.mkdtemp()
    try:
        shutil.copytree(os.path.join(root, "blueprints"), os.path.join(tmp, "blueprints"),
                        ignore=shutil.ignore_patterns("charts", "helm-toolkit"))
        os.makedirs(os.path.join(tmp, "hack"))
        shutil.copy(os.path.join(root, "hack", "gen-componentvalues-schema.py"), os.path.join(tmp, "hack"))
        r = subprocess.run([sys.executable, os.path.join(tmp, "hack", "gen-componentvalues-schema.py")],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return [f"umbrella: hack/gen-componentvalues-schema.py failed: {r.stderr.strip()[-300:]}"]
        want = open(os.path.join(tmp, rel), "rb").read()
        have = open(os.path.join(root, rel), "rb").read()
        if want != have:
            return ["umbrella: values.schema.json is not what hack/gen-componentvalues-schema.py produces from "
                    "the component schemas in this tree; re-run it and commit the result"]
        return []
    finally:
        shutil.rmtree(tmp)


def main():
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    bp = os.path.join(root, "blueprints")
    charts = sorted(d for d in (os.listdir(bp) if os.path.isdir(bp) else [])
                    if d != "openstack" and os.path.isfile(os.path.join(bp, d, "chart", "values.schema.json")))
    if not charts:
        print("FAIL: no component charts with a values.schema.json found -- nothing was measured")
        return 1
    problems = []
    for name in charts:
        found = check_chart(os.path.join(bp, name, "chart"), name)
        problems += found
        print(f"{'FAIL' if found else 'OK  '} {name}: {len(found)} problem(s)")
    umb = check_umbrella(root)
    problems += umb
    print(f"{'FAIL' if umb else 'OK  '} umbrella componentValues matches the generator")
    for p in problems:
        print(f"FAIL {p}")
    if problems:
        print(f"FAIL: {len(problems)} problem(s) across {len(charts)} component charts")
        return 1
    print(f"OK: {len(charts)} component charts declare every top-level values key, open all the way down, and the "
          "umbrella's componentValues matches the generator byte for byte")
    return 0


if __name__ == "__main__":
    sys.exit(main())
