#!/usr/bin/env python3
"""Every container the blueprint renders requests CPU and memory and has a memory limit (#283).

    hack/validate-pod-resources.py [repo-root] [chart ...]

OpenStack-Helm ships `pod.resources.enabled: false`, and on main 33 of the 36 component charts kept it. With it off no
container renders `resources`, so every pod is BestEffort: the scheduler packs nodes by requests of zero, a capacity
total built on requests measures nothing, and under memory pressure the kubelet evicts MariaDB, RabbitMQ and the APIs
before anything else. A ResourceQuota on cpu or memory (a trusted-tenant pool) rejects such a pod outright, init
containers included. Checked here, for every component chart:

  1. EVERY CONTAINER IS SIZED. The chart is rendered at its defaults and with every workload manifest it ships turned
     on (each alone where turning all on does not render). Every container and init container of every Deployment,
     StatefulSet, DaemonSet, Job, CronJob and Pod sets requests.cpu, requests.memory and limits.memory, with the limit
     not below the request.
  2. NO CPU LIMIT BY DEFAULT. CPU is compressible: a CPU limit throttles an API worker for the rest of a 100 ms CFS
     period even on an idle node, which shows up as latency and probe timeouts, never as a clear failure. The defaults
     carry none; an operator who wants one sets it through componentValues.<component>.pod.resources.
  3. THE SWITCH IS THE CHART'S OWN. values.yaml sets pod.resources.enabled: true, so the umbrella's componentValues can
     turn it off or resize any one container with OpenStack-Helm's own keys.

  4. FEATURES ARE RENDERED TOO. A workload behind a switch that is off by default (keystone's and nova's KOG
     credential Jobs, libvirt's kvm-check init container) is rendered from hack/feature-renders.yaml; an entry that
     fails to render, or adds no container, fails.

cinder and manila refuse to render with no backend chosen (#196, #426), so every render here carries
hack/gate_values.py's values, as the other gates that render component charts do: what the umbrella delivers
cinder with storage.backend ceph-external, and manila's documented operator path
(hack/share-backend-operator-example.yaml). Every other chart renders with no extra values.

Fails when helm or PyYAML is missing, when a chart named on the command line does not exist, when no chart rendered and
when no container was checked. Naming charts limits the run to them (a local check); CI runs it with none, over all.
"""
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
ONLY = sys.argv[2:]
BP = os.path.join(ROOT, "blueprints")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)

WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod")
UNITS = {"Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40, "k": 10 ** 3, "M": 10 ** 6, "G": 10 ** 9,
         "T": 10 ** 12, "m": 10 ** -3}
failures = []
FAILING = set()
CHECKED = []
try:
    FEATURES = yaml.safe_load(open(os.path.join(ROOT, "hack", "feature-renders.yaml"))) or {}
except OSError as e:
    print("FAILED MEASUREMENT: cannot read hack/feature-renders.yaml: %s" % e)
    sys.exit(2)


# What the umbrella delivers for a chart's storage (#308): cinder's default refuses with no volume backend (#196) and
# manila's with no share backend (#426), so a chart render carries hack/gate_values.py's arguments (nothing for every
# other chart), as every other gate that renders component charts does.
import importlib.util as _gv_util
import os as _gv_os
_gv_spec = _gv_util.spec_from_file_location(
    "gate_values", _gv_os.path.join(_gv_os.path.dirname(_gv_os.path.abspath(__file__)), "gate_values.py"))
gate_values = _gv_util.module_from_spec(_gv_spec)
_gv_spec.loader.exec_module(gate_values)


def fail(m, chart):
    failures.append(m)
    FAILING.add(chart)


def quantity(q):
    m = re.fullmatch(r"([0-9.]+)([A-Za-z]*)", str(q))
    if not m or (m.group(2) and m.group(2) not in UNITS):
        return None
    return float(m.group(1)) * (UNITS[m.group(2)] if m.group(2) else 1)


def stage(chart_dir, tmp, name):
    w = os.path.join(tmp, name)
    shutil.copytree(chart_dir, w)
    cy = os.path.join(w, "Chart.yaml")
    txt = open(cy).read()
    open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
    if "helm-toolkit" in txt:
        os.makedirs(os.path.join(w, "charts"), exist_ok=True)
        shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
    return w


def helm_template(chart, release, values):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values, f)
        vf = f.name
    try:
        r = subprocess.run(["helm", "template", release, chart, "--namespace", "openstack", "--kube-version", "1.36.0",
                            *gate_values.helm_args(chart), "-f", vf], capture_output=True, text=True)
    finally:
        os.unlink(vf)
    return r.returncode, r.stdout, r.stderr


