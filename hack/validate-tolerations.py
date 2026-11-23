#!/usr/bin/env python3
"""The umbrella's `tolerations` reach every pod the blueprint renders.

    hack/validate-tolerations.py [repo-root] [chart ...]

A trusted-tenant vcluster copies its pool taint (krateo.io/trusted-tenant=<ns>:NoSchedule) onto every child node, so a
pod without a matching toleration schedules nowhere: every DaemonSet desires 0 pods, and every Deployment, Job and
CronJob sits Pending. The umbrella takes one list, `tolerations`, and passes it to each component as OpenStack-Helm's
pod.tolerations.<component> {enabled: true, tolerations: <list>} (templates/compositions.yaml). That only works if
every workload of every chart applies it, and upstream's charts do not: nine had no pod.tolerations block, and DaemonSets,
Deployments and helm-toolkit Jobs in others never included the snippet. Checked here:

  1. THE UMBRELLA PASSES IT. The lines of compositions.yaml that build a component's spec ($ov, through the tolerations
     merge) are rendered for every component through a probe: each spec carries pod.tolerations.<name> = {enabled: true,
     tolerations: <the list>}, an explicit componentValues entry wins, and with no list nothing is added.
  2. EVERY POD CARRIES IT. Every component chart is rendered with pod.tolerations.<chart> enabled and a probe list, at
     defaults and with every workload manifest it ships turned on (each one alone where turning all on does not render,
     e.g. a backend another one excludes), and with every opt-in feature it declares turned on (kog_credentials, #303:
     its Jobs and CronJob are off by default, so no other render reaches them): every Deployment, StatefulSet,
     DaemonSet, Job and CronJob pod template carries exactly that list. A chart whose values have no
     pod.tolerations.<chart> fails: the component would ignore it.
  3. NO TEMPLATE IS LEFT OUT. Statically, every template that declares one of those kinds applies the snippet, and every
     helm-toolkit job manifest a chart includes is given tolerationsEnabled: a workload no render turns on is covered.

Named charts limit checks 2 and 3 to them (an unknown name fails); check 1 always covers every component.

Fails when helm or PyYAML is missing, when no chart was rendered, and when no pod template was checked.
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
UMBRELLA = os.path.join(BP, "openstack", "chart")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)

PROBE = [{"key": "gate.krateo.io/tolerations-probe", "operator": "Equal", "value": "7f3c", "effect": "NoSchedule"}]
WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob")
KIND = re.compile(r"^kind:\s*(%s)\s*$" % "|".join(WORKLOADS), re.M)
TK_JOB = re.compile(r'include "helm-toolkit\.manifests\.(job_[\w-]+)"')
failures = []
PODS_CHECKED = []


# What the umbrella delivers for a chart's storage (#308): cinder's default refuses, so a chart render carries
# hack/gate_values.py's arguments (nothing for every other chart).
import importlib.util as _gv_util
import os as _gv_os
_gv_spec = _gv_util.spec_from_file_location(
    "gate_values", _gv_os.path.join(_gv_os.path.dirname(_gv_os.path.abspath(__file__)), "gate_values.py"))
gate_values = _gv_util.module_from_spec(_gv_spec)
_gv_spec.loader.exec_module(gate_values)


def fail(m):
    failures.append(m)


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
        if d["kind"] == "CronJob":
            pod = (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec")
        else:
            pod = (spec.get("template") or {}).get("spec")
        if isinstance(pod, dict):
            yield "%s/%s" % (d["kind"], d["metadata"]["name"]), pod


def umbrella_check(tmp):
    """1. what compositions.yaml builds for each component's spec."""
    u = stage(UMBRELLA, tmp, "umbrella")
    comp = open(os.path.join(UMBRELLA, "templates", "compositions.yaml")).read()
    start = comp.find("{{- $ov := ")
    tol = comp.find('"tolerations"', start)
    end = comp.find("{{- end }}", tol) if tol >= 0 else -1
    if start < 0 or tol < 0 or end < 0:
        fail("1: compositions.yaml no longer builds $ov and merges pod.tolerations into it (the span from `{{- $ov := ` "
             "to the end of the tolerations merge), so no component would receive the umbrella's tolerations")
        return
    span = comp[start:end + len("{{- end }}")]
    with open(os.path.join(u, "templates", "zz-gate-tolerations-probe.yaml"), "w") as f:
        f.write("{{- $dr := include \"osh.deregistration\" (list $ (splitList \" \" (include \"osh.selected\" (list $))) (list)) | fromYaml }}\n{{- range $c := .Values.components }}\n" + span + "\n# TOLSPEC {{ $c.name }} {{ $ov | toJson }}\n{{- end }}\n")
    uv = yaml.safe_load(open(os.path.join(UMBRELLA, "values.yaml")))
    names = [c["name"] for c in uv.get("components") or []]
    explicit = [{"key": "gate.krateo.io/explicit", "operator": "Exists"}]
    for label, values in (("with the list", {"tolerations": PROBE,
                                              "componentValues": {"keystone": {"pod": {"tolerations": {"keystone": {
                                                  "tolerations": explicit}}}}}}),
                          ("without it", {})):
        rc, out, err = helm_template(u, "openstack-gate", values)
        if rc != 0:
            fail("1: the umbrella does not render %s: %s" % (label, err.strip()[:300]))
            continue
        specs = {}
        for line in out.splitlines():
            if line.startswith("# TOLSPEC "):
                _, _, name, js = line.split(" ", 3)
                specs[name] = json.loads(js)
        if sorted(specs) != sorted(names):
            fail("1: the probe saw %d component spec(s), the umbrella lists %d" % (len(specs), len(names)))
        for n in names:
            got = (((specs.get(n) or {}).get("pod") or {}).get("tolerations") or {}).get(n)
            if label == "without it":
                if got is not None:
                    fail("1: with no tolerations the umbrella still passes %s pod.tolerations.%s = %s" % (n, n, got))
                continue
            want = {"enabled": True, "tolerations": explicit if n == "keystone" else PROBE}
            if got != want:
                fail("1: the umbrella passes %s pod.tolerations.%s = %s, want %s%s" % (
                    n, n, got, want, " (the explicit componentValues entry must win)" if n == "keystone" else ""))


