#!/usr/bin/env python3
"""Switching a projection to refreshPolicy OnChange keeps its Secret byte-identical, and OnChange then follows only its
own changes, measured on a real External Secrets Operator (#309 C).

    hack/validate-eso-onchange-switch.py [repo-root]

Every projection of a generated root moved from the default Periodic policy (refreshInterval 1h) to OnChange, so that a
root edited by hand (ESO regenerates it, v2.14.0 externalsecret_controller.go:370) reaches no live credential until a
rotation. An installed base upgrades through that switch, so the switch itself must not change any credential. From
ESO v2.14.0's source it should not: the spec change moves the generation, so GetResourceVersion (generation plus
HashMeta) differs from status.syncedResourceVersion and shouldRefresh is true; the refresh re-reads the unchanged root
through the unchanged template. This measures it, on the ESO version create.sh installs, in a kind cluster:

  1. ESO 2.14.0 (helm template of the pinned chart, CRDs included), a kubernetes-provider SecretStore reading a namespace,
     and a root Secret.
  2. A projection in today's form (Periodic, refreshInterval 1h, a transport-URL template): its Secret's data recorded.
  3. The same projection switched to OnChange (no interval): ESO refreshes it (status.syncedResourceVersion moves to the
     new generation), and its Secret's data must be BYTE-IDENTICAL.
  4. The root changed. A twin projection with refreshInterval 10s picks the change up (so the change is observable);
     the OnChange projection must NOT, over the same wait.
  5. Positive control: an annotation on the OnChange projection (its own change) must refresh it to the new root.

Fails when helm, kind, kubectl or PyYAML is missing, and when ESO never syncs (nothing measured).
"""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")
    sys.exit(2)

ESO_VERSION = "2.14.0"   # the version create.sh installs (scratchpad h2 pins the same), and the one whose source is cited
NS = "gate"
failures = []
for tool in ("helm", "kind", "kubectl"):
    if shutil.which(tool) is None:
        print("FAILED MEASUREMENT: %s not on PATH, so nothing was measured" % tool)
        sys.exit(2)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def k(kc, *args, inp=None):
    return run(["kubectl", "--kubeconfig", kc] + list(args), input=inp)


def apply(kc, docs):
    r = k(kc, "apply", "--server-side", "--force-conflicts", "-f", "-", inp=yaml.safe_dump_all(docs, default_flow_style=False))
    if r.returncode != 0:
        print("FAILED MEASUREMENT: apply failed: %s" % r.stderr[-600:])
        sys.exit(2)


def get(kc, kind, name):
    r = k(kc, "-n", NS, "get", kind, name, "-o", "json")
    return json.loads(r.stdout) if r.returncode == 0 else {}


def data_sha(kc, name):
    d = get(kc, "secret", name).get("data") or {}
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest() if d else None, d


def wait(pred, timeout, step=3):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def projection(name, policy, interval=None, annotations=None):
    spec = {"secretStoreRef": {"kind": "SecretStore", "name": "gate-store"},
            "target": {"name": name, "creationPolicy": "Owner",
                       "template": {"type": "Opaque", "engineVersion": "v2",
                                    "data": {"url": "rabbit://heat:{{ .password }}@openstack-mq-nodes:5672/heat"}}},
            "data": [{"secretKey": "password", "remoteRef": {"key": "root", "property": "password"}}]}
    if policy:
        spec["refreshPolicy"] = policy
    if interval:
        spec["refreshInterval"] = interval
    meta = {"name": name, "namespace": NS}
    if annotations:
        meta["annotations"] = annotations
    return {"apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret", "metadata": meta, "spec": spec}


def synced(kc, name, generation=None):
    es = get(kc, "externalsecret", name)
    st = es.get("status") or {}
    ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in st.get("conditions") or [])
    srv = str(st.get("syncedResourceVersion") or "")
    if not ready or not srv:
        return None
    if generation is not None and not srv.startswith("%d-" % generation):
        return None
    return es


