#!/usr/bin/env python3
"""Move mariadb or rabbitmq state from a node path onto the claim 0.3.36 expects (#177).

Up to and including 0.3.35, both charts kept their state on a hostPath: mariadb at
/tmp/mysql-data, rabbitmq at /var/lib/rabbitmq. 0.3.36 gives both StatefulSets
volumeClaimTemplates instead. Kubernetes refuses that change on an existing StatefulSet. The
composition-dynamic-controller then patches the REST of the chart and leaves the StatefulSet
as it was, retrying forever with Ready=Unknown. Measured on GKE, Kubernetes 1.36.4, cdc 2.13.71.
A hook cannot help either, because it runs after the change. So this step has to happen before
the upgrade, outside the chart.

Two phases, run on either side of the upgrade:

  prepare   pause the component's Composition, stop the StatefulSet, prove the node path holds
            state, copy it into a claim named exactly as the new StatefulSet will claim it,
            verify the copy, then delete the old StatefulSet. The node path is left in place.
  finish    after a 0.3.38-or-later umbrella CompositionDefinition is applied: refuse unless the
            component's own CompositionDefinition renders a chart that claims the volume
            (hack/durable-state.yaml claimsSince), unpause the Composition, wait for the new
            StatefulSet to adopt the claim and for its pod to be Ready on that claim.

Without --apply each phase prints what it would do and changes nothing.

It refuses rather than guesses whenever the cluster is not in the state it expects. Each case:
no explicit --context; already migrated; more than one replica, because each pod's path is on
a different node; a pod that was never scheduled, so the node holding the state is unknown; a
node path without the component's marker file, which means there is nothing to copy and the
install needs a reinstall; a copy whose file count or byte total differs from the source.

Usage:
  hack/migrate-to-pvc.py prepare --context CTX --namespace openstack --component mariadb [--apply]
  # apply the 0.3.38-or-later umbrella CompositionDefinition, as in the README
  hack/migrate-to-pvc.py finish  --context CTX --namespace openstack --component mariadb [--apply]
"""
import argparse
import json
import os
import subprocess
import sys
import time

try:
    import yaml
except ImportError:
    sys.exit("ERROR: PyYAML is required")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# What proves a node path holds state rather than an empty directory. kind recreated
# /tmp/mysql-data as an empty directory after a node restart, holding only .cache, so the
# directory existing proves nothing.
MARKERS = {
    "mariadb": "grastate.dat",
    "rabbitmq": "mnesia",
}

# Measured on GKE, Kubernetes 1.36.4, cdc 2.13.72, one Composition, 60s resync. Unpaused, a deleted
# StatefulSet came back in 48.7s. With this annotation it stayed gone for the 150s watched, and
# the umbrella kept the annotation while it re-rendered the child's spec. With
# krateo.io/gracefully-paused it was recreated while paused: at 2.13.72 that key takes effect
# only once an Update pass has written krateo.io/gracefully-paused-time, so the first change
# still lands (core-provider#381). It cannot protect the copy. Full table on #177.
PAUSE_ANNOTATION = "krateo.io/paused"

COPY_POD_TIMEOUT = 1800
RESYNC_GUARD = 90
WAIT_STEP = 5


class Refusal(Exception):
    pass


def entry_for(component):
    listing = os.path.join(ROOT, "hack", "durable-state.yaml")
    for e in (yaml.safe_load(open(listing)) or {}).get("durable") or []:
        if e["component"] == component:
            return e
    raise Refusal(f"{component} is not listed in hack/durable-state.yaml")


class Kube:
    def __init__(self, context, namespace, apply):
        self.base = ["kubectl", "--context", context, "--namespace", namespace]
        self.namespace = namespace
        self.apply = apply

    def run(self, *args, stdin=None, check=True):
        p = subprocess.run(self.base + list(args), input=stdin, capture_output=True, text=True)
        if check and p.returncode:
            raise Refusal(f"kubectl {' '.join(args)}: {p.stderr.strip()}")
        return p

    def get(self, *args):
        p = self.run("get", *args, "-o", "json", check=False)
        if p.returncode:
            if "NotFound" in p.stderr or "not found" in p.stderr:
                return None
            raise Refusal(f"kubectl get {' '.join(args)}: {p.stderr.strip()}")
        return json.loads(p.stdout)

    def change(self, description, *args, stdin=None):
        print(("  DO   " if self.apply else "  PLAN ") + description)
        if self.apply:
            self.run(*args, stdin=stdin)


