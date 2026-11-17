#!/usr/bin/env python3
"""Every long-running container must carry a readiness probe (#231, #151).

A container with no readinessProbe is Ready the moment it starts. Measured on a 0.3.47 cold
install on GKE: watcher-decision-engine exited 1 about seven seconds after every start and had no
probe at all, so each short run counted as Ready. The `Watcher` composition went Ready=True, the
umbrella `Openstack` went Ready=True with it, and a once-a-minute watch recorded "32/32 components
ready" while the decision engine had never worked once. 32 of 76 long-running containers on that
install had no readiness probe, so Ready on a component or on the umbrella did not mean its
workers ran.

This renders every chart, at its defaults and with every workload it ships switched off turned
on, and fails when a container of a Deployment, StatefulSet, DaemonSet, ReplicaSet or plain Pod
(not a Helm hook) has no readinessProbe, unless hack/readiness-probe-exceptions.yaml names it with
a reason. It also fails when:

  * a readiness probe is an exec of `true` (or `exit 0`, `:`): a probe that cannot fail is the
    defect with a different spelling;
  * an exec probe runs a script under /tmp that the container does not mount from a ConfigMap or
    Secret that renders it. /tmp is an emptyDir in these charts, so such a probe fails forever
    and the pod is never Ready -- the opposite defect, and the likeliest way to ship this fix
    wrong;
  * an exceptions entry matches no container that renders without a readiness probe. An entry
    nothing needs is how an allowlist quietly grows into a second, unreviewed copy of the defect;
  * an entry has no reason;
  * no chart rendered or no container was inspected: a gate with no input must fail.

Usage: hack/validate-readiness-probes.py [repo-root] [chart ...]
With chart names, only those charts are rendered, and only their exceptions are checked for need.
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
    print("FAILED MEASUREMENT: PyYAML unavailable, so no chart was inspected")
    sys.exit(2)

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
ONLY = sys.argv[2:]
BP = os.path.join(ROOT, "blueprints")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
EXCEPTIONS = os.path.join(ROOT, "hack", "readiness-probe-exceptions.yaml")
WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "ReplicationController")
TRIVIAL = re.compile(r"^\s*(?:/bin/|/usr/bin/)?(?:true|:|exit\s+0)\s*$")

if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)

# Workloads a manifests.<x> toggle alone does not switch on (hack/validate-credentials.py MQ_VARIANTS).
VARIANTS = {
    "nova": [("spice console", {"console": {"console_kind": "spice"}, "manifests": {"deployment_spiceproxy": True}}),
             ("serial console", {"console": {"console_kind": "serial"}, "manifests": {"deployment_serialproxy": True}})],
    "neutron": [("SR-IOV backend", {"network": {"backend": ["openvswitch", "sriov"]},
                                    "manifests": {"daemonset_sriov_agent": True}})],
}

failures = []


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
        yaml.safe_dump(values or {}, f)
    try:
        r = subprocess.run(["helm", "template", release, chart, "--namespace", "openstack", "--kube-version", "1.36.0",
                            "-f", f.name], capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    return r.returncode, r.stdout, r.stderr


def workload_toggles(chart):
    """manifests.<x> switches a chart ships off that name a long-running workload (not a Job or CronJob)."""
    v = yaml.safe_load(open(os.path.join(BP, chart, "chart", "values.yaml"))) or {}
    man = v.get("manifests") or {}
    return sorted(k for k, x in man.items() if x is False
                  and (k.startswith(("daemonset_", "deployment_", "statefulset_")) or k.endswith("_monitor")))


def renders_for(chart, w):
    """(label, values) pairs: defaults, every workload on (or each one alone if together they do not render), variants."""
    out = [("defaults", {})]
    toggles = workload_toggles(chart)
    if toggles:
        allon = {"manifests": {k: True for k in toggles}}
        rc, _, _ = helm_template(w, chart + "-gate", allon)
        if rc == 0:
            out.append(("every workload on", allon))
        else:
            out += [("manifests.%s on" % k, {"manifests": {k: True}}) for k in toggles]
    out += VARIANTS.get(chart, [])
    return out


def pod_specs(docs):
    for d in docs:
        if not isinstance(d, dict):
            continue
        kind = d.get("kind")
        meta = d.get("metadata") or {}
        if kind in WORKLOADS:
            spec = ((d.get("spec") or {}).get("template") or {}).get("spec")
        elif kind == "Pod" and "helm.sh/hook" not in (meta.get("annotations") or {}):
            spec = d.get("spec")
        else:
            continue
        if spec:
            yield kind, meta.get("name", "?"), spec


def rendered_keys(docs):
    """ConfigMap/Secret name -> the keys it renders."""
    out = {}
    for d in docs:
        if isinstance(d, dict) and d.get("kind") in ("ConfigMap", "Secret"):
            out[(d.get("metadata") or {}).get("name")] = set((d.get("data") or {})) | set((d.get("stringData") or {}))
    return out


def probe_script_delivered(path, spec, container, keys):
    """True if `path` is mounted into the container from a ConfigMap or Secret that renders it."""
    vols = {v.get("name"): v for v in spec.get("volumes") or []}
    for m in container.get("volumeMounts") or []:
        mp = (m.get("mountPath") or "").rstrip("/")
        v = vols.get(m.get("name")) or {}
        src = v.get("configMap") or v.get("secret")
        if not src:
            continue
        name = src.get("name") or src.get("secretName")
        have = keys.get(name, set())
        items = src.get("items")
        if items:
            have = {i.get("path") or i.get("key") for i in items if i.get("key") in keys.get(name, set())}
        if m.get("subPath"):
            if mp == path and m["subPath"] in have:
                return True
        elif path.startswith(mp + "/") and path[len(mp) + 1:] in have:
            return True
    return False


def check_probe(where, container, spec, keys):
    probe = container.get("readinessProbe") or {}
    cmd = ((probe.get("exec") or {}).get("command")) or []
    if not cmd:
        return
    text = " ".join(str(c) for c in cmd)
    shell_body = str(cmd[-1]) if len(cmd) >= 3 and str(cmd[-2]) == "-c" else None
    if TRIVIAL.match(text) or (shell_body is not None and TRIVIAL.match(shell_body)):
        fail("%s: readinessProbe is `%s`, which cannot fail" % (where, text))
    for arg in cmd:
        arg = str(arg)
        if arg.startswith("/tmp/") and re.search(r"\.(py|sh)$", arg) \
                and not probe_script_delivered(arg, spec, container, keys):
            fail("%s: readinessProbe runs %s, which no volumeMount delivers from a rendered ConfigMap or Secret key; "
                 "/tmp is an emptyDir, so this probe fails forever and the pod is never Ready" % (where, arg))


def load_exceptions():
    if not os.path.isfile(EXCEPTIONS):
        print("FAILED MEASUREMENT: %s is missing" % os.path.relpath(EXCEPTIONS, ROOT))
        sys.exit(2)
    data = yaml.safe_load(open(EXCEPTIONS)) or {}
    out = {}
    for e in data.get("exceptions") or []:
        key = (e.get("chart"), e.get("workload"), e.get("container"))
        if not all(key):
            fail("exceptions: entry %s must name chart, workload (Kind/name) and container" % json.dumps(e))
            continue
        if not str(e.get("reason") or "").strip():
            fail("exceptions: %s/%s:%s has no reason" % key)
        out[key] = e
    return out


def main():
    if not os.path.isdir(BP):
        print("FAILED MEASUREMENT: no blueprints directory at %s" % BP)
        sys.exit(2)
    charts = sorted(d for d in os.listdir(BP) if os.path.isfile(os.path.join(BP, d, "chart", "Chart.yaml")))
    if ONLY:
        unknown = sorted(set(ONLY) - set(charts))
        if unknown:
            print("FAILED MEASUREMENT: no chart named %s" % ", ".join(unknown))
            sys.exit(2)
        charts = [c for c in charts if c in ONLY]
    exceptions = load_exceptions()
    seen, missing, renders = set(), {}, 0
    with tempfile.TemporaryDirectory() as tmp:
        for c in charts:
            w = stage(os.path.join(BP, c, "chart"), tmp, c)
            for label, values in renders_for(c, w):
                rc, out, err = helm_template(w, c + "-gate", values)
                if rc != 0:
                    if label == "defaults":
                        fail("%s: does not render at its defaults: %s" % (c, err.strip()[-300:]))
                    else:
                        fail("%s (%s): does not render, so its containers were not inspected: %s"
                             % (c, label, err.strip()[-300:]))
                    continue
                renders += 1
                docs = [d for d in yaml.safe_load_all(out) if d]
                keys = rendered_keys(docs)
                for kind, name, spec in pod_specs(docs):
                    for ctr in spec.get("containers") or []:
                        key = (c, "%s/%s" % (kind, name), ctr.get("name"))
                        seen.add(key)
                        where = "%s %s:%s (%s)" % (c, key[1], key[2], label)
                        if ctr.get("readinessProbe"):
                            check_probe(where, ctr, spec, keys)
                        else:
                            missing.setdefault(key, label)
    if renders == 0 or not seen:
        for f in failures:
            print("FAIL: " + f)
        print("FAILED MEASUREMENT: no chart rendered a long-running container, so nothing was checked")
        sys.exit(2)
    unlisted = sorted(k for k in missing if k not in exceptions)
    for k in unlisted:
        fail("%s %s:%s has no readinessProbe (%s): it reads Ready the moment it starts, crash-looping or not. "
             "Give it a probe that tests its real health, or add it to %s with the reason it cannot have one"
             % (k[0], k[1], k[2], missing[k], os.path.relpath(EXCEPTIONS, ROOT)))
    for k, e in sorted(exceptions.items()):
        if ONLY and k[0] not in ONLY:
            continue
        if k not in missing:
            state = "renders with a readinessProbe" if k in seen else "is not rendered by any mode checked"
            fail("exceptions: %s %s:%s is listed, but it %s; remove the entry" % (k[0], k[1], k[2], state))
    print("%d long-running container(s) in %d chart(s) across %d render(s); %d without a readinessProbe "
          "(%d listed as exceptions, %d not)" % (len(seen), len(charts), renders, len(missing),
                                               len(missing) - len(unlisted), len(unlisted)))
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: every long-running container has a readiness probe or a reasoned exception")


main()
