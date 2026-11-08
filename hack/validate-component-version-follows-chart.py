#!/usr/bin/env python3
"""An umbrella upgrade must move its components to the umbrella's own version (#179).

Measured on a 6-node GKE cluster, Kubernetes 1.36.4, core-provider 2.13.72: 0.3.34 installed,
then the 0.3.36 CompositionDefinition applied, exactly as the README's upgrade section says. The
umbrella's Helm release moved to openstack-0.3.36. Every component CompositionDefinition stayed at
`spec.chart.version: 0.3.34`, every component Composition stayed at v0-3-34, and powerdns -- new
in 0.3.36 -- was created at 0.3.34, a chart that was never published, so it could never sync.

The cause was a default. The umbrella took the component version from `.Values.chartVersion`,
and values.schema.json defaulted that field to `CHART_VERSION`, which the release stamps. The
schema becomes the generated CRD, and the API server WRITES a structural-schema default into the
object when it is created. So a Composition born at 0.3.34 held `spec.chartVersion: 0.3.34` as if
a user had typed it, read back through the v0-3-36 API it still said 0.3.34, and every later
umbrella chart rendered its components at the version the install was born at.

Two checks, one per half of that mechanism:

  (a) No values.schema.json in the repo may carry a release-stamped placeholder in a `default`.
      A stamped default is frozen into every object at create time, so it can never be a version
      that has to change on upgrade. The placeholders are read out of release-tag.yaml's own
      `sed` stamps rather than listed here, so a new placeholder cannot slip past.

  (b) Render the umbrella as release X with values that a Composition born at an OLDER release
      carries: the older release's stamped schema defaults, plus the stale `chartVersion` that
      0.3.34-0.3.37 wrote into every Composition. Every component CompositionDefinition must be at
      X, and the version helpers must answer vX. This is the differential that matters: it fails
      on the chart that shipped the defect, and passes only when the stored object cannot steer
      the version.

Exits non-zero when it finds nothing to check, for the reason every gate here does.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    print("FAIL: PyYAML is required; nothing was measured", file=sys.stderr)
    sys.exit(2)

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 \
    else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHART = os.path.join(ROOT, "blueprints", "openstack", "chart")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
RELEASE = os.path.join(ROOT, ".github", "workflows", "release-tag.yaml")

# X is the release being installed; OLD is the release the Composition was created at.
X = "0.3.99"
OLD = "0.3.34"


def measurement_failed(msg):
    print(f"FAIL: {msg} -- nothing was measured", file=sys.stderr)
    sys.exit(2)


def stamped_placeholders():
    """Every placeholder release-tag.yaml substitutes, read from its `sed -i "s/NAME/${...}/g"`."""
    if not os.path.isfile(RELEASE):
        measurement_failed(f"no release workflow at {RELEASE}")
    names = set(re.findall(r's/([A-Z][A-Z0-9_]+)/\$\{', open(RELEASE).read()))
    if not names:
        measurement_failed("release-tag.yaml stamps no placeholder this gate can recognise; "
                           "the stamp syntax changed, so update the pattern rather than pass")
    return names


def schema_files():
    out = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, "blueprints")):
        # Vendored dependencies are not this repo's schemas.
        dirnames[:] = [d for d in dirnames if d not in ("charts", "helm-toolkit")]
        if "values.schema.json" in filenames:
            out.append(os.path.join(dirpath, "values.schema.json"))
    return sorted(out)


def stamped_defaults(node, names, path=""):
    hits = []
    if isinstance(node, dict):
        if "default" in node:
            text = json.dumps(node["default"])
            for n in names:
                if n in text:
                    hits.append((path or "/", n))
        for k, v in node.items():
            hits += stamped_defaults(v, names, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            hits += stamped_defaults(v, names, f"{path}[{i}]")
    return hits


def check_a(problems):
    names = stamped_placeholders()
    files = schema_files()
    if not files:
        measurement_failed("found no values.schema.json under blueprints/")
    for f in files:
        for where, name in stamped_defaults(json.load(open(f)), names):
            problems.append(
                f"{os.path.relpath(f, ROOT)}: `default` at {where} carries the release-stamped "
                f"{name}. The API server writes a default into every object at create time, so "
                "an install keeps the value it was born with through every upgrade (#179).")
    return len(files), sorted(names)


def top_level_defaults(schema):
    return {k: v["default"] for k, v in (schema.get("properties") or {}).items()
            if isinstance(v, dict) and "default" in v}


PROBE = """apiVersion: v1
kind: ConfigMap
metadata:
  name: version-probe