def semver_at_least(have, want):
    """True when release version `have` (X.Y.Z) is `want` or later. Anything else is False."""
    def parts(v):
        core = v.split("-", 1)[0].split("+", 1)[0].split(".")
        if len(core) != 3 or not all(c.isdigit() for c in core):
            return None
        return tuple(int(c) for c in core)
    h, w = parts(have), parts(want)
    if w is None:
        raise Refusal(f"claimsSince {want!r} in hack/durable-state.yaml is not X.Y.Z")
    return h is not None and h >= w


def composition(kube, component):
    found = kube.get(f"{component}s.composition.krateo.io")
    items = (found or {}).get("items") or []
    if len(items) != 1:
        raise Refusal(f"expected one {component} Composition in {kube.namespace}, "
                      f"found {len(items)}")
    return items[0]


def paused(comp):
    return (comp["metadata"].get("annotations") or {}).get(PAUSE_ANNOTATION) == "true"


def wait_for(description, predicate, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(WAIT_STEP)
    raise Refusal(f"timed out after {timeout}s waiting for {description}")


def helper_pod(name, node, image, volumes, mounts, script):
    """A pod pinned to `node` by affinity, not nodeName. A claim on a WaitForFirstConsumer class
    binds only once the scheduler has picked a node, and nodeName bypasses the scheduler."""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "labels": {
                "app.kubernetes.io/managed-by": "migrate-to-pvc",
            },
        },
        "spec": {
            "restartPolicy": "Never",
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "kubernetes.io/hostname",
                                        "operator": "In",
                                        "values": [node],
                                    },
                                ],
                            },
                        ],
                    },
                },
            },
            "tolerations": [
                {
                    "operator": "Exists",
                },
            ],
            "containers": [
                {
                    "name": "copy",
                    "image": image,
                    "command": ["/bin/sh", "-ec", script],
                    "securityContext": {
                        "runAsUser": 0,
                    },
                    "volumeMounts": mounts,
                },
            ],
            "volumes": volumes,
        },
    }


def run_helper(kube, pod, timeout):
    name = pod["metadata"]["name"]
    kube.run("delete", "pod", name, "--ignore-not-found", "--wait=true")
    kube.run("apply", "-f", "-", stdin=json.dumps(pod))

    def done():
        p = kube.get("pod", name)
        return p and p["status"].get("phase") in ("Succeeded", "Failed")

    wait_for(f"pod {name}", done, timeout)
    phase = kube.get("pod", name)["status"]["phase"]
    log = kube.run("logs", name, check=False).stdout
    kube.run("delete", "pod", name, "--wait=true")
    if phase != "Succeeded":
        raise Refusal(f"pod {name} ended {phase}:\n{log}")
    return log


# Inventory both sides the same way, so the comparison is between like numbers.
INVENTORY = ("printf '{p}files=%s\\n' \"$(find {d} -mindepth 1 | wc -l)\"; "
             "printf '{p}bytes=%s\\n' \"$(find {d} -type f -exec cat {{}} + | wc -c)\"")


def parse_inventory(text, prefix):
    out = {}
    for line in text.splitlines():
        if line.startswith(prefix):
            k, _, v = line[len(prefix):].partition("=")
            out[k] = int(v.strip())
    return out


