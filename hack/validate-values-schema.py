#!/usr/bin/env python3
"""Check that every chart's default values.yaml satisfies that chart's own values.schema.json.

Issue #53 was this defect: keystone shipped `conf.software.apache2.a2enmod: null` while its
values.schema.json declared that property `"type": "array"`. Helm validates values against the
schema BEFORE it renders anything, so `helm install` of the blueprint's own defaults died with

    at '/conf/software/apache2/a2enmod': got null, want array

and chart-inspector answered 500 with nothing actionable in it. The chart's templates had always
guarded the field with `{{- if ... }}`, so null was a supported value everywhere except in the
curated schema that gates the API.

The per-PR lint workflow could not catch it. That job renders the real charts, and charts in this
repo legitimately fail to render without values or private dependencies, so every failure there is
reported as a warning — a schema violation arrives looking exactly like a missing subchart.

So this check does the one thing that needs neither templates nor dependencies: it puts the chart's
values.yaml and values.schema.json in a throwaway chart of its own and renders that. Nothing else
of the chart comes along, which is what makes the result unambiguous — the only thing that can fail
is the values-vs-schema contract. Helm does the validating, deliberately: a JSON Schema library here
would be a second implementation that disagrees with Helm at the edges, and Helm is what a user hits.

Scope: a chart's own defaults against its own schema. Helm also validates a parent's coalesced
values against subchart schemas at install time; that needs the real dependency tree and belongs
with the render job, not here.

Exit 1 if any chart's defaults violate its schema.
"""
import os, re, shutil, subprocess, sys, tempfile

SKIP_DIRS = {".git", "charts"}
AT_LINE = re.compile(r"^\s*-\s+at\s+'")


def charts(root):
    """Every chart directory, vendored subcharts under charts/ excluded."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if "Chart.yaml" in filenames:
            found.append(dirpath)
    return sorted(found)


def validate(chart_dir):
    """Render a throwaway chart carrying only this chart's values + schema.

    Returns (status, detail): status is "pass", "fail" or "skip".
    """
    schema = os.path.join(chart_dir, "values.schema.json")
    values = os.path.join(chart_dir, "values.yaml")
    if not os.path.exists(schema):
        return "skip", "no values.schema.json"
    if not os.path.exists(values):
        return "skip", "values.schema.json but no values.yaml"

    with tempfile.TemporaryDirectory() as tmp:
        harness = os.path.join(tmp, "harness")
        os.makedirs(os.path.join(harness, "templates"))
        # Our own Chart.yaml, so the CHART_VERSION placeholder in the real one needs no stamping.
        with open(os.path.join(harness, "Chart.yaml"), "w") as f:
            f.write("apiVersion: v2\nname: harness\nversion: 0.0.1\n")
        # One inert template: a chart with an empty templates/ renders fine today, but keeping a
        # file there makes the harness behave identically across Helm versions.
        with open(os.path.join(harness, "templates", "noop.yaml"), "w") as f:
            f.write("# intentionally empty\n")
        shutil.copyfile(values, os.path.join(harness, "values.yaml"))
        shutil.copyfile(schema, os.path.join(harness, "values.schema.json"))

        p = subprocess.run(
            ["helm", "template", "values-schema-conformance", harness],
            capture_output=True, text=True,
        )
        if p.returncode == 0:
            return "pass", ""
        detail = [l.strip() for l in (p.stderr or p.stdout).splitlines() if AT_LINE.match(l)]
        return "fail", "; ".join(detail) or (p.stderr or p.stdout).strip().splitlines()[-1]


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    if not shutil.which("helm"):
        print("helm not found on PATH", file=sys.stderr)
        return 2

    bad, checked = [], 0
    print("values.yaml vs values.schema.json")
    for chart_dir in charts(root):
        rel = os.path.relpath(chart_dir, root)
        status, detail = validate(chart_dir)
        if status == "skip":
            continue
        checked += 1
        print("  %-4s %s" % (status.upper(), rel))
        if status == "fail":
            print("       %s" % detail)
            bad.append((rel, detail))

    print()
    if bad:
        print("DEFAULTS THAT VIOLATE THEIR OWN SCHEMA: %d of %d charts" % (len(bad), checked))
        for rel, detail in bad:
            print("  %s  ->  %s" % (rel, detail))
        print()
        print("Helm rejects these values before rendering, so a default install of the blueprint")
        print("fails at `helm install` and chart-inspector answers 500. Fix the schema when the")
        print("templates already accept the value; fix values.yaml only if it is not vendored.")
        return 1
    if checked == 0:
        print("FAILED MEASUREMENT: no chart carrying a values.schema.json was found. 35 of them")
        print("exist in this repository, so finding none means the scan did not look where it")
        print("meant to -- a clean result from an instrument that examined nothing.")
        return 1
    print("%d charts carry a schema; every one accepts its own defaults" % checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