data:
  version: {{ include "osh.version" (list $) | quote }}
  apiVersion: {{ include "osh.apiVersion" (list $) | quote }}
"""


def check_b(problems):
    if not os.path.isdir(CHART):
        measurement_failed(f"no umbrella chart at {CHART}")
    tmp = tempfile.mkdtemp()
    try:
        dst = os.path.join(tmp, "chart")
        shutil.copytree(CHART, dst)
        # Stamp the staged chart as the release stamps release X: the same files, the same
        # placeholder, so the render is the one the release would ship.
        for f in ("Chart.yaml", "values.yaml", "values.schema.json"):
            p = os.path.join(dst, f)
            if os.path.isfile(p):
                # Read before opening for write: open(p, "w") truncates, and Python evaluates
                # the receiver first, so a one-liner would stamp an empty file.
                with open(p) as fh:
                    body = fh.read()
                with open(p, "w") as fh:
                    fh.write(body.replace("CHART_VERSION", X))
        if os.path.isdir(TOOLKIT):
            os.makedirs(os.path.join(dst, "charts"), exist_ok=True)
            shutil.copytree(TOOLKIT, os.path.join(dst, "charts", "helm-toolkit"))
        open(os.path.join(dst, "templates", "zz-version-probe.yaml"), "w").write(PROBE)

        # What a Composition created at OLD holds in its spec: the OLD release's stamped schema
        # defaults, as the API server wrote them, plus the chartVersion 0.3.34-0.3.37 defaulted.
        old_schema = json.loads(open(os.path.join(CHART, "values.schema.json")).read()
                                .replace("CHART_VERSION", OLD))
        stale = top_level_defaults(old_schema)
        stale["chartVersion"] = OLD
        stale["profile"] = "full"
        vals = os.path.join(tmp, "stale.yaml")
        yaml.safe_dump(stale, open(vals, "w"), default_flow_style=False)

        r = subprocess.run(["helm", "template", "gate", dst, "-f", vals,
                            "--kube-version", "1.36.0"], capture_output=True, text=True)
        if r.returncode != 0:
            problems.append(
                "the umbrella refuses values a Composition created at "
                f"{OLD} still carries -- {r.stderr.strip()[-400:]}. An existing install could "
                "never render the new release.")
            return 0
        docs = [d for d in yaml.safe_load_all(r.stdout) if d]
        cds = [d for d in docs if d.get("kind") == "CompositionDefinition"]
        if not cds:
            measurement_failed("the render emitted no component CompositionDefinition, so no "
                               "version was observed")
        for d in cds:
            v = str((d.get("spec") or {}).get("chart", {}).get("version"))
            if v != X:
                problems.append(
                    f"CompositionDefinition {d['metadata']['name']} renders at chart version "
                    f"{v} when the umbrella is {X}: the stored object is steering the component "
                    "version, so the upgrade leaves it behind (#179).")
        probe = [d for d in docs if d.get("kind") == "ConfigMap"
                 and d["metadata"]["name"] == "version-probe"]
        if not probe:
            measurement_failed("the version probe rendered nothing")
        want = "v" + X.replace(".", "-")
        data = probe[0].get("data") or {}
        if data.get("version") != want:
            problems.append(f"osh.version answers {data.get('version')!r}, not {want!r}")
        if data.get("apiVersion") != f"composition.krateo.io/{want}":
            problems.append(f"osh.apiVersion answers {data.get('apiVersion')!r}, not "
                            f"'composition.krateo.io/{want}'")
        return len(cds)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    problems = []
    nfiles, names = check_a(problems)
    ncds = check_b(problems)
    if problems:
        for p in problems:
            print(f"FAIL: {p}")
        return 1
    print(f"OK: {nfiles} schemas carry no stamped default ({', '.join(names)}); "
          f"rendered as {X} with a Composition born at {OLD}, all {ncds} component "
          f"CompositionDefinitions are at {X} and the helpers answer v{X.replace('.', '-')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
