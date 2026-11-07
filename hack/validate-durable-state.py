#!/usr/bin/env python3
"""State that exists nowhere else must be on a claim, not on a node.

mariadb 0.3.35 rendered one replica, no volumeClaimTemplates, and a hostPath at /tmp/mysql-data.
A node restart on kind emptied it, because a kind node's /tmp is tmpfs, and start.py then
crash-looped on the missing grastate.dat with every OpenStack database gone. rabbitmq rendered
the same shape at /var/lib/rabbitmq. Both came from the chart defaults, so the failure needed no
misconfiguration, and nothing reported it until a machine rebooted. Issue #177.

For each entry in hack/durable-state.yaml this renders the chart with its defaults and asserts:

- the StatefulSet exists and has a claim template named after the volume;
- that volume is not also declared in the pod spec, where a hostPath or emptyDir would be;
- every container mounting the state path mounts that volume, so nothing reads a neighbour;
- the claim names no StorageClass, because `general` exists on no cluster this blueprint targets
  and an unset class is the only value that works everywhere.

Rendering is the point: the values alone say what was intended, and the template decides what
ships.

Exits non-zero when the list is empty or a chart does not render, because a gate that inspected
nothing has said nothing.
"""
import os
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    sys.exit("ERROR: PyYAML is required")

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 \
    else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def render(chart_dir):
    tmp = tempfile.mkdtemp()
    try:
        dst = os.path.join(tmp, "chart")
        shutil.copytree(chart_dir, dst)
        cy = os.path.join(dst, "Chart.yaml")
        meta = open(cy).read()
        with open(cy, "w") as f:
            f.write(meta.replace("CHART_VERSION", "0.0.0-gate"))
        tk = os.path.join(ROOT, "charts", "helm-toolkit")
        if os.path.isdir(tk):
            os.makedirs(os.path.join(dst, "charts"), exist_ok=True)
            shutil.copytree(tk, os.path.join(dst, "charts", "helm-toolkit"))
        p = subprocess.run(["helm", "template", "gate", dst, "--namespace", "openstack"],
                           capture_output=True, text=True)
        if p.returncode:
            return None, p.stderr.strip().splitlines()[-1] if p.stderr else "helm template failed"
        return [d for d in yaml.safe_load_all(p.stdout) if d], None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check(entry, docs):
    problems = []
    name, volume, path = entry["statefulset"], entry["volume"], entry["mountPath"]
    sts = [d for d in docs if d.get("kind") == "StatefulSet" and d["metadata"]["name"] == name]
    if not sts:
        return [f"StatefulSet {name} is not rendered"]
    spec = sts[0]["spec"]
    pod = spec["template"]["spec"]
    claims = {c["metadata"]["name"]: c for c in spec.get("volumeClaimTemplates") or []}
    if volume not in claims:
        problems.append(f"{name} has no claim template named {volume}")
    else:
        klass = claims[volume]["spec"].get("storageClassName")
        if klass:
            problems.append(f"{name} claim {volume} names StorageClass {klass!r}, which a "
                            "cluster may not have; leave it unset unless an operator chose it")
    for v in pod.get("volumes") or []:
        if v["name"] == volume:
            kind = next((k for k in v if k != "name"), "unknown")
            problems.append(f"{name} also declares {volume} in the pod as {kind}, "
                            "which is state that stays on one node or dies with the pod")
    containers = (pod.get("containers") or []) + (pod.get("initContainers") or [])
    mounted = 0
    for c in containers:
        for m in c.get("volumeMounts") or []:
            if m.get("mountPath") == path:
                mounted += 1
                if m["name"] != volume:
                    problems.append(f"{name} container {c['name']} mounts {m['name']} at {path}, "
                                    f"not {volume}")
    if not mounted:
        problems.append(f"{name} mounts nothing at {path}")
    return problems


def main():
    listing = os.path.join(ROOT, "hack", "durable-state.yaml")
    entries = (yaml.safe_load(open(listing)) or {}).get("durable") or []
    if not entries:
        sys.exit(f"ERROR: nothing to inspect in {listing}")
    failed = False
    for entry in entries:
        chart = os.path.join(ROOT, "blueprints", entry["component"], "chart")
        docs, err = render(chart)
        if err:
            print(f"FAIL {entry['component']}: does not render: {err}")
            failed = True
            continue
        problems = check(entry, docs)
        for p in problems:
            print(f"FAIL {entry['component']}: {p}")
        if problems:
            failed = True
        else:
            print(f"OK   {entry['component']}: {entry['statefulset']} keeps "
                  f"{entry['mountPath']} on claim {entry['volume']}")
    if failed:
        sys.exit("#177: state that exists nowhere else must be on a claim, see "
                 "hack/durable-state.yaml")


if __name__ == "__main__":
    main()
