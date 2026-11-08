#!/usr/bin/env python3
"""Every bare Pod a component chart renders must be a Helm test hook (#179).

A Pod's spec cannot be changed once it exists. Krateo's composition-dynamic-controller applies
a chart's Pods as ordinary objects and patches them on every reconcile, so a Pod that a release
installed blocks every later upgrade of that component for good:

    cannot patch "aodh-<release>-test" with kind Pod: ... pod updates may not change fields

Measured on GKE, Kubernetes 1.36.4, core-provider 2.13.72, upgrading aodh 0.3.34 -> 0.3.41: the
test Pod from 0.3.34 kept aodh on 0.3.34 through five upgrades. Upstream OpenStack-Helm marks
test Pods `helm.sh/hook: test`, so they run only on `helm test` and are never installed.

Each chart is rendered twice: with its defaults, and with every manifests.*test* switch on.
Most test Pods are off by default, and a gate that rendered only the defaults would measure
almost nothing. The hook must be `test` (the deprecated alias `test-success` is refused, so
there is one spelling) with `before-hook-creation`.

Fails when no chart is found, when a chart does not render, or when no Pod is rendered at all.

Usage: hack/validate-test-pods.py [repo-root] [component ...]
"""
import argparse
import importlib.util
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("job_hooks", HERE / "validate-job-hooks.py")
job_hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job_hooks)
yaml = job_hooks.yaml


def test_switches(chart: Path):
    values = yaml.safe_load((chart / "values.yaml").read_text()) or {}
    manifests = values.get("manifests") or {}
    return sorted(k for k, v in manifests.items() if "test" in k and isinstance(v, bool))


def problems(component, docs):
    out, pods = [], 0
    for d in docs:
        if d.get("kind") != "Pod":
            continue
        pods += 1
        name = (d.get("metadata") or {}).get("name", "?")
        ann = (d.get("metadata") or {}).get("annotations") or {}
        hook = str(ann.get("helm.sh/hook", ""))
        policy = str(ann.get("helm.sh/hook-delete-policy", ""))
        if hook != "test":
            out.append(f"{component}: Pod/{name} has helm.sh/hook {hook or '(none)'!r}, not 'test': "
                       "it is installed as a plain Pod and blocks every later upgrade (#179)")
        if "before-hook-creation" not in [p.strip() for p in policy.split(",")]:
            out.append(f"{component}: Pod/{name} lacks hook-delete-policy before-hook-creation, "
                       "so a second `helm test` collides with the first Pod")
    return out, pods


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("components", nargs="*")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    charts = sorted(p.parent for p in (root / "blueprints").glob("*/chart/Chart.yaml"))
    charts = [c for c in charts if c.parent.name != "openstack"]
    if a.components:
        charts = [c for c in charts if c.parent.name in a.components]
    if not charts:
        print("FAIL: no component charts found -- nothing was measured")
        return 1
    failures, unrendered, total_pods = [], [], 0
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        toolkit = root / "charts" / "helm-toolkit"
        for chart in charts:
            component = chart.parent.name
            switches = test_switches(chart)
            on = [x for k in switches for x in ("--set", f"manifests.{k}=true")]
            seen = set()
            for label, extra in (("defaults", []), ("all tests on", on)):
                if label == "all tests on" and not switches:
                    continue
                try:
                    docs = job_hooks.render(chart, toolkit, component, work / "r", extra)
                except Exception as e:  # a chart that does not render measured nothing
                    unrendered.append(f"{component} ({label}): {str(e)[:300]}")
                    continue
                found, pods = problems(component, docs)
                total_pods += pods
                for f in found:
                    if f not in seen:
                        seen.add(f)
                        failures.append(f)
            print(f"{'FAIL' if seen else 'OK  '} {component}: switches {switches or 'none'}")
    for u in unrendered:
        print(f"FAIL {u}: did not render, so nothing was measured")
    if total_pods == 0:
        print("FAIL: no Pod rendered in any chart -- nothing was measured")
        return 1
    for f in failures:
        print(f"FAIL {f}")
    if failures or unrendered:
        print(f"FAIL: {len(failures)} problem(s), {len(unrendered)} unrendered, {total_pods} Pods rendered")
        return 1
    print(f"OK: {len(charts)} charts, {total_pods} Pods rendered, every one a test hook")
    return 0


if __name__ == "__main__":
    sys.exit(main())
