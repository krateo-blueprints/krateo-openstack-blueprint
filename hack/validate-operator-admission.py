#!/usr/bin/env python3
"""Every rabbitmq.com object this repository renders in messaging.backend operator mode is admitted by a real API server
that serves the operators' own CRDs.

    hack/validate-operator-admission.py [repo-root]

Why (H2, 2026-11-18): 0.3.74-main.g92cd983 rendered a RabbitmqCluster whose spec.override.statefulSet.spec.template.spec
held only a nodeSelector. The CRD types that field as a whole PodSpec, where `containers` is required, so on GKE the API
server refused the object and no broker was ever created. validate-messaging-operator and validate-messaging-topology
were green: they render the objects and check this repository's own predicates, and nothing asked the CRD. A JSON
Schema library here would be a second implementation that disagrees with the API server at the edges (defaults,
x-kubernetes-* extensions, CEL rules), so the API server does the validating, as Helm does in validate-values-schema.

What it does:
  1. reads hack/operator-crds.yaml and fetches each upstream manifest from rabbitmq-operator-blueprint at the pinned
     commit, refusing a file whose sha256 differs or a missing CRD from `require`;
  2. runs validate-messaging-operator.py and validate-messaging-topology.py with OPERATOR_OBJECTS_DIR set, so the
     objects are the ones those gates render from the umbrella's operator spec (one source of what is rendered); both
     must pass, and every kind in `require` must have at least one object;
  3. starts a kind cluster, applies the CRDs, and dry-runs every object server-side (`kubectl apply --dry-run=server`);
  4. checks that the instrument can say no: per kind, one admitted object is broken and must be refused -- the
     RabbitmqCluster without its override `containers` (this defect), every other kind without the first field its
     CRD lists under spec.required;
  5. PHASE 2, the operators' real admission webhooks (#405; H2 run 3 met the third admission-class defect of the day
     there: the topology operator's User webhook refused an import Secret without rabbitmq.com/topology-operator=true).
     In the same cluster it installs cert-manager (pinned, hack/operator-crds.yaml certManager) and both operator
     manifests whole, waits until both webhooks answer, writes for every ExternalSecret the gates render the Secret ESO
     would write (hack/eso_model.py, whose self-test runs first), and dry-runs every rabbitmq.com object again with
     the webhooks active. Controls, each refused for its own reason: a User whose import Secret lacks the label ("must
     have label"), a User whose import Secret does not exist ("not found"), and a RabbitmqCluster whose override sets
     hostNetwork ("hostNetwork is not permitted"; the cluster-operator's override rules).

What the API server enforces here is the CRD's whole structural schema as on a real cluster: required, type, enum,
pattern, format, min/max bounds, x-kubernetes-validations (CEL), x-kubernetes-int-or-string,
x-kubernetes-preserve-unknown-fields, and (with --validate=strict) unknown and duplicate fields.

Phase 2 adds what the operators' webhooks enforce on create: the cluster-operator's defaulting and override rules for
RabbitmqCluster, and the topology operator's Secret-label, user/userReference and cluster-reference rules.

What it does not enforce:
  - ESO itself: the Secrets phase 2 reads are eso_model's, a model of what ESO writes from each ExternalSecret's
    spec.target. A check built on that model cannot validate ESO; the H2 live test does;
  - update rules (transition CEL rules, immutability, webhook ValidateUpdate): every object is admitted as a create;
  - anything an operator does after admission (the H2 live test covers those);
  - objects rendered outside the two gates' operator-spec renders (mutations, other values).

Exits 2 when a tool is missing or there is nothing to check, 1 on any refusal or failed control.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")
    sys.exit(2)

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
HACK = os.path.join(ROOT, "hack")
NS = "openstack"
GATES = ("validate-messaging-operator.py", "validate-messaging-topology.py")
KIND_OF = {
    "rabbitmqclusters.rabbitmq.com": "RabbitmqCluster",
    "users.rabbitmq.com": "User",
    "vhosts.rabbitmq.com": "Vhost",
    "permissions.rabbitmq.com": "Permission",
    "policies.rabbitmq.com": "Policy",
}
failures = []
MANIFESTS = []   # the pinned operator manifests, whole, for phase 2

for tool in ("helm", "kind", "kubectl"):
    if shutil.which(tool) is None:
        print("FAILED MEASUREMENT: %s not on PATH, so nothing was admitted" % tool)
        sys.exit(2)


def _load(name):
    import importlib.util as u
    spec = u.spec_from_file_location(name, os.path.join(HACK, name + ".py"))
    mod = u.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def fetch_pinned(url, sha):
    body = urllib.request.urlopen(url, timeout=60).read()
    got = hashlib.sha256(body).hexdigest()
    if got != sha:
        print("FAILED MEASUREMENT: %s has sha256 %s, pinned %s" % (url, got, sha))
        sys.exit(2)
    return body


def fetch_crds(pins):
    src = pins["source"]
    crds = {}
    MANIFESTS.clear()
    for f in pins.get("files") or []:
        url = "https://raw.githubusercontent.com/%s/%s/%s" % (src["repository"], src["commit"], f["path"])
        body = urllib.request.urlopen(url, timeout=60).read()
        got = hashlib.sha256(body).hexdigest()
        if got != f["sha256"]:
            print("FAILED MEASUREMENT: %s at %s has sha256 %s, pinned %s" % (f["path"], src["commit"], got, f["sha256"]))
            sys.exit(2)
        MANIFESTS.append(body)
        for d in yaml.safe_load_all(body):
            if d and d.get("kind") == "CustomResourceDefinition":
                crds[d["metadata"]["name"]] = d
    missing = [n for n in pins.get("require") or [] if n not in crds]
    if not pins.get("require") or missing:
        print("FAILED MEASUREMENT: the pinned manifests lack the required CRDs %s" % (missing or "(none required)"))
        sys.exit(2)
    return crds


def render_objects(out_dir):
    env = dict(os.environ, OPERATOR_OBJECTS_DIR=out_dir)
    for g in GATES:
        r = run([sys.executable, os.path.join(HACK, g), ROOT], env=env)
        if r.returncode != 0:
            failures.append("%s failed, so its objects are not the rendered truth:\n%s" % (g, (r.stdout + r.stderr)[-1500:]))
    objs = []
    for fn in sorted(os.listdir(out_dir)):
        for d in yaml.safe_load_all(open(os.path.join(out_dir, fn))):
            if d:
                objs.append((fn[:-5], d))
    return objs


def dry_run(kc, obj, tmp):
    p = os.path.join(tmp, "obj.yaml")
    with open(p, "w") as f:
        yaml.safe_dump(obj, f, default_flow_style=False, sort_keys=False)
    # --validate=strict: server-side field validation refuses unknown and duplicate fields, which the API server would
    # otherwise prune silently (a misspelled key would vanish, not fail).
    r = run(["kubectl", "--kubeconfig", kc, "apply", "--dry-run=server", "--validate=strict", "-n", NS, "-f", p])
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def broken(obj, crd):
    """A copy of obj with one thing its CRD requires taken away, and what was taken."""
    b = json.loads(json.dumps(obj))
    if obj["kind"] == "RabbitmqCluster":
        ps = ((((b["spec"].get("override") or {}).get("statefulSet") or {}).get("spec") or {}).get("template") or {}).get("spec")
        if not isinstance(ps, dict) or "containers" not in ps:
            return None, None
        ps.pop("containers")
        return b, "spec.override.statefulSet.spec.template.spec.containers"
    req = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"].get("required") or []
    if not req:
        return None, None
    b["spec"].pop(req[0], None)
    return b, "spec." + req[0]


def main():
    pins = yaml.safe_load(open(os.path.join(HACK, "operator-crds.yaml")))
    crds = fetch_crds(pins)
    with tempfile.TemporaryDirectory() as tmp:
        od = os.path.join(tmp, "objects")
        os.makedirs(od)
        rendered = render_objects(od)
        externals = [(t, d) for t, d in rendered if d.get("kind") == "ExternalSecret"]
        objs = [(t, d) for t, d in rendered if str(d.get("apiVersion", "")).startswith("rabbitmq.com/")]
        by_kind = {}
        for tag, d in objs:
            by_kind.setdefault(d["kind"], []).append((tag, d))
        need = [KIND_OF[n] for n in pins["require"]]
        empty = [k for k in need if not by_kind.get(k)]
        if empty:
            print("FAILED MEASUREMENT: the gates rendered no %s, so nothing of that kind was admitted (%d objects: %s)"
                  % (empty, len(objs), sorted(by_kind)))
            for f in failures:
                print("FAIL: " + f)
            sys.exit(2)
        name = "opadm-%d" % os.getpid()
        kc = os.path.join(tmp, "kubeconfig")
        r = run(["kind", "create", "cluster", "--name", name, "--kubeconfig", kc, "--wait", "180s"])
        if r.returncode != 0:
            print("FAILED MEASUREMENT: kind create cluster failed: %s" % r.stderr[-800:])
            sys.exit(2)
        try:
            cf = os.path.join(tmp, "crds.yaml")
            with open(cf, "w") as f:
                yaml.safe_dump_all(list(crds.values()), f, default_flow_style=False, sort_keys=False)
            for cmd in (["apply", "--server-side", "-f", cf],
                        ["wait", "--for", "condition=Established", "--timeout", "120s"] + ["crd/" + n for n in crds],
                        ["create", "namespace", NS]):
                r = run(["kubectl", "--kubeconfig", kc] + cmd)
                if r.returncode != 0:
                    print("FAILED MEASUREMENT: kubectl %s failed: %s" % (cmd[0], r.stderr[-800:]))
                    sys.exit(2)
            admitted = 0
            for tag, d in objs:
                ok, msg = dry_run(kc, d, tmp)
                if ok:
                    admitted += 1
                else:
                    failures.append("%s %s (rendered by %s) is refused by the API server: %s"
                                    % (d["kind"], d["metadata"]["name"], tag, msg[:600]))
            controls = 0
            crd_of = {v: k for k, v in KIND_OF.items()}
            for k in need:
                tag, d = by_kind[k][0]
                b, what = broken(d, crds[crd_of[k]])
                if b is None:
                    failures.append("control %s: nothing required to take away (no override containers, or no "
                                    "spec.required in its CRD), so no refusal can be shown" % k)
                    continue
                ok, msg = dry_run(kc, b, tmp)
                if ok:
                    failures.append("control %s %s without %s was ADMITTED: this check cannot say no"
                                    % (k, d["metadata"]["name"], what))
                elif what.rsplit(".", 1)[-1] not in msg:
                    failures.append("control %s without %s was refused for another reason: %s" % (k, what, msg[:300]))
                else:
                    controls += 1
            phase2 = webhooks(kc, tmp, pins, objs, externals, by_kind)
        finally:
            run(["kind", "delete", "cluster", "--name", name, "--kubeconfig", kc])
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: %d rabbitmq.com object(s) (%s) admitted by the API server serving the CRDs of %s@%s; %d control(s) "
          "refused as expected" % (admitted, ", ".join("%s %d" % (k, len(by_kind[k])) for k in sorted(by_kind)),
                                   pins["source"]["repository"], pins["source"]["commit"][:7], controls))
    print("OK: phase 2, the operators' webhooks: %d object(s) admitted, %d imported Secret(s) modelled from their "
          "ExternalSecrets, %d control(s) refused for their own reason" % phase2)


WEBHOOK_DOWN = ("failed calling webhook", "connection refused", "no endpoints available", "x509", "context deadline",
                "service \"", "i/o timeout")


def kubectl(kc, *args, **kw):
    return run(["kubectl", "--kubeconfig", kc] + list(args), **kw)


def webhooks(kc, tmp, pins, objs, externals, by_kind):
    """Phase 2 (#405): cert-manager and both operators whole, the Secrets ESO would write, every object again with the
    webhooks active, and three controls. Returns (admitted, secrets, controls); records failures."""
    import time
    eso = _load("eso_model")
    bad = eso.self_test()
    if bad:
        print("FAILED MEASUREMENT: eso_model's self-test fails, so the Secrets phase 2 reads are not ESO's: %s" % bad)
        sys.exit(2)
    if not externals:
        print("FAILED MEASUREMENT: the gates emitted no ExternalSecret, so no import Secret can be modelled")
        sys.exit(2)
    cm = fetch_pinned(pins["certManager"]["url"], pins["certManager"]["sha256"])
    for label, body in [("cert-manager", cm)] + [("operator manifest %d" % i, b) for i, b in enumerate(MANIFESTS)]:
        f = os.path.join(tmp, "apply.yaml")
        open(f, "wb").write(body)
        r = kubectl(kc, "apply", "--server-side", "--force-conflicts", "-f", f)
        if r.returncode != 0:
            print("FAILED MEASUREMENT: applying %s failed: %s" % (label, r.stderr[-800:]))
            sys.exit(2)
        if label == "cert-manager":
            r = kubectl(kc, "-n", "cert-manager", "wait", "--for=condition=Available", "deploy", "--all", "--timeout=300s")
            if r.returncode != 0:
                print("FAILED MEASUREMENT: cert-manager did not become Available: %s" % r.stderr[-400:])
                sys.exit(2)
    r = kubectl(kc, "-n", "rabbitmq-system", "wait", "--for=condition=Available", "deploy", "--all", "--timeout=300s")
    if r.returncode != 0:
        print("FAILED MEASUREMENT: the operators did not become Available: %s" % r.stderr[-400:])
        sys.exit(2)
    # The Secrets the rabbitmq.com objects import (a User's importCredentialsSecret, a RabbitmqCluster's
    # secretBackend.externalSecret), each written as ESO would write it from the ExternalSecret that renders it, and
    # created for real: the webhooks read them through the operator's cache. Only those are modelled.
    def target_name(e):
        return ((e.get("spec") or {}).get("target") or {}).get("name") or e["metadata"]["name"]
    imports = {}
    for tag, d in objs:
        n = ((d.get("spec") or {}).get("importCredentialsSecret") or {}).get("name") if d["kind"] == "User" else \
            (((d.get("spec") or {}).get("secretBackend") or {}).get("externalSecret") or {}).get("name") \
            if d["kind"] == "RabbitmqCluster" else None
        if n:
            imports.setdefault(n, "%s %s (%s)" % (d["kind"], d["metadata"]["name"], tag))
    by_target = {}
    for tag, e in externals:
        by_target.setdefault(target_name(e), (tag, e))
    orphans = sorted(n for n in imports if n not in by_target)
    for n in orphans:
        failures.append("phase 2: %s imports Secret %s, which no rendered ExternalSecret writes" % (imports[n], n))
    if not imports:
        print("FAILED MEASUREMENT: no rabbitmq.com object imports a Secret, so no import Secret can be modelled")
        sys.exit(2)
    secrets = 0
    for tag, es in [by_target[n] for n in sorted(imports) if n in by_target]:
        try:
            sec = eso.secret(es, NS)
        except eso.Unmodelled as e:
            failures.append("phase 2: %s (rendered by %s): %s" % (es["metadata"]["name"], tag, e))
            continue
        f = os.path.join(tmp, "secret.yaml")
        yaml.safe_dump(sec, open(f, "w"), default_flow_style=False, sort_keys=False)
        r = kubectl(kc, "apply", "-n", NS, "-f", f)
        if r.returncode != 0:
            failures.append("phase 2: the modelled Secret %s could not be created: %s" % (sec["metadata"]["name"], r.stderr[-300:]))
        else:
            secrets += 1
    # Both webhooks must answer before any verdict: probe with one RabbitmqCluster and one User until no answer is a
    # webhook-unreachable one (cert-manager issues the serving certs and injects the CA asynchronously).
    probes = [by_kind["RabbitmqCluster"][0][1], by_kind["User"][0][1]]
    end = time.time() + 240
    while True:
        msgs = [dry_run(kc, d, tmp)[1] for d in probes]
        if not any(any(w in m for w in WEBHOOK_DOWN) for m in msgs):
            break
        if time.time() > end:
            print("FAILED MEASUREMENT: the webhooks did not answer within 240 s: %s" % [m[:200] for m in msgs])
            sys.exit(2)
        time.sleep(5)
    admitted = 0
    for tag, d in objs:
        ok, msg = dry_run(kc, d, tmp)
        # A Secret created a moment ago may not be in the operator's cache yet: retry twice, only on those two answers.
        for _ in range(2):
            if ok or not ("must have label" in msg or "not found" in msg):
                break
            time.sleep(5)
            ok, msg = dry_run(kc, d, tmp)
        if ok:
            admitted += 1
        else:
            failures.append("phase 2: %s %s (rendered by %s) is refused with the operators' webhooks active: %s"
                            % (d["kind"], d["metadata"]["name"], tag, msg[:600]))
    controls = 0

    def control(label, obj, want):
        nonlocal controls
        ok, msg = dry_run(kc, obj, tmp)
        if ok:
            failures.append("phase 2 control %s was ADMITTED: this check cannot say no" % label)
        elif want not in msg:
            failures.append("phase 2 control %s was refused for another reason (want %r): %s" % (label, want, msg[:300]))
        else:
            controls += 1
    user = json.loads(json.dumps(by_kind["User"][0][1]))
    imported = (user["spec"].get("importCredentialsSecret") or {}).get("name")
    if not imported or imported not in by_target:
        failures.append("phase 2: the first User imports %r, which no rendered ExternalSecret writes, so the label "
                        "controls cannot be built" % imported)
    else:
        unlabelled = json.loads(json.dumps(eso.secret(by_target[imported][1], NS)))
        unlabelled["metadata"]["name"] = imported + "-gate-nolabel"
        unlabelled["metadata"]["labels"].pop("rabbitmq.com/topology-operator", None)
        f = os.path.join(tmp, "nolabel.yaml")
        yaml.safe_dump(unlabelled, open(f, "w"), default_flow_style=False, sort_keys=False)
        kubectl(kc, "apply", "-n", NS, "-f", f)
        u1 = json.loads(json.dumps(user))
        u1["metadata"]["name"] += "-gate-nolabel"
        u1["spec"]["importCredentialsSecret"]["name"] = unlabelled["metadata"]["name"]
        control("User importing a Secret without rabbitmq.com/topology-operator", u1, "must have label")
        u2 = json.loads(json.dumps(user))
        u2["metadata"]["name"] += "-gate-absent"
        u2["spec"]["importCredentialsSecret"]["name"] = imported + "-gate-absent"
        control("User importing a Secret that does not exist", u2, "not found")
    rc = json.loads(json.dumps(by_kind["RabbitmqCluster"][0][1]))
    rc["metadata"]["name"] += "-gate-hostnet"
    ((rc["spec"].setdefault("override", {}).setdefault("statefulSet", {}).setdefault("spec", {})
      .setdefault("template", {}).setdefault("spec", {})))["hostNetwork"] = True
    control("RabbitmqCluster whose override sets hostNetwork", rc, "hostNetwork is not permitted")
    return admitted, secrets, controls


main()
