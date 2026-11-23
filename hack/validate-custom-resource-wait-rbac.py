#!/usr/bin/env python3
"""A pod that waits on a custom resource may read it (issue #309).

    hack/validate-custom-resource-wait-rbac.py [repo-root]

kubernetes-entrypoint, at the image every chart pins (c606628c), waits on custom resources: `dependencies.<x>.
custom_resources` entries, passed as JSON in DEPENDENCY_CUSTOM_RESOURCE, matched by `fields` or by `status.conditions`
the way `kubectl wait --for=condition=Ready` does (dependencies/customresource/customresource.go). It reads each one with
a named GET after discovering its group/version (client/client.go). helm-toolkit's dependency Role covered only core,
batch, apps and discovery resources, so every such wait would be Forbidden and the pod would stay in Init. The operator
CRs that replace db-init and rabbit-init (#309: mariadb-operator User/Grant/Database, the RabbitMQ topology operator's
User/Vhost/Permission) are custom resources, so this has to hold before any chart waits on one.

helm-toolkit's kubernetes_pod_rbac_serviceaccount now renders, for a component with custom_resources, a Role granting
`get` on exactly each named object (its group, its `resource` plural, resourceNames) and a RoleBinding to the pod's
ServiceAccount.

  1. RENDERED, WITH A STAND-IN DEPENDENCY. memcached is rendered with custom_resources on its server component (one
     waiting on a condition). Every entry in its init container's DEPENDENCY_CUSTOM_RESOURCE has a Role rule granting
     get on its group, resource and name, bound to the pod's ServiceAccount, in the release namespace.
  2. REFUSED. An entry without `resource`, or with an apiVersion that is not <group>/<version>, fails the render with
     a message naming the entry.
  3. NOTHING WITHOUT THEM. memcached at its defaults renders no custom-resource Role.
  4. EVERY CHART. Every chart whose values declare custom_resources is rendered at its defaults, and part 1's check
     holds for every pod that waits on one. Each such entry must also wait on a condition: a wait on mere existence
     passes the moment the object is created, before its operator has reconciled it.

Fails when helm or PyYAML is missing, and when part 1 finds no entry to check.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
BP = os.path.join(ROOT, "blueprints")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
NS = "openstack"


def measurement_failed(msg):
    print("FAILED MEASUREMENT: " + msg)
    sys.exit(2)


try:
    import yaml
except ImportError:
    measurement_failed("PyYAML unavailable, so nothing was rendered")
if shutil.which("helm") is None:
    measurement_failed("helm not on PATH, so nothing was rendered")

failures = []


def fail(m):
    failures.append(m)


def stage(chart, tmp):
    w = os.path.join(tmp, chart)
    if not os.path.isdir(w):
        shutil.copytree(os.path.join(BP, chart, "chart"), w)
        cy = os.path.join(w, "Chart.yaml")
        txt = open(cy).read()
        open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
        os.makedirs(os.path.join(w, "charts"), exist_ok=True)
        shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
    return w


def render(chart_dir, values):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values or {}, f)
    try:
        r = subprocess.run(["helm", "template", "gate", chart_dir, "--namespace", NS, "--kube-version", "1.36.0",
                            "-f", f.name], capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    return r.returncode, r.stdout, r.stderr


def pod_specs(docs):
    for d in docs:
        spec = (d.get("spec") or {})
        if d.get("kind") in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
            yield d, spec.get("template", {}).get("spec", {})
        elif d.get("kind") == "CronJob":
            yield d, spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})


def waits(pod):
    out = []
    for c in (pod.get("initContainers") or []):
        for e in (c.get("env") or []):
            if e.get("name") == "DEPENDENCY_CUSTOM_RESOURCE" and e.get("value"):
                out.extend(json.loads(e["value"]))
    return out


def grants(docs):
    """{(sa, namespace): set of (group, resource, name)} from Roles and their RoleBindings."""
    roles = {(d["metadata"]["namespace"], d["metadata"]["name"]): d for d in docs if d.get("kind") == "Role"}
    out = {}
    for b in (d for d in docs if d.get("kind") == "RoleBinding"):
        role = roles.get((b["metadata"].get("namespace"), b["roleRef"]["name"]))
        if not role:
            continue
        for s in b.get("subjects") or []:
            if s.get("kind") != "ServiceAccount":
                continue
            key = (s["name"], s.get("namespace"))
            for r in role.get("rules") or []:
                if "get" not in (r.get("verbs") or []):
                    continue
                for g in r.get("apiGroups") or []:
                    for res in r.get("resources") or []:
                        for n in r.get("resourceNames") or ["*"]:
                            out.setdefault(key, set()).add((g, res, n))
    return out


def check_waits(label, docs, need_condition):
    g = grants(docs)
    seen = 0
    for d, pod in pod_specs(docs):
        entries = waits(pod)
        if not entries:
            continue
        sa = pod.get("serviceAccountName")
        for e in entries:
            seen += 1
            group = (e.get("apiVersion") or "").split("/")[0]
            have = g.get((sa, NS), set())
            if (group, e.get("resource"), e.get("name")) not in have and (group, e.get("resource"), "*") not in have:
                fail("%s: %s %s waits on %s %s %r, but its ServiceAccount %s may not get it (grants: %s)"
                     % (label, d["kind"], d["metadata"]["name"], e.get("apiVersion"), e.get("kind"), e.get("name"), sa,
                        sorted(have) or "none"))
            if e.get("namespace") != NS:
                fail("%s: %s waits on %r in namespace %r, not the release namespace %s"
                     % (label, d["metadata"]["name"], e.get("name"), e.get("namespace"), NS))
            if need_condition and not e.get("conditions"):
                fail("%s: %s waits on %s %r without a condition: existence passes before the operator has reconciled "
                     "it. Add conditions (type Ready)." % (label, d["metadata"]["name"], e.get("kind"), e.get("name")))
    return seen


STANDIN = [
    {"apiVersion": "k8s.mariadb.com/v1alpha1", "kind": "User", "resource": "users", "name": "gate-user",
     "conditions": [{"type": "Ready"}]},
    {"apiVersion": "rabbitmq.com/v1beta1", "kind": "Vhost", "resource": "vhosts", "name": "gate-vhost",
     "conditions": [{"type": "Ready", "status": "True"}]},
]


def main():
    with tempfile.TemporaryDirectory() as tmp:
        mc = stage("memcached", tmp)
        rc, out, err = render(mc, {"dependencies": {"static": {"memcached": {"custom_resources": STANDIN}}}})
        if rc != 0:
            measurement_failed("memcached does not render with stand-in custom_resources: %s" % err.strip()[:300])
        docs = [d for d in yaml.safe_load_all(out) if d]
        n = check_waits("1", docs, True)
        if n != len(STANDIN):
            fail("1: the stand-in render carries %d custom-resource wait(s), want %d: the entries never reached the "
                 "init container, so nothing was checked" % (n, len(STANDIN)))
        for label, entry, needle in (
                ("an entry without resource", {"apiVersion": "k8s.mariadb.com/v1alpha1", "kind": "User",
                                               "name": "gate-user"}, "needs `resource`"),
                ("a core apiVersion", {"apiVersion": "v1", "kind": "ConfigMap", "resource": "configmaps",
                                       "name": "x"}, "<group>/<version>")):
            rc, _, err = render(mc, {"dependencies": {"static": {"memcached": {"custom_resources": [entry]}}}})
            if rc == 0:
                fail("2: %s renders; it must be refused" % label)
            elif needle not in err:
                fail("2: %s is refused, but not by this check: %s" % (label, err.strip()[:200]))
        rc, out, err = render(mc, {})
        if rc != 0:
            measurement_failed("memcached does not render at its defaults: %s" % err.strip()[:200])
        extra = [d["metadata"]["name"] for d in yaml.safe_load_all(out)
                 if d and d.get("kind") in ("Role", "RoleBinding") and d["metadata"]["name"].endswith("-custom-resources")]
        if extra:
            fail("3: memcached at its defaults renders %s, with no custom_resources declared" % extra)
        declaring = []
        for chart in sorted(os.listdir(BP)):
            vp = os.path.join(BP, chart, "chart", "values.yaml")
            if not os.path.isfile(vp):
                continue
            deps = ((yaml.safe_load(open(vp)) or {}).get("dependencies") or {})
            if any((v or {}).get("custom_resources") for v in (deps.get("static") or {}).values()):
                declaring.append(chart)
        for chart in declaring:
            rc, out, err = render(stage(chart, tmp), {})
            if rc != 0:
                fail("4: %s declares custom_resources and does not render: %s" % (chart, err.strip()[:200]))
                continue
            if not check_waits("4 " + chart, [d for d in yaml.safe_load_all(out) if d], True):
                fail("4: %s declares custom_resources, but no pod it renders waits on one" % chart)
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: %d stand-in custom-resource wait(s) each granted get on exactly that object; entries without a resource "
          "or with a core apiVersion refused; none at defaults; %d chart(s) declare custom_resources%s"
          % (len(STANDIN), len(declaring), (" (" + ", ".join(declaring) + "), all granted and waiting on a condition")
                                            if declaring else ""))


main()