def prepare(kube, component, entry, storage_class, size):
    name, volume = entry["statefulset"], entry["volume"]
    claim = f"{volume}-{name}-0"
    marker = MARKERS[component]

    comp = composition(kube, component)
    sts = kube.get("statefulset", name)
    if sts is None:
        if kube.get("pvc", claim):
            raise Refusal(f"StatefulSet {name} is gone and claim {claim} exists: prepare has "
                          "already run. Upgrade, then run finish.")
        raise Refusal(f"StatefulSet {name} does not exist and there is no claim {claim}")
    templates = [t["metadata"]["name"] for t in sts["spec"].get("volumeClaimTemplates") or []]
    if volume in templates:
        print(f"{name} already claims {volume}: nothing to migrate")
        return
    source = next((v for v in sts["spec"]["template"]["spec"].get("volumes") or []
                   if v["name"] == volume), None)
    if not source or "hostPath" not in source:
        raise Refusal(f"{name} volume {volume} is not a hostPath ({source}); this script moves "
                      "only the shape 0.3.35 shipped")
    path = source["hostPath"]["path"]
    replicas = sts["spec"].get("replicas", 1)
    if replicas != 1:
        raise Refusal(f"{name} has {replicas} replicas, so its state is on {replicas} nodes; "
                      "this script moves one")
    pod = kube.get("pod", f"{name}-0")
    node = (pod or {}).get("spec", {}).get("nodeName")
    if not node:
        raise Refusal(f"pod {name}-0 is not scheduled, so the node holding {path} is unknown")
    image = sts["spec"]["template"]["spec"]["containers"][0]["image"]

    print(f"{component}: {name} keeps {path} on node {node}; target claim {claim}")

    if not paused(comp):
        kube.change(f"pause Composition {comp['metadata']['name']} ({PAUSE_ANNOTATION})",
                    "annotate", f"{component}s.composition.krateo.io",
                    comp["metadata"]["name"], f"{PAUSE_ANNOTATION}=true", "--overwrite")
    kube.change(f"scale {name} to 0", "scale", "statefulset", name, "--replicas=0")
    if kube.apply:
        wait_for(f"pod {name}-0 to stop", lambda: kube.get("pod", f"{name}-0") is None, 600)
        # Measured on 2.13.72 with a 60s resync: unpaused, a manual scale to 0 was undone by the
        # next resync (0 for 45s, then 1), and under this pause it held for 150s. Waiting past
        # one resync turns a missing pause into a refusal here instead of a server starting
        # under the copy. A controller configured with a longer resync would still get past
        # this check, which is why the pause is set first and not relied on from here.
        time.sleep(RESYNC_GUARD)
        held = kube.get("statefulset", name)["spec"].get("replicas")
        if held != 0:
            raise Refusal(f"{name} was scaled back to {held} while paused: something is still "
                          "reconciling it, so the copy would race a running server")

    probe_volumes = [{"name": "src", "hostPath": {"path": path, "type": "Directory"}}]
    probe_mounts = [{"name": "src", "mountPath": "/src", "readOnly": True}]
    probe = helper_pod(f"{name}-migrate-probe", node, image, probe_volumes, probe_mounts,
                       f"test -e /src/{marker} || {{ echo 'no {marker} in {path}'; exit 3; }}; "
                       + INVENTORY.format(d="/src", p="src."))
    if not kube.apply:
        print(f"  PLAN prove {path} on {node} holds {marker}, and count its files and bytes")
        print(f"  PLAN create claim {claim} ({size}, class {storage_class or 'cluster default'})")
        print(f"  PLAN copy {path} into {claim} on {node}, and compare counts")
        print(f"  PLAN delete StatefulSet {name}; leave {path} on {node} in place")
        return
    try:
        src = parse_inventory(run_helper(kube, probe, 600), "src.")
    except Refusal as e:
        raise Refusal(f"{e}\nThe node path holds no {component} state, so there is nothing to "
                      "migrate: this install lost its data before this script ran. The "
                      f"Composition is left paused and {name} at 0.")
    print(f"  OK   {path} holds {marker}: {src['files']} entries, {src['bytes']} bytes")

    if kube.get("pvc", claim) is None:
        pvc = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": claim,
                "labels": {
                    "app.kubernetes.io/managed-by": "migrate-to-pvc",
                },
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {
                    "requests": {
                        "storage": size,
                    },
                },
            },
        }
        if storage_class:
            pvc["spec"]["storageClassName"] = storage_class
        kube.change(f"create claim {claim}", "apply", "-f", "-", stdin=json.dumps(pvc))

    copy_volumes = probe_volumes + [{"name": "dst", "persistentVolumeClaim": {"claimName": claim}}]
    copy_mounts = probe_mounts + [{"name": "dst", "mountPath": "/dst"}]
    script = ("if [ -n \"$(ls -A /dst | grep -v '^lost+found$')\" ]; then "
              "echo 'claim is not empty: a previous copy stopped part way. Delete the claim, "
              "then run prepare again'; exit 4; fi; "
              "cp -a /src/. /dst/; sync; "
              + INVENTORY.format(d="/dst", p="dst."))
    copy = helper_pod(f"{name}-migrate-copy", node, image, copy_volumes, copy_mounts, script)
    print(f"  DO   copy {path} into {claim}")
    dst = parse_inventory(run_helper(kube, copy, COPY_POD_TIMEOUT), "dst.")
    lost_found = 1 if dst["files"] == src["files"] + 1 else 0
    if dst["files"] - lost_found != src["files"] or dst["bytes"] != src["bytes"]:
        raise Refusal(f"copy differs: source {src}, claim {dst}. The StatefulSet is kept, at 0, "
                      "and the Composition paused, so nothing has been lost.")
    print(f"  OK   claim matches: {dst['files'] - lost_found} entries, {dst['bytes']} bytes")

    kube.change(f"delete StatefulSet {name}, keeping claim {claim}",
                "delete", "statefulset", name, "--wait=true")
    print(f"prepared. {path} stays on {node} until you delete it. Now apply a 0.3.38-or-later "
          "umbrella CompositionDefinition (0.3.36 and 0.3.37 leave the components behind, #179), "
          "then run finish.")


