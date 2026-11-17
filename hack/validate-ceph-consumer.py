#!/usr/bin/env python3
"""The install consumes Ceph through copies of keys Ceph mints, and never through client.admin (#308).

    hack/validate-ceph-consumer.py [repo-root]

With storage.backend ceph-rook or ceph-external, Ceph's side (a Rook CephClient, or the Ceph admin) creates the users and
holds their keys. The umbrella COPIES each key and the monitor addresses into this namespace (credentials.items with
`source: provided`), and the charts project the copies. Checked by rendering:

  27. PROVIDED ROOTS. With none: no provided root, no copy store, and every other object of the umbrella identical to its
      render without the provided items -- adding them changed nothing for an install without Ceph. With each ceph
      backend: exactly one root ExternalSecret per provided item, refreshPolicy Periodic with an interval (Ceph's side
      can rotate a key), the teardown annotation and NOT helm.sh/resource-policy: keep (a copy is recreated, not lost),
      read through the SecretStore <store>-ceph from the source the backend names (ceph-rook: Rook's Secret and key from
      storage.ceph.rook; ceph-external: storage.ceph.keys), the root holding exactly the item's key. The copy store reads
      storage.ceph.sourceNamespace; its Role is rendered only when that is the release namespace, listing exactly the
      source Secrets with get. The projection store's Role lists the provided roots only with a ceph backend.
      ceph-external with an empty source Secret is refused, naming it.

Fails when helm or PyYAML is missing, when the umbrella defines no provided item, and when a render it needs fails.
"""
import json
import os
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
BP = os.path.join(ROOT, "blueprints")
UMBRELLA = os.path.join(BP, "openstack", "chart")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)

NS = "openstack"
EXTERNAL_KEYS = {
    "cinder": {"secret": "gate-ceph-cinder-key", "key": "key"},
    "glance": {"secret": "gate-ceph-glance-key", "key": "key"},
    "monHost": {"secret": "gate-ceph-mon", "key": "mon_host"},
}
failures = []
counts = {}


def fail(m):
    failures.append(m)


def count(k):
    counts[k] = counts.get(k, 0) + 1


def stage(chart_dir, tmp, name, edit=None):
    w = os.path.join(tmp, name)
    shutil.copytree(chart_dir, w)
    cy = os.path.join(w, "Chart.yaml")
    txt = open(cy).read()
    open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
    if "helm-toolkit" in txt:
        os.makedirs(os.path.join(w, "charts"), exist_ok=True)
        shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
    if edit:
        edit(w)
    return w


