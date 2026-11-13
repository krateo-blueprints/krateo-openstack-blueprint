#!/usr/bin/env python3
"""The umbrella is not Ready while a selected component does not exist yet (#257).

    hack/validate-umbrella-install-pending.py [repo-root]

The umbrella emits one DAG tier per render, and composition-dynamic-controller health-evaluates the
children the chart HAS emitted. Between the render that made the second-to-last tier Ready and the
render that emits the last one, "every emitted child is healthy" was true while a selected component
did not exist: measured on a 32-component install, Ready=True at 21:47:26, cyborg emitted at 21:48:21.

The fix has two halves, and each is checked here:
  1. CHART. While the render holds back any selected component, it emits the ConfigMap
     openstack-install-pending listing them; with nothing held back it emits no such ConfigMap.
     Offline (helm template) no lookup sees a served CRD, so nothing is emitted and EVERY selected
     component is pending: the ConfigMap must list exactly osh.selected's set, for identity and
     full-strict. With no component selected, the ConfigMap must not render at all.
  2. PROJECTION. blueprints/openstack/compositiondefinition.yaml has exactly one statusDataTemplate
     entry for `health`, preserveUnknownFields. Its jq is EXECUTED (jq binary) over three inventories:
     the ConfigMap present -> {ready: false, reason: "Creating", ...}; absent, or nothing recorded ->
     exactly {} (no `ready` key). Never {ready: true}: core-provider counts that as evidence where no
     child could be read. Never empty output: that would leave a stale not-ready forever.

Fails when helm, jq or PyYAML is missing, or when a render produces nothing to check.
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
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")
    sys.exit(2)

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
UMBRELLA = os.path.join(ROOT, "blueprints", "openstack", "chart")
CD = os.path.join(ROOT, "blueprints", "openstack", "compositiondefinition.yaml")
NAME = "openstack-install-pending"
for tool in ("helm", "jq"):
    if shutil.which(tool) is None:
        print("FAILED MEASUREMENT: %s not on PATH, so nothing was checked" % tool)
        sys.exit(2)

failures = []


def fail(msg):
    failures.append(msg)


def render(values):
    with tempfile.TemporaryDirectory() as tmp:
        w = os.path.join(tmp, "umbrella")
        shutil.copytree(UMBRELLA, w)
        cy = os.path.join(w, "Chart.yaml")
        txt = open(cy).read()
        open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
        with open(os.path.join(w, "templates", "zz-gate-selected.yaml"), "w") as f:
            f.write('# SELECTED {{ include "osh.selected" (list $) }}\n')
        vf = os.path.join(tmp, "values.yaml")
        yaml.safe_dump(values, open(vf, "w"))
        r = subprocess.run(["helm", "template", "openstack-gate", w, "-n", "openstack", "--kube-version", "1.36.0",
                            "-f", vf], capture_output=True, text=True)
    if r.returncode:
        print("FAILED MEASUREMENT: the umbrella does not render with %s: %s" % (values, r.stderr.strip()[:300]))
        sys.exit(2)
    selected = []
    for line in r.stdout.splitlines():
        if line.startswith("# SELECTED"):
            selected = sorted(line[len("# SELECTED"):].split())
    docs = [d for d in yaml.safe_load_all(r.stdout) if d]
    cms = [d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == NAME]
    return selected, cms


def chart_half():
    for profile in ("identity", "full-strict"):
        selected, cms = render({"profile": profile})
        if not selected:
            print("FAILED MEASUREMENT: osh.selected returned nothing for profile %s" % profile)
            sys.exit(2)
        if len(cms) != 1:
            fail("profile %s: %d ConfigMap(s) %s rendered while all %d selected components are held back, want 1"
                 % (profile, len(cms), NAME, len(selected)))
            continue
        pending = sorted((cms[0].get("data") or {}).get("pending", "").split())
        if pending != selected:
            fail("profile %s: %s lists %s, want the selected set %s" % (profile, NAME, pending, selected))
        if (cms[0].get("data") or {}).get("count") != str(len(selected)):
            fail("profile %s: %s count is %r, want %r" % (profile, NAME, (cms[0].get("data") or {}).get("count"),
                                                        str(len(selected))))
    _selected, cms = render({"components": []})
    if cms:
        fail("with no component selected, %s still renders: it must exist only while something is held back" % NAME)


def jq(expr, doc):
    r = subprocess.run(["jq", "-c", expr], input=json.dumps(doc), capture_output=True, text=True)
    if r.returncode:
        return "<jq error: %s>" % r.stderr.strip()[:200]
    out = r.stdout.strip()
    return json.loads(out) if out else "<no output>"


def projection_half():
    cd = yaml.safe_load(open(CD))
    entries = [e for e in ((cd.get("spec") or {}).get("statusDataTemplate") or []) if e.get("forPath") == "health"]
    if len(entries) != 1:
        fail("compositiondefinition.yaml has %d statusDataTemplate entries for health, want 1" % len(entries))
        return
    e = entries[0]
    if e.get("preserveUnknownFields") is not True:
        fail("the health projection must be preserveUnknownFields: true (it writes either an object or {})")
    m = re.match(r"^\$\{(.*)\}$", e.get("expression", "").strip(), re.S)
    if not m:
        fail("the health expression is not a ${ jq } program: %r" % e.get("expression"))
        return
    expr = m.group(1)
    item = {"apiVersion": "v1", "resource": "configmaps", "name": NAME, "namespace": "openstack", "path": "p"}
    other = {"apiVersion": "composition.krateo.io/v0-3-55", "resource": "keystones", "name": "keystone",
             "namespace": "openstack", "path": "p"}
    pending = jq(expr, {"status": {"managed": [other, item]}})
    if not (isinstance(pending, dict) and pending.get("ready") is False and pending.get("reason") == "Creating"
            and pending.get("message")):
        fail("with %s in status.managed the projection is %r, want {ready: false, reason: Creating, message}"
             % (NAME, pending))
    for label, doc in (("absent", {"status": {"managed": [other]}}), ("nothing recorded", {"status": {}})):
        got = jq(expr, doc)
        if got != {}:
            fail("with %s %s the projection is %r, want exactly {} (no ready key: never ready: true, never no "
                 "output)" % (NAME, label, got))


chart_half()
projection_half()
if failures:
    for f in failures:
        print("FAIL: " + f)
    sys.exit(1)
print("OK: %s lists exactly the held-back selected components (identity, full-strict) and is absent when none "
      "are; the health projection gives Ready=False/Creating while it exists and {} otherwise" % NAME)
