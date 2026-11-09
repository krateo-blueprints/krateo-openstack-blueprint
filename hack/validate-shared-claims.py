#!/usr/bin/env python3
"""Every claim a default install renders can be bound and attached (#201).

Two rules, checked on each component chart rendered with its defaults under a release name
unlike its chart name, as Krateo renders it:

1. A default cluster's StorageClass is ReadWriteOnce. A component that is not flagged
   `requiresProvisioning` in the umbrella must not render a ReadWriteMany claim by default:
   tacker's three claims asked for ReadWriteMany on a class named `general` that no target
   cluster has, and stayed Pending on every install from 0.3.34 to 0.3.43 (#201).

2. A ReadWriteOnce claim can be used by several pods only on the node it attaches to. When more
   than one pod template mounts the same ReadWriteOnce claim, all but one of them (the anchor)
   must carry a required podAffinity on kubernetes.io/hostname whose label selector matches the
   anchor's pod labels, and the anchor must carry no required affinity to any of the others,
   which would leave each waiting for the other to schedule first.

3. A component that is not flagged `requiresProvisioning` must not name a storageClassName in a
   default render: unset is what takes the cluster's default class. glance-images named
   `standard`, which exists only on clusters that happen to call a class that, and tacker named
   `general`, which exists on none (#201). This covers standalone claims and StatefulSet
   volumeClaimTemplates alike.

Per-pod claims (StatefulSet volumeClaimTemplates) are not shared and are not checked by rule 2.

Fails when no chart is found, when a chart does not render, or when no pod template renders.

Usage: hack/validate-shared-claims.py [repo-root] [component ...]
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

WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob")
HOSTNAME = "kubernetes.io/hostname"


def provisioning_flagged(root: Path):
    values = yaml.safe_load((root / "blueprints/openstack/chart/values.yaml").read_text()) or {}
    flagged = set()
    for entry in values.get("components") or []:
        if isinstance(entry, dict) and entry.get("requiresProvisioning"):
            flagged.add(entry.get("name"))
    return flagged


def pod_template(doc):
    spec = doc.get("spec") or {}
    if doc.get("kind") == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
    return spec.get("template") or {}


def claims_of(template):
    names = set()
    for v in (template.get("spec") or {}).get("volumes") or []:
        pvc = v.get("persistentVolumeClaim")
        if pvc and pvc.get("claimName"):
            names.add(pvc["claimName"])
    return names


def selector_matches(selector, labels):
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, vals = expr.get("key"), expr.get("operator"), expr.get("values") or []
        if op == "In" and labels.get(key) not in vals:
            return False
        if op == "NotIn" and labels.get(key) in vals:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


def required_affinity_to(template, target_labels):
    """True when the template requires to share a node with pods carrying target_labels."""
    aff = ((template.get("spec") or {}).get("affinity") or {}).get("podAffinity") or {}
    for term in aff.get("requiredDuringSchedulingIgnoredDuringExecution") or []:
        if term.get("topologyKey") == HOSTNAME and selector_matches(term.get("labelSelector") or {}, target_labels):
            return True
    return False


def check(component, docs, flagged):
    problems = []
    modes = {}
    for d in docs:
        named = []
        if d.get("kind") == "PersistentVolumeClaim":
            named.append((f"PersistentVolumeClaim/{d['metadata']['name']}", d.get("spec") or {}))
        if d.get("kind") == "StatefulSet":
            for t in (d.get("spec") or {}).get("volumeClaimTemplates") or []:
                named.append((f"StatefulSet/{d['metadata']['name']} volumeClaimTemplate "
                              f"{(t.get('metadata') or {}).get('name')}", t.get("spec") or {}))
        for what, spec in named:
            if spec.get("storageClassName") and component not in flagged:
                problems.append(f"{component}: {what} names storageClassName "
                                f"{spec['storageClassName']!r} by default; leave it unset so the claim "
                                "takes the cluster's default class (#201), or flag the component "
                                "requiresProvisioning")
        if d.get("kind") == "PersistentVolumeClaim":
            name = d["metadata"]["name"]
            modes[name] = set((d.get("spec") or {}).get("accessModes") or [])
            if "ReadWriteMany" in modes[name] and component not in flagged:
                problems.append(f"{component}: PersistentVolumeClaim/{name} asks for ReadWriteMany by default, "
                                "and a default cluster's StorageClass offers ReadWriteOnce only (#201); "
                                "default to ReadWriteOnce or flag the component requiresProvisioning")
    workloads = []
    for d in docs:
        if d.get("kind") in WORKLOADS:
            t = pod_template(d)
            workloads.append((f"{d['kind']}/{d['metadata']['name']}", t, (t.get("metadata") or {}).get("labels") or {}))
    for claim, m in sorted(modes.items()):
        if not (m & {"ReadWriteOnce", "ReadWriteOncePod"}):
            continue
        users = [w for w in workloads if claim in claims_of(w[1])]
        if len(users) < 2:
            continue
        anchors = []
        for anchor in users:
            others = [u for u in users if u is not anchor]
            if all(required_affinity_to(o[1], anchor[2]) for o in others) and \
                    not any(required_affinity_to(anchor[1], o[2]) for o in others):
                anchors.append(anchor[0])
        if not anchors:
            problems.append(f"{component}: ReadWriteOnce PersistentVolumeClaim/{claim} is mounted by "
                            f"{', '.join(u[0] for u in users)}, and no required podAffinity on {HOSTNAME} "
                            "co-locates them with one anchor that waits on none of the others")
    return problems, len(workloads)


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
    flagged = provisioning_flagged(root)
    failures, unrendered, templates = [], [], 0
    with tempfile.TemporaryDirectory() as td:
        toolkit = root / "charts" / "helm-toolkit"
        for chart in charts:
            component = chart.parent.name
            try:
                docs = job_hooks.render(chart, toolkit, component, Path(td) / "r", [])
            except Exception as e:  # a chart that does not render measured nothing
                unrendered.append(f"{component}: {str(e)[:300]}")
                continue
            found, n = check(component, docs, flagged)
            templates += n
            failures.extend(found)
            print(f"{'FAIL' if found else 'OK  '} {component}: {n} pod templates")
    for u in unrendered:
        print(f"FAIL {u}: did not render, so nothing was measured")
    if templates == 0:
        print("FAIL: no pod template rendered in any chart -- nothing was measured")
        return 1
    for f in failures:
        print(f"FAIL {f}")
    if failures or unrendered:
        print(f"FAIL: {len(failures)} problem(s), {len(unrendered)} unrendered, across {len(charts)} charts")
        return 1
    print(f"OK: {len(charts)} charts, {templates} pod templates; every default claim takes the cluster's "
          "default class and can be bound, and every shared ReadWriteOnce claim is co-located")
    return 0


if __name__ == "__main__":
    sys.exit(main())