def helm_template(chart, release, values):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values or {}, f)
    try:
        r = subprocess.run(["helm", "template", release, chart, "--namespace", NS, "--kube-version", "1.36.0",
                            "-f", f.name], capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    return r.returncode, r.stdout, r.stderr


def docs_of(out):
    return [d for d in yaml.safe_load_all(out) if isinstance(d, dict)]


def by(docs, kind, name=None):
    return [d for d in docs if d.get("kind") == kind and (name is None or d["metadata"]["name"] == name)]


def ceph_values(backend, source_ns=""):
    v = {"storage": {"backend": backend, "ceph": {"sourceNamespace": source_ns}}}
    if backend == "ceph-external":
        v["storage"]["ceph"]["keys"] = json.loads(json.dumps(EXTERNAL_KEYS))
    return v


def check27(tmp, uv):
    creds = uv["credentials"]
    store = creds["store"]
    provided = [i for i in creds["items"] if i.get("source") == "provided"]
    if not provided:
        print("FAILED MEASUREMENT: the umbrella defines no `source: provided` credential item")
        sys.exit(2)
    ceph = uv["storage"]["ceph"]
    u = stage(UMBRELLA, tmp, "umbrella")

    # none: nothing new, and nothing else moved
    rc, out, err = helm_template(u, "openstack-gate", {})
    if rc != 0:
        fail("27: the umbrella does not render at its defaults: %s" % err.strip()[:300])
        return
    d0 = docs_of(out)
    names = {i["secret"] for i in provided}
    for d in d0:
        n = d["metadata"]["name"]
        if n in names or n.startswith(store + "-ceph"):
            fail("27: with storage.backend none the umbrella renders %s/%s" % (d["kind"], n))
    for d in by(d0, "Role", store + "-reader"):
        listed = set(((d.get("rules") or [{}])[0]).get("resourceNames") or [])
        if listed & names:
            fail("27: with none, the projection store's Role lists provided roots %s" % sorted(listed & names))

    def strip(w):
        p = os.path.join(w, "values.yaml")
        vals = yaml.safe_load(open(p))
        vals["credentials"]["items"] = [i for i in vals["credentials"]["items"] if i.get("source") != "provided"]
        open(p, "w").write(yaml.safe_dump(vals, sort_keys=False))
    u2 = stage(UMBRELLA, tmp, "umbrella-noprovided", strip)
    rc, out2, err = helm_template(u2, "openstack-gate", {})
    if rc != 0:
        fail("27: the umbrella without its provided items does not render: %s" % err.strip()[:300])
    elif out2 != out:
        fail("27: with storage.backend none the umbrella renders differently with the provided items than without "
             "them: an install without Ceph must not change")
    count("27")

    # each ceph backend
    cases = [("ceph-rook", "rook-ceph"), ("ceph-rook", ""), ("ceph-external", ""), ("ceph-external", "ceph-keys")]
    for backend, src_ns in cases:
        label = "%s, sourceNamespace %r" % (backend, src_ns)
        rc, out, err = helm_template(u, "openstack-gate", ceph_values(backend, src_ns))
        if rc != 0:
            fail("27: %s: the umbrella does not render: %s" % (label, err.strip()[:300]))
            continue
        docs = docs_of(out)
        want_src = {}
        for i in provided:
            if backend == "ceph-rook":
                r = ceph["rook"]
                want_src[i["id"]] = ((r["monHostSecret"], r["monHostKey"]) if i["ceph"] == "monHost" else
                                     (r["clientSecretPrefix"] + ceph["clients"][i["ceph"]], r["clientKey"]))
            else:
                want_src[i["id"]] = (EXTERNAL_KEYS[i["ceph"]]["secret"], EXTERNAL_KEYS[i["ceph"]]["key"])
            es = by(docs, "ExternalSecret", i["secret"])
            if len(es) != 1:
                fail("27: %s: %d root ExternalSecret(s) %s, want 1" % (label, len(es), i["secret"]))
                continue
            e = es[0]
            ann = e["metadata"].get("annotations") or {}
            spec = e.get("spec") or {}
            if ann.get("krateo.io/teardown") != "with-release":
                fail("27: %s: %s lacks krateo.io/teardown: with-release (#254)" % (label, i["secret"]))
            if "helm.sh/resource-policy" in ann:
                fail("27: %s: %s carries helm.sh/resource-policy: a copy is recreated from its source, and keep would "
                     "leave a stale key behind an uninstall" % (label, i["secret"]))
            if spec.get("refreshPolicy") != "Periodic" or not spec.get("refreshInterval"):
                fail("27: %s: %s is refreshPolicy %r interval %r, want Periodic with an interval: Ceph's side can rotate "
                     "the key" % (label, i["secret"], spec.get("refreshPolicy"), spec.get("refreshInterval")))
            if (spec.get("secretStoreRef") or {}).get("name") != store + "-ceph":
                fail("27: %s: %s reads store %r, want %s-ceph" % (label, i["secret"],
                                                                (spec.get("secretStoreRef") or {}).get("name"), store))
            refs = [((x.get("remoteRef") or {}).get("key"), (x.get("remoteRef") or {}).get("property"))
                    for x in spec.get("data") or []]
            if refs != [want_src[i["id"]]]:
                fail("27: %s: %s copies %s, want %s" % (label, i["secret"], refs, [want_src[i["id"]]]))
            keys = sorted((((spec.get("target") or {}).get("template") or {}).get("data") or {}))
            if keys != [i["key"]] or (spec.get("target") or {}).get("name") != i["secret"]:
                fail("27: %s: %s targets %s holding %s, want itself holding [%s]" % (
                    label, i["secret"], (spec.get("target") or {}).get("name"), keys, i["key"]))
            count("27")
        st = by(docs, "SecretStore", store + "-ceph")
        want_ns = src_ns or NS
        if len(st) != 1 or (((st[0].get("spec") or {}).get("provider") or {}).get("kubernetes") or {}).get(
                "remoteNamespace") != want_ns:
            fail("27: %s: want one SecretStore %s-ceph reading namespace %s, got %s" % (
                label, store, want_ns, [(((s.get("spec") or {}).get("provider") or {}).get("kubernetes") or {})
                                         .get("remoteNamespace") for s in st]))
        roles = by(docs, "Role", store + "-ceph-reader")
        if want_ns == NS:
            srcs = sorted({v[0] for v in want_src.values()})
            rules = (roles[0].get("rules") or []) if roles else []
            secret_rules = [r for r in rules if "secrets" in (r.get("resources") or [])]
            if len(roles) != 1 or len(secret_rules) != 1 or sorted(secret_rules[0].get("resourceNames") or []) != srcs \
                    or secret_rules[0].get("verbs") != ["get"]:
                fail("27: %s: the copy store's Role must allow get on exactly %s, got %s" % (label, srcs, secret_rules))
            if not by(docs, "RoleBinding", store + "-ceph-reader"):
                fail("27: %s: no RoleBinding for the copy store's Role" % label)
        elif roles:
            fail("27: %s: a Role for the copy store is rendered in %s while the sources are in %s: that namespace's "
                 "owner grants it" % (label, NS, want_ns))
        if not by(docs, "ServiceAccount", store + "-ceph-reader"):
            fail("27: %s: no ServiceAccount %s-ceph-reader for the copy store" % (label, store))
        listed = set()
        for d in by(docs, "Role", store + "-reader"):
            for r in d.get("rules") or []:
                listed |= set(r.get("resourceNames") or [])
        if not names <= listed:
            fail("27: %s: the projection store's Role does not list %s, so no chart could project them"
                 % (label, sorted(names - listed)))
        count("27")

    # ceph-external with a source missing
    for k in EXTERNAL_KEYS:
        v = ceph_values("ceph-external")
        v["storage"]["ceph"]["keys"][k]["secret"] = ""
        rc, out, err = helm_template(u, "openstack-gate", v)
        if rc == 0:
            fail("27: ceph-external with storage.ceph.keys.%s.secret empty renders; it must refuse" % k)
        elif "storage.ceph.keys.%s.secret" % k not in err:
            fail("27: ceph-external with storage.ceph.keys.%s.secret empty is refused without naming it: %s"
                 % (k, err.strip()[-300:]))
        count("27")


def main():
    uv = yaml.safe_load(open(os.path.join(UMBRELLA, "values.yaml")))
    with tempfile.TemporaryDirectory() as tmp:
        check27(tmp, uv)
    for k in ("27",):
        if not counts.get(k):
            fail("%s: measured nothing" % k)
    if failures:
        print("FAIL: %d problem(s)" % len(failures))
        for m in failures:
            print("  " + m)
        sys.exit(1)
    print("OK: " + "; ".join("check %s: %d" % (k, v) for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