def finish(kube, component, entry):
    name, volume = entry["statefulset"], entry["volume"]
    claim = f"{volume}-{name}-0"
    comp = composition(kube, component)
    if kube.get("pvc", claim) is None:
        raise Refusal(f"claim {claim} does not exist: run prepare first")
    if kube.get("statefulset", name) is not None:
        sts = kube.get("statefulset", name)
        templates = [t["metadata"]["name"] for t in sts["spec"].get("volumeClaimTemplates") or []]
        if volume not in templates:
            raise Refusal(f"{name} exists again without a claim template: something recreated "
                          "it from the old chart while it was meant to be paused")
    # What decides the chart this Composition is rendered from is its component
    # CompositionDefinition's spec.chart.version, so that is what is checked. Not the
    # krateo.io/composition-version label: on an umbrella install it alternates between the
    # version the Composition was created at (the umbrella writes it through that pinned API
    # version) and the new one (the migration writes it through the new API version), about once
    # a minute. Measured on GKE, Kubernetes 1.36.4, core-provider 2.13.72, upgrading 0.3.34 to
    # 0.3.38 (#179). A check on that label passes or refuses depending on the second it runs.
    cd = kube.get("compositiondefinitions.core.krateo.io", component)
    if cd is None:
        raise Refusal(f"no CompositionDefinition {component} in {kube.namespace}: the umbrella "
                      "registers one per component, so this is not an umbrella install")
    have = str(((cd.get("spec") or {}).get("chart") or {}).get("version") or "")
    since = str(entry["claimsSince"])
    if not semver_at_least(have, since):
        raise Refusal(f"CompositionDefinition {component} renders chart {have or '(none)'}, which "
                      f"keeps the state on the node path; claims start at {since}. Apply a "
                      f"{since}-or-later umbrella CompositionDefinition and wait for this one to "
                      "follow. If the umbrella is newer and this stays, see the README section "
                      "\"Components follow the umbrella's version\".")

    if paused(comp):
        kube.change(f"unpause Composition {comp['metadata']['name']}",
                    "annotate", f"{component}s.composition.krateo.io",
                    comp["metadata"]["name"], f"{PAUSE_ANNOTATION}-")
    if not kube.apply:
        print(f"  PLAN wait for {name}-0 Ready on claim {claim}")
        return

    def ready():
        p = kube.get("pod", f"{name}-0")
        if not p:
            return False
        claims = [v["persistentVolumeClaim"]["claimName"] for v in p["spec"].get("volumes") or []
                  if "persistentVolumeClaim" in v]
        conds = {c["type"]: c["status"] for c in p["status"].get("conditions") or []}
        return claim in claims and conds.get("Ready") == "True"

    wait_for(f"{name}-0 Ready on claim {claim}", ready, 1800)
    print(f"finished. {name}-0 is Ready on claim {claim}.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("phase", choices=["prepare", "finish"])
    ap.add_argument("--context", required=True,
                    help="kubeconfig context; required so the shared current context is never used")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--component", required=True, choices=sorted(MARKERS))
    ap.add_argument("--storage-class", default="",
                    help="must match componentValues.<component>.volume.class_name; empty means "
                         "the cluster default, as the chart's `default` does")
    ap.add_argument("--size", help="defaults to the chart's volume.size")
    ap.add_argument("--apply", action="store_true", help="make the changes; otherwise only plan")
    a = ap.parse_args()
    entry = entry_for(a.component)
    size = a.size or yaml.safe_load(open(os.path.join(
        ROOT, "blueprints", a.component, "chart", "values.yaml")))["volume"]["size"]
    kube = Kube(a.context, a.namespace, a.apply)
    try:
        if a.phase == "prepare":
            prepare(kube, a.component, entry, a.storage_class, size)
        else:
            finish(kube, a.component, entry)
    except Refusal as e:
        sys.exit(f"REFUSED: {e}")


if __name__ == "__main__":
    main()
