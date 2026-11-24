#!/usr/bin/env python3
"""MariaDB and RabbitMQ roll when a credential's data changes, and for nothing else in its manifest (#449).

    hack/validate-credential-hash.py [repo-root]

Measured in the 0.3.71 -> 0.3.74 upgrade (GKE, r074): #422 moved the credential ExternalSecrets from `refreshInterval: 1h` to
`refreshPolicy: OnChange`, which changes no password, and both one-replica servers restarted: MariaDB down 70 s, RabbitMQ 40 s,
Keystone's tokens failing for 2 min. Their pod templates hashed the whole manifest.

Each chart is rendered as the umbrella delivers it (its credentials by reference), then again with one change:
  (a) a field that decides no data, in every credential ExternalSecret (refreshPolicy, a label, an annotation, creationPolicy):
      the server's pod template must be IDENTICAL;
  (b) what decides the data (the ESO template of a key; the key read from the root): it must DIFFER;
  (c) a rotation (another root Secret named): it must DIFFER.
Planted control on every run: with helm-toolkit.utils.hash back in the StatefulSet, (a) must be caught. A change that alters
no rendered document at all is a failure of this gate, not a pass.

Fails when helm or PyYAML is missing, when a chart does not render, and when a mutation finds nothing to change.
"""
import copy
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
sys.path.insert(0, os.path.join(ROOT, "hack"))
import umbrella_spec  # noqa: E402

TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
# component -> its one-replica server, and the credential files its pod template hashes
SERVERS = {
    "mariadb": ("mariadb-server", ("secret-dbadmin-password.yaml", "secret-sst-password.yaml", "secret-dbaudit-password.yaml")),
    "rabbitmq": ("rabbitmq-rabbitmq", ("secret-rabbit-admin.yaml", "secret-erlang-cookie.yaml")),
}
fails = []


def stage(tmp, name, comp, edit=None):
    w = os.path.join(tmp, name)
    shutil.copytree(os.path.join(ROOT, "blueprints", comp, "chart"), w)
    cy = os.path.join(w, "Chart.yaml")
    text = open(cy).read()
    open(cy, "w").write(text.replace("CHART_VERSION", "0.0.0-gate"))
    os.makedirs(os.path.join(w, "charts"), exist_ok=True)
    if not os.path.isdir(os.path.join(w, "charts", "helm-toolkit")):
        shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
    n = 0
    if edit:
        for d, _, fs in os.walk(os.path.join(w, "templates")):
            for f in fs:
                p = os.path.join(d, f)
                s = open(p).read()
                t = edit(f, s)
                if t != s:
                    n += 1
                    open(p, "w").write(t)
    return w, n


def render(w, comp, spec, tmp):
    p = os.path.join(tmp, "values.yaml")
    yaml.safe_dump(spec, open(p, "w"))
    r = subprocess.run(["helm", "template", comp, w, "--namespace", "openstack", "-f", p], capture_output=True, text=True)
    if r.returncode != 0:
        print("FAIL: %s does not render: %s" % (comp, r.stderr.strip()[-400:]))
        sys.exit(1)
    return [d for d in yaml.safe_load_all(r.stdout) if d]


def pod_template(docs, comp):
    hit = [d for d in docs if d.get("kind") == "StatefulSet" and "server" in d["metadata"]["name"] or
           d.get("kind") == "StatefulSet" and comp == "rabbitmq"]
    if len(hit) != 1:
        print("FAIL: %s renders %d server StatefulSet(s), want 1" % (comp, len(hit)))
        sys.exit(1)
    return json.dumps(hit[0]["spec"]["template"], sort_keys=True)


def credentials(docs):
    return {d["metadata"]["name"]: d for d in docs if d.get("kind") == "ExternalSecret"}


def in_es(s, fn):
    """Apply fn to the text of every ExternalSecret literal in a template file."""
    parts = re.split(r"(?m)^(?=apiVersion: external-secrets\.io/)", s)
    return parts[0] + "".join(fn(p) for p in parts[1:])