def main():
    name = "esoswitch-%d" % os.getpid()
    with tempfile.TemporaryDirectory() as tmp:
        kc = os.path.join(tmp, "kubeconfig")
        r = run(["kind", "create", "cluster", "--name", name, "--kubeconfig", kc, "--wait", "180s"])
        if r.returncode != 0:
            print("FAILED MEASUREMENT: kind create cluster failed: %s" % r.stderr[-600:])
            sys.exit(2)
        try:
            r = run(["helm", "template", "external-secrets", "external-secrets", "--repo", "https://charts.external-secrets.io",
                     "--version", ESO_VERSION, "-n", "external-secrets", "--set", "installCRDs=true", "--include-crds"])
            if r.returncode != 0:
                print("FAILED MEASUREMENT: ESO %s does not render: %s" % (ESO_VERSION, r.stderr[-400:]))
                sys.exit(2)
            k(kc, "create", "namespace", "external-secrets")
            rr = k(kc, "apply", "--server-side", "-n", "external-secrets", "-f", "-", inp=r.stdout)
            if rr.returncode != 0:
                print("FAILED MEASUREMENT: ESO does not apply: %s" % rr.stderr[-400:])
                sys.exit(2)
            if k(kc, "-n", "external-secrets", "wait", "--for=condition=Available", "deploy", "--all", "--timeout=300s").returncode:
                print("FAILED MEASUREMENT: ESO did not become Available")
                sys.exit(2)
            k(kc, "create", "namespace", NS)
            apply(kc, [
                {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "gate-reader", "namespace": NS}},
                {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {"name": "gate-reader", "namespace": NS},
                 "rules": [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["get", "list", "watch"]},
                           {"apiGroups": ["authorization.k8s.io"], "resources": ["selfsubjectrulesreviews"], "verbs": ["create"]}]},
                {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": "gate-reader", "namespace": NS},
                 "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "gate-reader"},
                 "subjects": [{"kind": "ServiceAccount", "name": "gate-reader", "namespace": NS}]},
                {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "root", "namespace": NS},
                 "stringData": {"password": "first-root-value"}},
            ])
            apply(kc, [{"apiVersion": "external-secrets.io/v1", "kind": "SecretStore",
                        "metadata": {"name": "gate-store", "namespace": NS},
                        "spec": {"provider": {"kubernetes": {
                            "remoteNamespace": NS,
                            "server": {"caProvider": {"type": "ConfigMap", "name": "kube-root-ca.crt", "key": "ca.crt"}},
                            "auth": {"serviceAccount": {"name": "gate-reader"}}}}}}])
            # 2. today's form, plus a fast Periodic twin for step 4
            apply(kc, [projection("proj", None, "1h"), projection("proj-fast", None, "10s")])
            if not wait(lambda: synced(kc, "proj") and synced(kc, "proj-fast"), 240):
                es = get(kc, "externalsecret", "proj")
                print("FAILED MEASUREMENT: ESO never synced the projection: %s" % json.dumps((es.get("status") or {}))[:600])
                sys.exit(2)
            before, before_data = data_sha(kc, "proj")
            # 3. the switch
            gen0 = get(kc, "externalsecret", "proj")["metadata"]["generation"]
            apply(kc, [projection("proj", "OnChange")])
            gen1 = get(kc, "externalsecret", "proj")["metadata"]["generation"]
            es1 = wait(lambda: synced(kc, "proj", gen1), 120)
            after, after_data = data_sha(kc, "proj")
            if gen1 == gen0:
                failures.append("3: switching to OnChange did not change the ExternalSecret's generation (%d)" % gen0)
            elif not es1:
                failures.append("3: ESO did not refresh the projection after the switch (generation %d never synced)" % gen1)
            elif after != before:
                failures.append("3: the switch changed the Secret's data: %s -> %s" % (before, after))
            else:
                print("3: switch to OnChange refreshed (generation %d -> %d) with byte-identical data (sha256 %s)"
                      % (gen0, gen1, after[:12]))
            # 4. the root changes
            apply(kc, [{"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "root", "namespace": NS},
                        "stringData": {"password": "second-root-value"}}])
            fast0 = data_sha(kc, "proj-fast")[0]
            moved = wait(lambda: data_sha(kc, "proj-fast")[0] != fast0, 90)
            still, _ = data_sha(kc, "proj")
            if not moved:
                failures.append("4: the 10s Periodic twin never picked up the root change: the step measured nothing")
            elif still != after:
                failures.append("4: the OnChange projection followed the root change by itself (it must not)")
            else:
                print("4: the root changed; the 10s Periodic twin followed it, the OnChange projection did not")
            # 5. its own change refreshes it
            apply(kc, [projection("proj", "OnChange", annotations={"krateo.io/rotation": "t1"})])
            refreshed = wait(lambda: data_sha(kc, "proj")[0] not in (None, after), 90)
            if not refreshed:
                failures.append("5: an annotation change did not refresh the OnChange projection: the refresh path is dead")
            else:
                url = base64.b64decode(data_sha(kc, "proj")[1].get("url", "")).decode()
                if "second-root-value" not in url:
                    failures.append("5: the refreshed projection does not carry the new root")
                else:
                    print("5: its own change refreshed it to the new root")
        finally:
            run(["kind", "delete", "cluster", "--name", name, "--kubeconfig", kc])
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: ESO %s: switching a projection to OnChange keeps its Secret byte-identical; OnChange ignores a root change "
          "a 10s Periodic twin follows, and refreshes on its own change" % ESO_VERSION)


main()