def pod_templates(docs):
    for d in docs:
        if d.get("kind") not in WORKLOADS:
            continue
        spec = d.get("spec") or {}
        if d["kind"] == "Pod":
            pod = spec
        elif d["kind"] == "CronJob":
            pod = (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec")
        else:
            pod = (spec.get("template") or {}).get("spec")
        if isinstance(pod, dict):
            yield "%s/%s" % (d["kind"], d["metadata"]["name"]), pod


def manifest_toggles(values):
    m = (values or {}).get("manifests") or {}
    return sorted(k for k, v in m.items()
                  if v is False and re.match(r"(deployment|daemonset|statefulset|job|cron_job|pod)_", k))


def check_container(chart, label, workload, c, seen):
    name = "%s %s container %s" % (workload, "init" if c.get("_init") else "", c.get("name"))
    name = re.sub(r"\s+", " ", name)
    key = (workload, c.get("name"), bool(c.get("_init")))
    if key in seen:
        return
    seen.add(key)
    CHECKED.append((chart, workload, c.get("name")))
    res = c.get("resources") or {}
    req = res.get("requests") or {}
    lim = res.get("limits") or {}
    missing = [k for k, v in (("requests.cpu", req.get("cpu")), ("requests.memory", req.get("memory")),
                              ("limits.memory", lim.get("memory"))) if v in (None, "")]
    if missing:
        fail("1: %s (%s): %s sets no %s" % (chart, label, name, ", ".join(missing)), chart)
        return
    rq, lm = quantity(req["memory"]), quantity(lim["memory"])
    if rq is None or lm is None:
        fail("1: %s (%s): %s has a memory quantity this gate cannot read: request %s, limit %s"
             % (chart, label, name, req["memory"], lim["memory"]), chart)
    elif lm < rq:
        fail("1: %s (%s): %s limits memory to %s, below its request %s" % (chart, label, name, lim["memory"],
                                                                         req["memory"]), chart)
    if "cpu" in lim:
        fail("2: %s (%s): %s carries a default CPU limit (%s); CPU throttling is not a default" % (
            chart, label, name, lim["cpu"]), chart)


def chart_check(chart, tmp):
    cdir = os.path.join(BP, chart, "chart")
    cv = yaml.safe_load(open(os.path.join(cdir, "values.yaml"))) or {}
    enabled = (((cv.get("pod") or {}).get("resources") or {}).get("enabled"))
    if enabled is not True:
        fail("3: %s: values.yaml sets pod.resources.enabled to %r, so no container renders requests" % (chart, enabled), chart)
    w = stage(cdir, tmp, chart)
    toggles = manifest_toggles(cv)
    renders = [("defaults", {})]
    if toggles:
        allon = {"manifests": {k: True for k in toggles}}
        rc, _, _ = helm_template(w, chart + "-gate", allon)
        if rc == 0:
            renders.append(("every workload manifest on", allon))
        else:
            renders.extend(("manifests.%s on" % k, {"manifests": {k: True}}) for k in toggles)
    features = sorted((FEATURES.get(chart) or {}).items())
    renders.extend(features)
    rendered = 0
    seen = set()
    for label, values in renders:
        rc, out, err = helm_template(w, chart + "-gate", values)
        if rc != 0:
            if label == "defaults":
                fail("1: %s: does not render at its defaults: %s" % (chart, err.strip()[:300]), chart)
            elif label in dict(features):
                fail("4: %s: hack/feature-renders.yaml's %r does not render: %s" % (chart, label, err.strip()[:300]),
                     chart)
            continue
        rendered += 1
        before = len(seen)
        for workload, pod in pod_templates([d for d in yaml.safe_load_all(out) if d]):
            for c in pod.get("initContainers") or []:
                check_container(chart, label, workload, dict(c, _init=True), seen)
            for c in pod.get("containers") or []:
                check_container(chart, label, workload, c, seen)
        if label in dict(features) and len(seen) == before:
            fail("4: %s: hack/feature-renders.yaml's %r renders no container the other renders lack; drop or fix the "
                 "entry" % (chart, label), chart)
    return rendered


def main():
    charts = sorted(d for d in os.listdir(BP) if d != "openstack" and os.path.isdir(os.path.join(BP, d, "chart")))
    if not charts:
        print("FAILED MEASUREMENT: no component chart found")
        sys.exit(2)
    unknown = sorted(set(ONLY) - set(charts))
    if unknown:
        print("FAILED MEASUREMENT: no such chart(s): %s" % ", ".join(unknown))
        sys.exit(2)
    run = ONLY or charts
    with tempfile.TemporaryDirectory() as tmp:
        rendered = sum(chart_check(c, tmp) for c in run)
    if rendered == 0 or not CHECKED:
        print("FAILED MEASUREMENT: no chart rendered a container, so nothing was checked")
        sys.exit(2)
    if failures:
        for f in failures:
            print("FAIL: " + f)
        unsized = sum(1 for f in failures if f.startswith("1:") and " sets no " in f)
        print("SUMMARY: %d chart(s) checked, %d container(s), %d without requests or a memory limit, %d failing chart(s)"
              % (len(run), len(CHECKED), unsized, len(FAILING)))
        sys.exit(1)
    print("OK: %d chart(s), %d render(s), %d container(s): each requests cpu and memory, has a memory limit not below "
          "its request, and no default CPU limit" % (len(run), rendered, len(CHECKED)))


main()
