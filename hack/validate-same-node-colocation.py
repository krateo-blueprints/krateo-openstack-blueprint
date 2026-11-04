#!/usr/bin/env python3
"""A requireSameNode dependency must be satisfiable by the node labels we tell operators to use.

kubernetes-entrypoint's DEPENDENCY_POD_JSON can require a dependency pod **on the same node**.
If the waiting workload and the required workload are scheduled by DIFFERENT node labels, the
requirement is satisfiable only on nodes that happen to carry both -- and unsatisfiable, forever
and silently, on any node that carries just one.

Measured on a 4-node GKE install of profile=full:

    nodes with openstack-control-plane=enabled   4   default-pool    <- dhcp/l3/metadata here
    nodes with openvswitch=enabled               2   ubuntu-compute  <- ovs-agent here

Disjoint. So all twelve neutron dhcp/l3/metadata pods waited forever for a co-located
neutron-ovs-agent that is on another pool by construction. They sit in Init:0/2 -- not Pending,
not CrashLoopBackOff -- so a pod census shows "19 in Init" and names nothing, and neutron's
Composition reports "3 of 73 managed children are not ready" indefinitely.

The charts are upstream-correct: upstream schedules the agents by openstack-control-plane and the
ovs agent by openvswitch, and upstream's reference deployment carries BOTH labels on the same
nodes. Nothing in the charts says so. Our README described the four labels as four roles, which
reads as "label the nodes that play that role" -- and the peer who built that cluster put
openvswitch on the Ubuntu pool for a real reason: COS ships no openvswitch kernel module, so the
compute tier could not run anywhere else. The disjointness was a correct decision against an
incomplete precondition.

hack/validate-node-label-preconditions.py asserts the four labels EXIST. That is not the
precondition. The precondition is a RELATIONSHIP between node sets, and this gate asserts that
every same-node pair whose two halves are scheduled by different labels is declared, with the
relation it needs, in hack/node-colocation.yaml -- so a new one cannot appear without someone
writing down what operators must do about it.

Derived from the render rather than from a list: a new chart that introduces a same-node
dependency is caught here instead of on someone's cluster.

THE DECLARATION IS KEYED ON THE NODE SELECTORS TOO, and that is not decoration. A matcher has
two independent properties -- correct and unambiguous -- and the first version of this gate only
had the first. Its key was (component, waiter, target), which drops both node selectors, so
changing neutron-dhcp-agent's nodeSelector would leave the derived pair unchanged, still
declared, still passing -- while the requiredRelation text named labels that were no longer the
ones in play. The declaration would go stale and the gate would say OK. Four break tests all
asked whether the gate fires on an undeclared pair; none asked whether a declaration could keep
matching something it no longer describes.

So an entry must name the labels the render actually found, and a selector change fails until
the relation is rewritten for the labels that now apply.
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    sys.exit("FAILED MEASUREMENT: PyYAML unavailable, so no chart was inspected")

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 \
    else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECL = os.path.join(ROOT, "hack", "node-colocation.yaml")


def render(chart_dir):
    tmp = tempfile.mkdtemp()
    try:
        dst = os.path.join(tmp, "c")
        shutil.copytree(chart_dir, dst)
        cy = os.path.join(dst, "Chart.yaml")
        meta = open(cy).read()
        open(cy, "w").write(meta.replace("CHART_VERSION", "0.0.0-gate"))
        tk = os.path.join(ROOT, "charts", "helm-toolkit")
        if os.path.isdir(tk):
            os.makedirs(os.path.join(dst, "charts"), exist_ok=True)
            shutil.copytree(tk, os.path.join(dst, "charts", "helm-toolkit"))
        p = subprocess.run(["helm", "template", "gate", dst], capture_output=True, text=True)
        if p.returncode:
            return None
        return [x for x in yaml.safe_load_all(p.stdout) if isinstance(x, dict)]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet")


def pairs_in(docs):
    """(waiter, its nodeSelector, target, target's nodeSelector) for every requireSameNode dep."""
    index = []
    for x in docs:
        if x.get("kind") not in WORKLOADS:
            continue
        tpl = x["spec"]["template"]
        index.append((x["metadata"]["name"],
                      tpl.get("metadata", {}).get("labels") or {},
                      tpl["spec"].get("nodeSelector") or {}))
    out = []
    for x in docs:
        if x.get("kind") not in WORKLOADS:
            continue
        spec = x["spec"]["template"]["spec"]
        sel = spec.get("nodeSelector") or {}
        for c in (spec.get("initContainers") or []):
            for e in (c.get("env") or []):
                if e.get("name") != "DEPENDENCY_POD_JSON" or not e.get("value"):
                    continue
                try:
                    reqs = json.loads(e["value"])
                except (ValueError, TypeError):
                    # A malformed value is a finding, not something to skip past: the init
                    # container would wait on a dependency nobody can read.
                    out.append((x["metadata"]["name"], sel, "<unparseable DEPENDENCY_POD_JSON>", {}))
                    continue
                if not isinstance(reqs, list):
                    continue
                for req in reqs:
                    if not isinstance(req, dict) or not req.get("requireSameNode"):
                        continue
                    want = req.get("labels") or {}
                    for name, labels, tsel in index:
                        if want and all(labels.get(k) == v for k, v in want.items()):
                            out.append((x["metadata"]["name"], sel, name, tsel))
    return out


def main():
    if not os.path.isfile(DECL):
        sys.exit(f"FAIL: missing declaration {DECL} -- this gate refuses to run without it")
    decl = yaml.safe_load(open(DECL)) or {}
    declared = {}
    for entry in (decl.get("colocations") or []):
        for k in ("component", "waiter", "target", "waiterSelector", "targetSelector",
                  "requiredRelation", "why"):
            if not entry.get(k):
                sys.exit(f"FAIL: a colocation entry is missing {k!r}: {entry}")
        for k in ("waiterSelector", "targetSelector"):
            if not isinstance(entry[k], dict):
                sys.exit(f"FAIL: {k!r} must be the nodeSelector mapping, not {entry[k]!r}")
        declared[(entry["component"], entry["waiter"], entry["target"],
                  tuple(sorted(entry["waiterSelector"].items())),
                  tuple(sorted(entry["targetSelector"].items())))] = entry

    bp = os.path.join(ROOT, "blueprints")
    charts = sorted(d for d in os.listdir(bp)
                    if os.path.isdir(os.path.join(bp, d, "chart")))
    if not charts:
        sys.exit("FAIL: no charts under blueprints/ -- nothing was measured")

    found, problems, rendered = [], [], 0
    for c in charts:
        docs = render(os.path.join(bp, c, "chart"))
        if docs is None:
            continue
        rendered += 1
        for waiter, wsel, target, tsel in pairs_in(docs):
            found.append((c, waiter, target))
            if wsel == tsel:
                continue  # same label, satisfiable by construction
            key = (c, waiter, target,
                   tuple(sorted(wsel.items())), tuple(sorted(tsel.items())))
            if key not in declared:
                # Distinguish "never declared" from "declared for different labels", because the
                # second is a stale declaration describing a relation that no longer applies and
                # it is the one the old key could not see.
                stale = [d for d in declared if d[:3] == (c, waiter, target)]
                if stale:
                    was = declared[stale[0]]
                    problems.append(
                        f"{c}: {waiter} -> {target} is declared, but for different node labels. "
                        f"The render now has waiter={wsel} target={tsel}; the declaration says "
                        f"waiter={was['waiterSelector']} target={was['targetSelector']}. Its "
                        f"requiredRelation names labels that are no longer the ones in play, so "
                        f"rewrite the relation for the labels that now apply."
                    )
                else:
                    problems.append(
                        f"{c}: {waiter} (nodeSelector {wsel}) requires a co-located {target} "
                        f"(nodeSelector {tsel}), and the two labels differ -- so the requirement "
                        f"is unsatisfiable on any node carrying only one of them. Declare the "
                        f"relation operators must satisfy in hack/node-colocation.yaml."
                    )

    if rendered == 0:
        sys.exit("FAIL: no chart rendered, so no same-node dependency was inspected")
    stale = [k for k in declared if k[:3] not in set(found)]
    if stale:
        problems.append(
            "declared colocations that no longer exist in any render: "
            + ", ".join(f"{a}:{b}->{c}" for a, b, c in sorted(stale))
            + " -- remove them, or the declaration stops describing the charts"
        )

    if problems:
        print(f"FAIL: {len(problems)} problem(s) with same-node dependencies "
              f"({len(found)} found across {rendered} charts)")
        for p in sorted(set(problems)):
            print("  " + p)
        return 1
    print(f"OK: {len(found)} requireSameNode dependencies across {rendered} charts; every pair "
          "scheduled by differing node labels is declared with the relation operators must meet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
