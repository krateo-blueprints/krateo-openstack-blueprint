#!/usr/bin/env python3
"""Every long-running workload rolls when the config it mounts changes (#223).

OpenStack-Helm charts roll a workload on a config change through pod-template annotations:
`configmap-etc-hash` and `configmap-bin-hash` hash the rendered configmap-etc.yaml /
configmap-bin.yaml, so a values change that alters the config changes the pod template and the
controller replaces the pods. Five workloads had neither annotation (mistral-engine,
mistral-event-engine, tacker-conductor, tacker-server, watcher-decision-engine), and skyline had no
configmap-bin-hash, so a config change was written into the Secret and the running pods kept
the old one. They mount the files with subPath, which never refreshes in place. Observed on a 0.3.47 install: the watcher release went
to revision 2 with the corrected config, and watcher-decision-engine stayed at StatefulSet
generation 1 with the same pod. It picked the fix up only because it was crash-looping.

This gate renders every component chart and checks every Deployment, StatefulSet and DaemonSet:
if its pod template mounts the chart's own `<name>-etc` (or `<name>-bin`) Secret/ConfigMap, the
template carries `configmap-etc-hash` (or `configmap-bin-hash`) with a sha256 value. Jobs are
out of scope: hook Jobs are recreated on every install and upgrade.

The `-etc`/`-bin` objects counted as the chart's own are the ones rendered from
templates/configmap-etc.yaml and templates/configmap-bin.yaml, the templates the annotations hash.

Fails when no chart renders or when no workload mounting such config is found.

Usage: hack/validate-config-hash-rollout.py [repo-root]
"""
import argparse
import importlib.util
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("job_hooks", HERE / "validate-job-hooks.py")
job_hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job_hooks)

LONG_RUNNING = ("Deployment", "StatefulSet", "DaemonSet")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# suffix of the config object -> (template it is rendered from, annotation that must hash it)
OWN = {
    "-etc": ("templates/configmap-etc.yaml", "configmap-etc-hash"),
    "-bin": ("templates/configmap-bin.yaml", "configmap-bin-hash"),
}


def own_configs(docs):
    """{object name: suffix} for the Secrets/ConfigMaps rendered from configmap-etc/-bin."""
    out = {}
    for d in docs:
        if d.get("kind") not in ("ConfigMap", "Secret"):
            continue
        name = d["metadata"]["name"]
        source = d.get("__source__", "")
        for suffix, (template, _) in OWN.items():
            if name.endswith(suffix) and (not source or source.endswith(template)):
                out[name] = suffix
    return out


def mounted(pod_spec, names):
    """Suffixes of the chart's own config objects this pod template mounts as volumes."""
    found = set()
    for v in pod_spec.get("volumes") or []:
        src = v.get("secret") or v.get("configMap")
        if not src:
            continue
        n = src.get("secretName") or src.get("name")
        if n in names:
            found.add(names[n])
        for p in ((v.get("projected") or {}).get("sources") or []):
            s = p.get("secret") or p.get("configMap") or {}
            if s.get("name") in names:
                found.add(names[s["name"]])
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", nargs="?", default=".")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    charts = sorted(p.parent for p in (root / "blueprints").glob("*/chart/Chart.yaml"))
    charts = [c for c in charts if c.parent.name != "openstack"]
    if not charts:
        print("FAIL: no component charts found -- nothing was measured")
        return 1
    failures, unrendered, checked = [], [], 0
    toolkit = root / "charts" / "helm-toolkit"
    with tempfile.TemporaryDirectory() as td:
        for chart in charts:
            component = chart.parent.name
            try:
                docs = job_hooks.render(chart, toolkit, component, Path(td) / "r", [])
            except Exception as e:
                unrendered.append(f"{component}: {str(e)[:300]}")
                continue
            names = own_configs(docs)
            bad, n = [], 0
            for d in docs:
                if d.get("kind") not in LONG_RUNNING:
                    continue
                tpl = (d.get("spec") or {}).get("template") or {}
                suffixes = mounted(tpl.get("spec") or {}, names)
                if not suffixes:
                    continue
                n += 1
                ann = (tpl.get("metadata") or {}).get("annotations") or {}
                for suffix in sorted(suffixes):
                    template, key = OWN[suffix]
                    val = str(ann.get(key, "")).strip('"')
                    if key not in ann:
                        bad.append(f"{component}: {d['kind']}/{d['metadata']['name']} mounts its "
                                   f"*{suffix} config but has no {key} annotation, so a change to "
                                   f"{template} never rolls it")
                    elif not SHA256.match(val):
                        bad.append(f"{component}: {d['kind']}/{d['metadata']['name']} {key} is "
                                   f"{val!r}, not a sha256 of {template}")
            checked += n
            failures += bad
            print(f"{'FAIL' if bad else 'OK  '} {component}: {n} workload(s) mounting their own config")
    for u in unrendered:
        print(f"FAIL {u}: did not render, so nothing was measured")
    if checked == 0:
        print("FAIL: no workload mounting its chart's own config was found -- nothing was measured")
        return 1
    for f in failures:
        print(f"FAIL {f}")
    if failures or unrendered:
        print(f"FAIL: {len(failures)} problem(s), {len(unrendered)} unrendered")
        return 1
    print(f"OK: {checked} long-running workload(s) across {len(charts)} charts each roll when the "
          "config they mount changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