def non_data(f, s):
    def fn(p):
        p = p.replace("refreshPolicy: OnChange", "refreshPolicy: Periodic\n  refreshInterval: 7h")
        p = p.replace("creationPolicy: Owner", "creationPolicy: Orphan")
        return p.replace("  annotations:\n", "  labels:\n    gate.krateo.io/churn: \"1\"\n  annotations:\n    gate.krateo.io/churn: \"1\"\n", 1)
    return in_es(s, fn)


def old_hash(f, s):
    return s.replace("helm-toolkit.utils.secret_data_hash", "helm-toolkit.utils.hash") if f == "statefulset.yaml" else s


def walk(node, key, fn, path=""):
    """Apply fn to every dict found under `key`; returns how many."""
    n = 0
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, dict) and v.get("secret"):
                fn(v)
                n += 1
            n += walk(v, key, fn, path + "/" + str(k))
    return n


def check(comp, spec, tmp):
    base_w, _ = stage(tmp, comp + "-base", comp)
    base = render(base_w, comp, spec, tmp)
    pt, es = pod_template(base, comp), credentials(base)
    if not es:
        fails.append("%s renders no ExternalSecret from the umbrella's values, so nothing was checked" % comp)
        return 0
    n = 0

    def expect(what, docs, same, control=False):
        changed = credentials(docs) != es
        if not changed:
            fails.append("%s, %s: no ExternalSecret changed, so the mutation tested nothing" % (comp, what))
        got = pod_template(docs, comp) == pt
        if got != same:
            fails.append("%s, %s: the server's pod template %s" % (comp, what, "changed: the server would restart" if same else "did not change: the server would keep the old credential"))
        return 1

    w, k = stage(tmp, comp + "-a", comp, non_data)
    if not k:
        fails.append("%s: no template holds an ExternalSecret to change" % comp)
    n += expect("(a) a change that decides no data", render(w, comp, spec, tmp), True)

    # (b) the ESO template of a key
    for name in SERVERS[comp][1]:
        def edit(f, s, name=name):
            if f == name:
                return s.replace("{{ .password }}\"", "x{{ .password }}\"").replace("\"{{ .password }}", "\"x{{ .password }}")
            return s
        w, k = stage(tmp, "%s-b-%s" % (comp, name), comp, edit)
        if k:
            n += expect("(b) another template in %s" % name, render(w, comp, spec, tmp), False)
    for key in ("passwordFrom", "erlang_cookie_from"):
        s2 = copy.deepcopy(spec)
        if walk(s2, key, lambda v: v.__setitem__("key", "gate-other-key")):
            n += expect("(b) another key read from the root (%s)" % key, render(base_w, comp, s2, tmp), False)
        s3 = copy.deepcopy(spec)
        if walk(s3, key, lambda v: v.__setitem__("secret", v["secret"] + "-gate2")):
            n += expect("(c) a rotation: another root Secret (%s)" % key, render(base_w, comp, s3, tmp), False)

    # the planted control: the whole-manifest hash must be caught on (a)
    w1, k = stage(tmp, comp + "-old", comp, old_hash)
    if not k:
        fails.append("%s's StatefulSet does not use helm-toolkit.utils.secret_data_hash" % comp)
        return n
    w2, _ = stage(tmp, comp + "-old-a", comp, lambda f, s: non_data(f, old_hash(f, s)))
    if pod_template(render(w1, comp, spec, tmp), comp) == pod_template(render(w2, comp, spec, tmp), comp):
        fails.append("%s: CONTROL: the whole-manifest hash does not notice (a), so (a) tests nothing" % comp)
    return n + 1


def main():
    if shutil.which("helm") is None:
        print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
        return 2
    specs = umbrella_spec.specs(ROOT, {"profile": "full-strict"})
    n = 0
    with tempfile.TemporaryDirectory() as tmp:
        for comp in sorted(SERVERS):
            if comp not in specs:
                fails.append("the umbrella delivers no spec for %s" % comp)
                continue
            k = check(comp, specs[comp], tmp)
            if k < 5:
                fails.append("%s: only %d comparison(s) ran, fewer than 5" % (comp, k))
            n += k
    for f in fails:
        print("FAIL: " + f)
    print("%d server(s), %d comparison(s), controls included; %d failure(s)" % (len(SERVERS), n, len(fails)))
    return 1 if fails else 0


sys.exit(main())
