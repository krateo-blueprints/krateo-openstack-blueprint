#!/usr/bin/env python3
"""A rollout, a drain or a lost node never takes an API down (#286).

    hack/validate-api-availability.py [repo-root] [chart ...]

Measured on GKE, 2026-11-16, across a 0.3.67 -> 0.3.68 upgrade: keystone token issue failed 9 of 76 samples, placement
3, orchestration 2, metric 1. keystone-api ran 1 replica with rolling_update {max_surge: 3, max_unavailable: 1}, so the
Deployment deleted the old pod while the new one was still Pending. 29 of the 30 charts with an API replica setting
defaulted it to 1, and every PodDisruptionBudget rendered minAvailable: 0. Checked here, for every component chart,
rendered at its defaults and with every workload manifest it ships turned on (each alone where all on does not render):

  1. EVERY SERVING DEPLOYMENT KEEPS ITS DESIRED COUNT THROUGH A ROLLOUT. A Deployment whose pods a Service in the same
     render selects is serving traffic, whatever its name (an API, a dashboard, a console proxy, a DNS server). It
     renders replicas >= 2 and RollingUpdate with maxUnavailable 0 and maxSurge >= 1, so a new pod is Ready before an
     old one goes.
  2. A ReadWriteOnce CLAIM PINS A DEPLOYMENT TO ONE POD, AND THAT POD MUST NOT SURGE. A Deployment that mounts a claim
     the chart renders ReadWriteOnce (or one the chart does not render, whose mode is unknown) attaches it on one node
     only: a second replica, or a surge pod, scheduled to another node waits on Multi-Attach forever, and with
     maxUnavailable 0 the old pod is never deleted, so the rollout stalls. Such a Deployment renders replicas 1 and
     either Recreate or maxUnavailable >= 1. This is the only way a serving Deployment escapes check 1, it is decided by
     the render (no list), and it is printed so it stays visible: a shared backend (ReadWriteMany, or object storage,
     #308) lifts it.
  3. EVERY MULTI-REPLICA WORKLOAD HAS A DISRUPTION BUDGET THAT ALLOWS A DRAIN. Each Deployment or StatefulSet that
     renders replicas > 1 is selected by a PodDisruptionBudget whose budget keeps at least one pod (minAvailable >= 1,
     or maxUnavailable < replicas) and still lets one go (minAvailable < replicas, or maxUnavailable >= 1): a budget
     equal to the replica count blocks every node drain.
  4. EVERY MULTI-REPLICA WORKLOAD SPREADS. Its pod template carries pod anti-affinity or a topology spread constraint
     over kubernetes.io/hostname and over topology.kubernetes.io/zone, selecting its own pods. A preferred term is
     enough: on a single node, or nodes with no zone label, it schedules anyway.
  5. THE SCHEMA AGREES WITH THE CHART, AND THE CHART READS ITS OWN KEY. Every Deployment and StatefulSet renders an
     integer replica count: `replicas: null` means its template reads a pod.replicas key values.yaml does not define
     (watcher's decision engine read pod.replicas.engine, while values.yaml and the schema offer decision_engine), so
     no override reaches it. Every pod.replicas.<key> default a component's values.schema.json declares equals
     values.yaml's: that schema becomes the Composition CRD, and the API server applies its defaults.

The one exception to check 1 is hack/single-replica-serving.yaml: a serving Deployment whose second replica would be a
second, unreplicated copy of its data. Each entry carries a reason and an issue, and an entry that no longer names a
single-replica serving Deployment fails.

Fails when helm or PyYAML is missing, when a chart named on the command line does not exist, when no chart rendered and
when no Deployment or StatefulSet was checked; over all charts, also when no serving Deployment was found. Naming
charts limits the run to them (a local check); CI runs it with none, over all.
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
ONLY = sys.argv[2:]
BP = os.path.join(ROOT, "blueprints")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)

HOST = "kubernetes.io/hostname"
ZONE = "topology.kubernetes.io/zone"
failures = []
FAILING = set()
SERVING = {}
PINNED = {}
MULTI = {}
WORKLOADS = []
EXEMPT_USED = set()
COUNT = {"under2": set(), "surge": set(), "nopdb": set(), "nospread": set()}
try:
    EXEMPT = yaml.safe_load(open(os.path.join(ROOT, "hack", "single-replica-serving.yaml"))) or {}
except OSError as e:
    print("FAILED MEASUREMENT: cannot read hack/single-replica-serving.yaml: %s" % e)
    sys.exit(2)


def fail(m, chart, kind=None, name=None):
    failures.append(m)
    FAILING.add(chart)
    if kind:
        COUNT[kind].add((chart, name))


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
                            "-f", vf], capture_output=True, text=True)
    finally:
        os.unlink(vf)
    return r.returncode, r.stdout, r.stderr


def manifest_toggles(values):
    m = (values or {}).get("manifests") or {}
    return sorted(k for k, v in m.items() if v is False and re.match(r"(deployment|statefulset|service|pdb|pvc)_", k))


def selects(selector, labels):
    """A label selector (matchLabels plus matchExpressions, or a Service's plain map) matches these labels."""
    if not selector:
        return False
    if "matchLabels" in selector or "matchExpressions" in selector:
        ml = selector.get("matchLabels") or {}
        exprs = selector.get("matchExpressions") or []
    else:
        ml, exprs = selector, []
    if not ml and not exprs:
        return False
    if any(labels.get(k) != v for k, v in ml.items()):
        return False
    for e in exprs:
        op, key, vals = e.get("operator"), e.get("key"), e.get("values") or []
        if op == "In" and labels.get(key) not in vals:
            return False
        if op == "NotIn" and labels.get(key) in vals:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


def budget(value, replicas):
    if isinstance(value, str) and value.endswith("%"):
        return int(replicas * int(value[:-1]) / 100)
    return int(value)


def spreads(pod, labels, key):
    aff = ((pod.get("affinity") or {}).get("podAntiAffinity") or {})
    terms = [t.get("podAffinityTerm") or {} for t in aff.get("preferredDuringSchedulingIgnoredDuringExecution") or []]
    terms += aff.get("requiredDuringSchedulingIgnoredDuringExecution") or []
    for t in terms:
        if t.get("topologyKey") == key and selects(t.get("labelSelector") or {}, labels):
            return True
    for c in pod.get("topologySpreadConstraints") or []:
        if c.get("topologyKey") == key and selects(c.get("labelSelector") or {}, labels):
            return True
    return False


def check_render(chart, label, docs):
    services = [d for d in docs if d.get("kind") == "Service"]
    pdbs = [d for d in docs if d.get("kind") == "PodDisruptionBudget"]
    claims = {d["metadata"]["name"]: (d.get("spec") or {}).get("accessModes") or []
              for d in docs if d.get("kind") == "PersistentVolumeClaim"}
    for d in docs:
        kind = d.get("kind")
        if kind not in ("Deployment", "StatefulSet"):
            continue
        name = d["metadata"]["name"]
        spec = d.get("spec") or {}
        pod = (spec.get("template") or {}).get("spec") or {}
        labels = ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}
        replicas = spec.get("replicas")
        ident = "%s/%s" % (kind, name)
        where = "%s (%s): %s" % (chart, label, ident)
        WORKLOADS.append((chart, ident))
        if not isinstance(replicas, int):
            fail("5: %s renders replicas: %r; its template reads a pod.replicas key values.yaml does not define, so no "
                 "override reaches it" % (where, replicas), chart)
            replicas = 1
        if kind == "Deployment":
            strategy = spec.get("strategy") or {}
            stype = strategy.get("type", "RollingUpdate")
            ru = strategy.get("rollingUpdate") or {}
            mu = ru.get("maxUnavailable", "25%")
            ms = ru.get("maxSurge", "25%")
            mu_n = budget(mu, replicas) if stype == "RollingUpdate" else replicas
            ms_n = -(-int(str(ms)[:-1]) * replicas // 100) if isinstance(ms, str) and ms.endswith("%") else int(ms)
            rwo = sorted(v["persistentVolumeClaim"]["claimName"] for v in pod.get("volumes") or []
                         if "persistentVolumeClaim" in v
                         and "ReadWriteMany" not in claims.get(v["persistentVolumeClaim"]["claimName"], []))
            serving = any(selects((s.get("spec") or {}).get("selector") or {}, labels) for s in services)
            if rwo:
                PINNED[(chart, ident)] = rwo
                if replicas != 1:
                    fail("2: %s mounts ReadWriteOnce claim(s) %s and renders %s replicas; a pod on a second node waits "
                         "on Multi-Attach" % (where, rwo, replicas), chart)
                if stype == "RollingUpdate" and mu_n < 1:
                    fail("2: %s mounts ReadWriteOnce claim(s) %s with maxUnavailable %s: a surge pod on another node "
                         "waits on Multi-Attach and the old pod is never deleted. Use Recreate or maxUnavailable 1"
                         % (where, rwo, mu), chart)
            elif serving and name in (EXEMPT.get(chart) or {}):
                entry = EXEMPT[chart][name] or {}
                EXEMPT_USED.add((chart, name))
                if not entry.get("reason") or not entry.get("issue"):
                    fail("1: %s is exempted by hack/single-replica-serving.yaml without a reason and an issue" % where,
                         chart)
                if replicas != 1:
                    fail("1: %s is exempted as single-replica by hack/single-replica-serving.yaml and renders %s; "
                         "drop the entry" % (where, replicas), chart)
            elif serving:
                SERVING[(chart, ident)] = replicas
                if replicas < 2:
                    fail("1: %s is selected by a Service and renders %s replica(s); want >= 2" % (where, replicas),
                         chart, "under2", ident)
                if stype != "RollingUpdate" or mu_n != 0 or ms_n < 1:
                    fail("1: %s is selected by a Service and rolls with %s maxUnavailable %s maxSurge %s; want "
                         "RollingUpdate, maxUnavailable 0, maxSurge >= 1" % (where, stype, mu, ms), chart, "surge",
                         ident)
        if replicas > 1:
            MULTI[(chart, ident)] = replicas
            ok = False
            for p in pdbs:
                ps = p.get("spec") or {}
                if not selects(ps.get("selector") or {}, labels):
                    continue
                if "minAvailable" in ps:
                    m = budget(ps["minAvailable"], replicas)
                    ok = 1 <= m < replicas
                    why = "minAvailable %s" % ps["minAvailable"]
                elif "maxUnavailable" in ps:
                    m = budget(ps["maxUnavailable"], replicas)
                    ok = 1 <= m < replicas
                    why = "maxUnavailable %s" % ps["maxUnavailable"]
                else:
                    why = "no budget"
                if not ok:
                    fail("3: %s renders %d replicas and its PodDisruptionBudget %s has %s; want at least one pod kept "
                         "and one allowed to go" % (where, replicas, p["metadata"]["name"], why), chart, "nopdb", ident)
                    ok = None
                break
            if ok is False:
                fail("3: %s renders %d replicas and no PodDisruptionBudget selects its pods" % (where, replicas),
                     chart, "nopdb", ident)
            for key in (HOST, ZONE):
                if not spreads(pod, labels, key):
                    fail("4: %s renders %d replicas with no anti-affinity or spread over %s selecting its own pods"
                         % (where, replicas, key), chart, "nospread", ident)


def schema_check(chart, cdir, cv):
    path = os.path.join(cdir, "values.schema.json")
    if not os.path.exists(path):
        return
    s = json.load(open(path))
    props = ((((s.get("properties") or {}).get("pod") or {}).get("properties") or {}).get("replicas") or {}).get(
        "properties") or {}
    have = ((cv.get("pod") or {}).get("replicas") or {})
    for k, p in props.items():
        if "default" in p and p["default"] != have.get(k):
            fail("5: %s: values.schema.json defaults pod.replicas.%s to %s, values.yaml to %s; the Composition CRD "
                 "would apply the schema's" % (chart, k, p["default"], have.get(k)), chart)


def chart_check(chart, tmp):
    cdir = os.path.join(BP, chart, "chart")
    cv = yaml.safe_load(open(os.path.join(cdir, "values.yaml"))) or {}
    schema_check(chart, cdir, cv)
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
    rendered = 0
    for label, values in renders:
        rc, out, err = helm_template(w, chart + "-gate", values)
        if rc != 0:
            if label == "defaults":
                fail("0: %s: does not render at its defaults: %s" % (chart, err.strip()[:300]), chart)
            continue
        rendered += 1
        check_render(chart, label, [d for d in yaml.safe_load_all(out) if d])
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
    if rendered == 0 or not WORKLOADS:
        print("FAILED MEASUREMENT: no chart rendered a Deployment or StatefulSet, so nothing was checked")
        sys.exit(2)
    if not ONLY and not SERVING:
        print("FAILED MEASUREMENT: no Deployment selected by a Service was found, so check 1 measured nothing")
        sys.exit(2)
    for chart, entries in sorted(EXEMPT.items()):
        if ONLY and chart not in ONLY:
            continue
        for name in sorted(entries or {}):
            if (chart, name) not in EXEMPT_USED:
                fail("1: hack/single-replica-serving.yaml exempts %s Deployment/%s, which no render shows as a serving "
                     "Deployment; drop the stale entry" % (chart, name), chart)
            else:
                print("EXEMPT: %s Deployment/%s runs one replica (#%s): %s" % (
                    chart, name, entries[name].get("issue"), " ".join(str(entries[name].get("reason")).split())))
    for (chart, ident), rwo in sorted(PINNED.items()):
        print("PINNED: %s %s runs one pod: it mounts ReadWriteOnce claim(s) %s" % (chart, ident, rwo))
    summary = ("%d chart(s), %d render(s); %d serving Deployment(s), %d pinned by a ReadWriteOnce claim, %d multi-replica "
               "workload(s)" % (len(run), rendered, len(SERVING), len(PINNED), len(MULTI)))
    if failures:
        for f in failures:
            print("FAIL: " + f)
        print("SUMMARY: %s; failing: %d serving below 2 replicas, %d serving that drop a pod to roll, %d multi-replica "
              "without a usable PodDisruptionBudget, %d multi-replica that do not spread; %d failing chart(s)"
              % (summary, len(COUNT["under2"]), len(COUNT["surge"]), len(COUNT["nopdb"]), len(COUNT["nospread"]),
                 len(FAILING)))
        sys.exit(1)
    print("OK: %s. Every serving Deployment runs >= 2 replicas and rolls with maxUnavailable 0; every multi-replica "
          "workload has a drainable PodDisruptionBudget and spreads over hosts and zones" % summary)


main()
