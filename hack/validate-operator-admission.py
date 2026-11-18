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
     CRD lists under spec.required.

What the API server enforces here is the CRD's whole structural schema as on a real cluster: required, type, enum,
pattern, format, min/max bounds, x-kubernetes-validations (CEL), x-kubernetes-int-or-string,
x-kubernetes-preserve-unknown-fields, and (with --validate=strict) unknown and duplicate fields.

What it does not enforce:
  - the operators' validating webhooks (the topology operator's need cert-manager and the operator running), which
    check, for example, that rabbitmqClusterReference names a cluster and that immutable fields do not change;
  - update rules (transition CEL rules, immutability): every object is admitted as a create;
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

for tool in ("helm", "kind", "kubectl"):
    if shutil.which(tool) is None:
        print("FAILED MEASUREMENT: %s not on PATH, so nothing was admitted" % tool)
        sys.exit(2)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def fetch_crds(pins):
    src = pins["source"]
    crds = {}
    for f in pins.get("files") or []:
        url = "https://raw.githubusercontent.com/%s/%s/%s" % (src["repository"], src["commit"], f["path"])
        body = urllib.request.urlopen(url, timeout=60).read()
        got = hashlib.sha256(body).hexdigest()
        if got != f["sha256"]:
            print("FAILED MEASUREMENT: %s at %s has sha256 %s, pinned %s" % (f["path"], src["commit"], got, f["sha256"]))
            sys.exit(2)
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
        objs = render_objects(od)
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
        finally:
            run(["kind", "delete", "cluster", "--name", name, "--kubeconfig", kc])
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: %d rabbitmq.com object(s) (%s) admitted by the API server serving the CRDs of %s@%s; %d control(s) "
          "refused as expected" % (admitted, ", ".join("%s %d" % (k, len(by_kind[k])) for k in sorted(by_kind)),
                                   pins["source"]["repository"], pins["source"]["commit"][:7], controls))


main()
