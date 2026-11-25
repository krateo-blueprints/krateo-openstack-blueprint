#!/usr/bin/env python3
"""cinder-volume can start: the internal_cinder project its init reads is created first (#308 run A).

    hack/validate-cinder-internal-tenant.py [repo-root] | --self-test

#308's first live run (GKE, Ceph through Rook, 2026-11-25) found cinder never Ready: cinder-volume's init container
init-cinder-conf runs retrieve-internal-tenant.sh, which reads the Keystone project internal_cinder, and only the
create-internal-tenant Job creates it. Our values had that Job off since cinder was vendored (#3; upstream ships it on).
cinder-bootstrap, a post-install hook at weight -7, waits for a Ready cinder-volume pod: each waited on the other. No
earlier run had a block-storage backend, so cinder-volume never started.

Rendered as the umbrella delivers cinder (hack/gate_values.py), for every rendering in which a cinder-volume workload
runs retrieve-internal-tenant.sh:
  1. the create-internal-tenant Job renders;
  2. it is a post-install and post-upgrade hook, at a weight below every hook that waits for a cinder-volume pod;
  3. it waits for no cinder-volume pod itself.
--self-test: the same checks on main's renders with the Job off must fail, and on with it on must pass.
"""
import json, os, shutil, subprocess, sys, tempfile
import yaml

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else ".")
sys.path.insert(0, os.path.join(ROOT, "hack"))
import gate_values  # noqa: E402


def render(extra):
    tmp = tempfile.mkdtemp()
    try:
        c = os.path.join(tmp, "cinder")
        shutil.copytree(os.path.join(ROOT, "blueprints", "cinder", "chart"), c)
        body = open(os.path.join(c, "Chart.yaml")).read()   # read before opening for write: "w" truncates first
        open(os.path.join(c, "Chart.yaml"), "w").write(body.replace("CHART_VERSION", "0.0.0-gate"))
        os.makedirs(os.path.join(c, "charts"), exist_ok=True)
        shutil.copytree(os.path.join(ROOT, "charts", "helm-toolkit"), os.path.join(c, "charts", "helm-toolkit"))
        r = subprocess.run(["helm", "template", "cinder", c, "-n", "openstack", "--kube-version", "1.36.0"]
                           + gate_values.helm_args(c) + extra, capture_output=True, text=True)
        if r.returncode:
            sys.exit("FAILED MEASUREMENT: cinder does not render: %s" % r.stderr.strip()[-300:])
        return [d for d in yaml.safe_load_all(r.stdout) if d]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def pod_spec(d):
    s = d.get("spec") or {}
    return ((s.get("template") or {}).get("spec")) or {}


def waits_for_volume(c):
    for e in c.get("env") or []:
        if e.get("name") == "DEPENDENCY_POD_JSON" and e.get("value"):
            for p in json.loads(e["value"]):
                l = p.get("labels") or {}
                if l.get("application") == "cinder" and l.get("component") == "volume":
                    return True
    return False


def check(docs):
    bad = []
    readers = [d["metadata"]["name"] for d in docs if d.get("kind") in ("Deployment", "StatefulSet") and
               any("retrieve-internal-tenant" in " ".join(c.get("command") or []) for c in pod_spec(d).get("initContainers") or [])]
    if not readers:
        return None, ["no cinder-volume workload runs retrieve-internal-tenant.sh: nothing measured"]
    jobs = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Job"}
    it = jobs.get("cinder-create-internal-tenant")
    if not it:
        return readers, ["%s read internal_cinder, and no cinder-create-internal-tenant Job renders to create it" % readers]
    a = it["metadata"].get("annotations") or {}
    hooks = set((a.get("helm.sh/hook") or "").split(","))
    if not {"post-install", "post-upgrade"} <= hooks:
        bad.append("cinder-create-internal-tenant is hook %r, want post-install and post-upgrade" % a.get("helm.sh/hook"))
    w = int(a.get("helm.sh/hook-weight") or 0)
    for n, j in jobs.items():
        if n == "cinder-create-internal-tenant":
            continue
        if any(waits_for_volume(c) for c in pod_spec(j).get("initContainers") or []):
            jw = int((j["metadata"].get("annotations") or {}).get("helm.sh/hook-weight") or 0)
            if w >= jw:
                bad.append("cinder-create-internal-tenant (weight %d) does not run before %s (weight %d), which waits for cinder-volume" % (w, n, jw))
    if any(waits_for_volume(c) for c in pod_spec(it).get("initContainers") or []):
        bad.append("cinder-create-internal-tenant itself waits for a cinder-volume pod")
    return readers, bad


if "--self-test" in sys.argv:
    off = check(render(["--set", "manifests.job_create_internal_tenant=false"]))[1]
    on = check(render(["--set", "manifests.job_create_internal_tenant=true"]))[1]
    wrong = (["the Job off is not refused"] if not off else []) + (["the Job on is refused: %s" % on] if on else [])
    for x in wrong:
        print("WRONG: " + x)
    print("self-test: 2 case(s), %d wrong" % len(wrong))
    sys.exit(1 if wrong else 0)
readers, bad = check(render([]))
for b in bad:
    print("FAIL: " + b)
if bad:
    sys.exit(1)
print("OK: %s read internal_cinder; cinder-create-internal-tenant creates it in an earlier hook wave than every hook that waits for cinder-volume" % readers)