def manifest_toggles(values):
    """The workload manifests a chart ships turned off: manifests.<name> booleans naming a workload."""
    m = (values or {}).get("manifests") or {}
    return sorted(k for k, v in m.items() if v is False and re.match(r"(deployment|daemonset|statefulset|job|cron_job)_", k))


def chart_check(chart, tmp):
    """2 and 3 for one chart."""
    cdir = os.path.join(BP, chart, "chart")
    cv = yaml.safe_load(open(os.path.join(cdir, "values.yaml"))) or {}
    tol = ((cv.get("pod") or {}).get("tolerations") or {})
    if not isinstance(tol, dict) or chart not in tol:
        fail("2: %s: values.yaml has no pod.tolerations.%s, so the umbrella's tolerations would be ignored" % (chart, chart))
    # 3. static
    tdir = os.path.join(cdir, "templates")
    for fn in sorted(os.listdir(tdir)):
        p = os.path.join(tdir, fn)
        if not os.path.isfile(p):
            continue
        t = open(p).read()
        if KIND.search(t) and "helm-toolkit.snippets.kubernetes_tolerations" not in t:
            fail("3: %s: templates/%s declares %s but never applies helm-toolkit.snippets.kubernetes_tolerations"
                 % (chart, fn, sorted(set(KIND.findall(t)))))
        if TK_JOB.search(t) and "tolerationsEnabled" not in t:
            fail("3: %s: templates/%s builds %s without tolerationsEnabled" % (chart, fn, TK_JOB.findall(t)))
    # 2. rendered
    w = stage(cdir, tmp, chart)
    base = {"pod": {"tolerations": {chart: {"enabled": True, "tolerations": PROBE}}}}
    toggles = manifest_toggles(cv)
    renders = [("defaults", base)]
    allon = json.loads(json.dumps(base))
    allon["manifests"] = {k: True for k in toggles}
    if toggles:
        rc, _, _ = helm_template(w, chart + "-gate", allon)
        if rc == 0:
            renders.append(("every workload manifest on", allon))
        else:
            for k in toggles:
                one = json.loads(json.dumps(base))
                one["manifests"] = {k: True}
                renders.append(("manifests.%s on" % k, one))
    if "kog_credentials" in cv:
        # #303: opt-in, so neither render above reaches its Jobs. Enabled, keystone requires the admin root's reference.
        kog = json.loads(json.dumps(base))
        kog["kog_credentials"] = {"enabled": True}
        kog["endpoints"] = {"identity": {"auth": {"admin": {"passwordFrom": {
            "store": "openstack-credentials", "secret": "openstack-credential-identity-admin", "key": "password"}}}}}
        renders.append(("kog_credentials on", kog))
    rendered = 0
    for label, values in renders:
        rc, out, err = helm_template(w, chart + "-gate", values)
        if rc != 0:
            if label in ("defaults", "kog_credentials on"):
                fail("2: %s (%s): does not render with its tolerations enabled: %s" % (chart, label, err.strip()[:300]))
            continue  # a workload that renders only with settings this check cannot supply; check 3 covers its template
        rendered += 1
        for name, pod in pod_templates([d for d in yaml.safe_load_all(out) if d]):
            PODS_CHECKED.append((chart, name))
            if pod.get("tolerations") != PROBE:
                fail("2: %s (%s): %s carries tolerations %s, want exactly the umbrella's list"
                     % (chart, label, name, pod.get("tolerations")))
    return rendered


def main():
    charts = sorted(d for d in os.listdir(BP) if d != "openstack" and os.path.isdir(os.path.join(BP, d, "chart")))
    uv = yaml.safe_load(open(os.path.join(UMBRELLA, "values.yaml")))
    listed = sorted(c["name"] for c in uv.get("components") or [])
    if charts != listed:
        fail("the umbrella lists %s, the repository has charts %s" % (listed, charts))
    if not charts:
        print("FAILED MEASUREMENT: no component chart found")
        sys.exit(2)
    with tempfile.TemporaryDirectory() as tmp:
        umbrella_check(tmp)
        unknown = sorted(set(ONLY) - set(charts))
        if unknown:
            print("FAILED MEASUREMENT: no chart named %s, so nothing was checked for it" % unknown)
            sys.exit(2)
        rendered = sum(chart_check(c, tmp) for c in charts if not ONLY or c in ONLY)
    if rendered == 0 or not PODS_CHECKED:
        print("FAILED MEASUREMENT: no chart rendered a pod template, so nothing was checked")
        sys.exit(2)
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: %d chart(s), %d render(s), %d pod template(s) carry exactly the umbrella's tolerations; every workload "
          "template applies them; the umbrella passes them to all %d components" % (len(set(ONLY) or charts), rendered,
                                                                                  len(PODS_CHECKED), len(listed)))


main()
