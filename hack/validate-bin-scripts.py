#!/usr/bin/env python3
"""Every shell script a chart renders parses.

designate-sink.sh shipped without its `function start () {` line: `bash -n` rejects it ("syntax error near
unexpected token `}'"), so enabling the sink would have crash-looped it, and nothing noticed because the deployment
is off by default while its script renders in designate-bin at defaults.

Renders every blueprint chart at its defaults (helm-toolkit staged as the release does) and runs `bash -n` on:
  - every ConfigMap or Secret data key ending in .sh (the charts' -bin scripts);
  - every container's inline `bash -c` / `sh -c` script.
A chart that fails to render, or a run that checked no script, fails: an empty input is not a pass.
usage: validate-bin-scripts.py <repo>
"""
import base64
import glob
import os
import shutil
import subprocess
import sys
import tempfile

import yaml

repo = sys.argv[1] if len(sys.argv) > 1 else "."
toolkit = os.path.join(repo, "charts", "helm-toolkit")
failures, checked, charts = [], 0, 0


# What the umbrella delivers for a chart's storage (#308): cinder's default refuses, so a chart render carries
# hack/gate_values.py's arguments (nothing for every other chart).
import importlib.util as _gv_util
import os as _gv_os
_gv_spec = _gv_util.spec_from_file_location(
    "gate_values", _gv_os.path.join(_gv_os.path.dirname(_gv_os.path.abspath(__file__)), "gate_values.py"))
gate_values = _gv_util.module_from_spec(_gv_spec)
_gv_spec.loader.exec_module(gate_values)


def bash_n(text):
    r = subprocess.run(["bash", "-n"], input=text, capture_output=True, text=True)
    return r.returncode, r.stderr.strip().replace("\n", " ")[:200]


def pod_specs(d):
    spec = d.get("spec") or {}
    kind = d.get("kind")
    if kind == "Pod":
        return [spec]
    if kind == "CronJob":
        return [((spec.get("jobTemplate") or {}).get("spec") or {}).get("template", {}).get("spec") or {}]
    return [(spec.get("template") or {}).get("spec") or {}]


with tempfile.TemporaryDirectory() as tmp:
    for chart_dir in sorted(glob.glob(os.path.join(repo, "blueprints", "*", "chart"))):
        name = chart_dir.split(os.sep)[-2]
        w = os.path.join(tmp, name)
        shutil.copytree(chart_dir, w)
        cy = os.path.join(w, "Chart.yaml")
        txt = open(cy).read()
        open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
        if "helm-toolkit" in txt and not os.path.isdir(os.path.join(w, "charts", "helm-toolkit")):
            os.makedirs(os.path.join(w, "charts"), exist_ok=True)
            shutil.copytree(toolkit, os.path.join(w, "charts", "helm-toolkit"))
        r = subprocess.run(["helm", "template", name + "-gate", w, "--namespace", "openstack", "--kube-version", "1.36.0"]
                           + gate_values.helm_args(w), capture_output=True, text=True)
        if r.returncode != 0:
            failures.append("%s: does not render at defaults: %s" % (name, r.stderr.strip()[:200]))
            continue
        charts += 1
        for d in yaml.safe_load_all(r.stdout):
            if not d:
                continue
            if d.get("kind") in ("ConfigMap", "Secret"):
                for key, val in (d.get("data") or {}).items():
                    if not key.endswith(".sh"):
                        continue
                    if d["kind"] == "Secret":
                        try:
                            val = base64.b64decode(val).decode()
                        except Exception:
                            continue
                    checked += 1
                    rc, err = bash_n(val)
                    if rc:
                        failures.append("%s: %s/%s key %s does not parse: %s" % (name, d["kind"], d["metadata"]["name"], key, err))
            for pod in pod_specs(d):
                for c in (pod.get("initContainers") or []) + (pod.get("containers") or []):
                    cmd = c.get("command") or []
                    if len(cmd) >= 3 and os.path.basename(cmd[0]) in ("bash", "sh") and cmd[1] == "-c":
                        checked += 1
                        rc, err = bash_n(cmd[2])
                        if rc:
                            failures.append("%s: %s/%s container %s's inline script does not parse: %s"
                                            % (name, d["kind"], d["metadata"]["name"], c["name"], err))

if not charts or not checked:
    print("FAILED MEASUREMENT: %d chart(s) rendered, %d script(s) checked; nothing was verified" % (charts, checked))
    sys.exit(2)
for f in failures:
    print("FAIL: " + f)
if failures:
    sys.exit(1)
print("OK: %d shell script(s) across %d chart(s) parse (bash -n)" % (checked, charts))
