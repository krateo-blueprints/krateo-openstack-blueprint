#!/usr/bin/env python3
"""No Service may select a hostNetwork pod (#200).

A hostNetwork pod's IP is its node's IP. A Service that selects it therefore lists that node IP as
an endpoint, and cluster DNS (kube-dns, behind node-local-dns on GKE) publishes a reverse record
for it: <node-ip-dashed>.<service>.<namespace>.svc.cluster.local. Every hostNetwork pod on that
node with dnsPolicy ClusterFirstWithHostNet then resolves its own node IP to that name, so
socket.getfqdn() answers the Service name instead of the node's.

Measured on GKE (Kubernetes 1.36.4, 0.3.43, profile full-strict): the node running
nova-novncproxy answered `<ip>.nova-novncproxy.openstack.svc.cluster.local` for its own IP, the
other five nodes answered their GCE FQDN. nova-compute (health probe --use-fqdn), the neutron dhcp
and l3 agents and cyborg-agent never became Ready on that node. nova-novncproxy was the only
Service in the cluster with a node IP as an endpoint.

Each component chart is rendered with its defaults under a Krateo-style release name. A Service
fails when its selector matches the pod-template labels of a Deployment, StatefulSet, DaemonSet or
Job that sets hostNetwork: true in the same chart. Fails when no chart is found, when a chart does
not render, or when no Service or no pod template renders at all.

Usage: hack/validate-hostnetwork-services.py [repo-root] [component ...]
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

WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet", "Job")

# A Service that genuinely has to front a hostNetwork pod would go here, with the reason and the
# measurement showing that no host-network agent on those nodes resolves its FQDN through cluster
# DNS. Empty: nothing in this repository needs one.
ALLOWED = {}


def pod_templates(docs):
    for d in docs:
        if d.get("kind") in WORKLOADS:
            tpl = (d.get("spec") or {}).get("template") or {}
            labels = (tpl.get("metadata") or {}).get("labels") or {}
            host = bool((tpl.get("spec") or {}).get("hostNetwork"))
            yield d["kind"], d["metadata"]["name"], labels, host


def problems(component, docs):
    out = []
    tpls = list(pod_templates(docs))
    services = [d for d in docs if d.get("kind") == "Service"]
    for svc in services:
        sel = (svc.get("spec") or {}).get("selector") or {}
        if not sel:
            continue
        name = svc["metadata"]["name"]
        for kind, wname, labels, host in tpls:
            if host and all(labels.get(k) == v for k, v in sel.items()):
                if ALLOWED.get((component, name)):
                    continue
                out.append(f"{component}: Service/{name} selects {kind}/{wname}, which is hostNetwork: its "
                           "node's IP becomes a Service endpoint, and cluster DNS then answers the node's "
                           "own reverse lookup with the Service name, so getfqdn() on that node is wrong (#200)")
    return out, len(services), len(tpls)


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
    failures, unrendered, n_svc, n_tpl = [], [], 0, 0
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        toolkit = root / "charts" / "helm-toolkit"
        for chart in charts:
            component = chart.parent.name
            try:
                docs = job_hooks.render(chart, toolkit, component, work / "r", [])
            except Exception as e:  # a chart that does not render measured nothing
                unrendered.append(f"{component}: {str(e)[:300]}")
                continue
            found, s, t = problems(component, docs)
            n_svc += s
            n_tpl += t
            failures += found
            print(f"{'FAIL' if found else 'OK  '} {component}: {s} Services, {t} pod templates")
    for u in unrendered:
        print(f"FAIL {u}: did not render, so nothing was measured")
    if n_svc == 0 or n_tpl == 0:
        print(f"FAIL: rendered {n_svc} Services and {n_tpl} pod templates -- nothing was measured")
        return 1
    for f in failures:
        print(f"FAIL {f}")
    if failures or unrendered:
        print(f"FAIL: {len(failures)} problem(s), {len(unrendered)} unrendered, across {len(charts)} charts")
        return 1
    print(f"OK: {len(charts)} charts, {n_svc} Services, {n_tpl} pod templates, no Service selects a hostNetwork pod")
    return 0


if __name__ == "__main__":
    sys.exit(main())
