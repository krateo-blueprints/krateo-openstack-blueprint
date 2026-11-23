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

  P.  PROJECTIONS. Each ceph-managed chart (cinder, glance in rbd, libvirt), rendered with the references the umbrella
      passes, renders exactly two ExternalSecrets through the umbrella's store: its keyring Secret (exactly `key`, from
      its user's root) and its ceph.conf Secret (exactly `ceph.conf`, mon_host from ceph-mon-host's root), each with the
      teardown annotation. Its ceph-etc and keyring volumes mount those two Secrets.
  22c. READER CLOSURE. hack/ceph-readers.yaml, measured per program, names the containers that receive each projection,
      per chart and mode (cinder as a Deployment and as a StatefulSet): exactly those mount it.
  18c. READERS RESTART. A reader holds the key it read at start (an init container's keyring file; libvirt's secret).
      Each long-running reader's pod template carries a checksum of the projected key as it is live, read with one
      lookup: rendered with a stand-in live Secret, the reader templates change with the key and nothing else does, and
      the checksum is of the Secret the pod mounts.
  28. NO CLIENT.ADMIN. In each ceph-managed render no pod references the admin keyring Secret (ceph_client.
      user_secret_name), no container runs or mounts ceph-admin-keyring.sh, no script a container runs creates a user or
      a pool (`ceph auth`, `osd pool create`), and no env var carries a key. Literal keys and hand-wired second Cephs are
      refused, naming the path; one reference without the other, references with a non-ceph backend (cinder), a non-rbd
      store (glance) or conf.ceph.enabled false (libvirt) are refused.

  29. ONE SOURCE, DELIVERED. Through hack/umbrella_spec.py (the umbrella's own spec-building lines), with each ceph
      backend and storage.ceph set to values no default has: only the Ceph consumers' specs differ from storage.backend
      none (cinder, libvirt, nova, and glance with glanceStore rbd); exactly the consumers get krateo.ceph references, to
      the provided roots, and nothing does with none. Each of the four charts, rendered with what it is delivered,
      renders, holds no client.admin (check 28), and agrees on every value storage.ceph sets: cinder's [rbd1] pool, user
      and secret UUID, glance's rbd store pool and user (and show_image_direct_url, and rbd as the default backend),
      libvirt's secret user and UUID, nova's [libvirt] rbd_user and rbd_secret_uuid. componentValues may set none of
      those, nor the references, nor glance's storage rbd/radosgw, nor conf.ceph.enabled on libvirt or nova: the
      umbrella refuses each, naming it.

  30. HELD UNTIL COPIED, AND THE GRANT. With a ceph backend a Ceph consumer is CREATED only once every provided root it
      lists reports Ready=True (osh.cephRootsReady, gating creation beside the dependency gate, never an existing
      component); and while the copy store reports Ready=False -- External Secrets Operator's own check that the read
      grant in storage.ceph.sourceNamespace exists -- the umbrella refuses, naming the namespace, the ServiceAccount and
      the exact Secrets to grant. Rendered with stand-in ExternalSecrets and SecretStore in place of the two named GETs,
      which are the chart's only reads of those kinds.
  18d. THE CHECKSUM CAN READ ITS SECRET. composition-dynamic-controller grants a release's ServiceAccount every verb on
      each kind its chart renders, with no resourceNames (internal/rbacgen/rbacgen.go, read 2026-11-18), so the
      checksum's lookup of the projected Secret is allowed exactly when the chart renders a Secret: each reader chart's
      ceph-managed render must. A denied lookup is not silent either way: Helm fails the render with the API's
      Forbidden (measured in this repo, osh.nodeReadGranted).

Fails when helm or PyYAML is missing, when the umbrella defines no provided item, and when a render it needs fails.
"""
import base64
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


CHARTS = {
    "cinder": {
        "item": "ceph-client-cinder", "keyring": "cinder-volume-rbd-keyring", "etc": "cinder-ceph-etc",
        "modes": {"deployment": {"krateo": {"storageBackend": "ceph-external"}},
                  "statefulset": {"krateo": {"storageBackend": "ceph-external"},
                                  "manifests": {"statefulset_volume": True}}},
        "restart": ["Deployment/cinder-volume", "StatefulSet/cinder-volume"],
        "literals": {"conf.ceph.admin_keyring": "AQgateliteral==", "ceph_client.enable_external_ceph_backend": True,
                     "ceph_client.external_ceph.rbd_user_keyring": "AQgateliteral==",
                     "backup.external_ceph_rbd.enabled": True, "backup.external_ceph_rbd.admin_keyring": "AQgate=="},
        "mismatch": [("krateo.storageBackend unmanaged", {"krateo": {"storageBackend": "unmanaged"}})],
    },
    "glance": {
        "item": "ceph-client-glance", "keyring": "images-rbd-keyring", "etc": "glance-ceph-etc",
        "modes": {"rbd": {"storage": "rbd"}},
        "restart": ["Deployment/glance-api"],
        "literals": {"conf.ceph.admin_keyring": "AQgateliteral=="},
        "mismatch": [("storage pvc", {"storage": "pvc"})],
    },
    "libvirt": {
        "item": "ceph-client-cinder", "keyring": "libvirt-ceph-cinder-keyring", "etc": "libvirt-ceph-etc",
        "modes": {"daemonset": {"conf": {"ceph": {"enabled": True}}}},
        "restart": ["DaemonSet/libvirt-libvirt-default"],
        "literals": {"conf.ceph.admin_keyring": "AQgateliteral==", "conf.ceph.cinder.keyring": "AQgateliteral==",
                     "conf.ceph.cinder.external_ceph.enabled": True},
        "mismatch": [("conf.ceph.enabled false", {"conf": {"ceph": {"enabled": False}}})],
    },
}
LOOKUP = 'lookup "v1" "Secret" $envAll.Release.Namespace $name'


def deep_merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            deep_merge(a[k], v)
        else:
            a[k] = v
    return a


def nested(path, value):
    out = cur = {}
    parts = path.split(".")
    for p in parts[:-1]:
        cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value
    return out


def ceph_refs(uv, item_id):
    items = {i["id"]: i for i in uv["credentials"]["items"]}
    store = uv["credentials"]["store"]
    k, m = items[item_id], items["ceph-mon-host"]
    return {"krateo": {"ceph": {"keyringFrom": {"store": store, "secret": k["secret"], "key": k["key"]},
                                "monHostFrom": {"store": store, "secret": m["secret"], "key": m["key"]}}}}


def pods(docs):
    for d in docs:
        if d.get("kind") not in ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"):
            continue
        spec = d.get("spec") or {}
        if d["kind"] == "CronJob":
            spec = (spec.get("jobTemplate") or {}).get("spec") or {}
        yield "%s/%s" % (d["kind"], d["metadata"]["name"]), (spec.get("template") or {})


def containers(pod):
    return ((pod.get("spec") or {}).get("initContainers") or []) + ((pod.get("spec") or {}).get("containers") or [])


def scripts(docs):
    out = {}
    for d in docs:
        if d.get("kind") in ("ConfigMap", "Secret"):
            for k, v in (d.get("data") or {}).items():
                if d["kind"] == "Secret":
                    try:
                        v = base64.b64decode(v).decode()
                    except (ValueError, UnicodeDecodeError):
                        continue
                out.setdefault(k, []).append(v or "")
    return out


def check_charts(tmp, uv):
    readers = (yaml.safe_load(open(os.path.join(ROOT, "hack", "ceph-readers.yaml"))) or {}).get("readers") or {}
    if sorted(readers) != sorted(CHARTS):
        fail("22c: hack/ceph-readers.yaml lists %s, the ceph-managed charts are %s" % (sorted(readers), sorted(CHARTS)))
    items = {i["id"]: i for i in uv["credentials"]["items"]}
    store = uv["credentials"]["store"]
    for chart, spec in CHARTS.items():
        cdir = os.path.join(BP, chart, "chart")
        cvals = yaml.safe_load(open(os.path.join(cdir, "values.yaml")))
        admin_secret = (cvals.get("ceph_client") or {}).get("user_secret_name")
        refs = ceph_refs(uv, spec["item"])
        w = stage(cdir, tmp, chart)
        rd = readers.get(chart) or {}
        if sorted(rd.get("modes") or []) != sorted(spec["modes"]):
            fail("22c: hack/ceph-readers.yaml renders %s in modes %s, this gate in %s" % (
                chart, rd.get("modes"), sorted(spec["modes"])))
        for mode, mv in spec["modes"].items():
            label = "%s (%s)" % (chart, mode)
            values = deep_merge(json.loads(json.dumps(mv)), json.loads(json.dumps(refs)))
            rc, out, err = helm_template(w, chart + "-gate1234", values)
            if rc != 0:
                fail("P: %s does not render ceph-managed: %s" % (label, err.strip()[-300:]))
                continue
            docs = docs_of(out)
            # P. projections
            es = {d["metadata"]["name"]: d for d in by(docs, "ExternalSecret")}
            want = {spec["keyring"]: (items[spec["item"]], "key"), spec["etc"]: (items["ceph-mon-host"], "ceph.conf")}
            for name, (item, key) in want.items():
                d = es.get(name)
                if not d:
                    fail("P: %s renders no ExternalSecret %s" % (label, name))
                    continue
                sp = d.get("spec") or {}
                if (d["metadata"].get("annotations") or {}).get("krateo.io/teardown") != "with-release":
                    fail("P: %s: %s lacks krateo.io/teardown: with-release" % (label, name))
                if (sp.get("secretStoreRef") or {}).get("name") != store:
                    fail("P: %s: %s reads store %r, want %s" % (label, name, (sp.get("secretStoreRef") or {}).get("name"), store))
                rr = [((x.get("remoteRef") or {}).get("key"), (x.get("remoteRef") or {}).get("property"))
                      for x in sp.get("data") or []]
                if rr != [(item["secret"], item["key"])]:
                    fail("P: %s: %s reads %s, want [(%s, %s)]" % (label, name, rr, item["secret"], item["key"]))
                data = ((sp.get("target") or {}).get("template") or {}).get("data") or {}
                if sorted(data) != [key] or (sp.get("target") or {}).get("name") != name:
                    fail("P: %s: %s holds %s, want exactly [%s]" % (label, name, sorted(data), key))
                if key == "ceph.conf" and "mon_host = {{ .mon_host }}" not in (data.get(key) or ""):
                    fail("P: %s: %s's ceph.conf does not set mon_host from the root: %r" % (label, name, data.get(key)))
                count("P")
            # 22c. reader closure
            got = {"keyring": set(), "etc": set()}
            for owner, pod in pods(docs):
                vols = {v["name"]: (v.get("secret") or {}).get("secretName")
                        for v in (pod.get("spec") or {}).get("volumes") or []}
                for c in containers(pod):
                    for m in c.get("volumeMounts") or []:
                        sec = vols.get(m["name"])
                        if sec == spec["keyring"]:
                            got["keyring"].add((owner, c["name"]))
                        if sec == spec["etc"]:
                            got["etc"].add((owner, c["name"]))
            present = {owner for owner, _ in pods(docs)}
            for what in ("keyring", "etc"):
                listed = {(wl, e["container"]) for e in rd.get(what) or [] for wl in e.get("workloads") or []
                          if wl in present}
                for x in sorted(got[what] - listed):
                    fail("22c: %s: %s %s receives the %s projection, and hack/ceph-readers.yaml does not list it"
                         % (label, x[0], x[1], what))
                for x in sorted(listed - got[what]):
                    fail("22c: %s: %s %s is listed as a %s reader and does not receive it" % (label, x[0], x[1], what))
                if not got[what]:
                    fail("22c: %s: nothing receives the %s projection" % (label, what))
                count("22c")
            # 28. no client.admin
            scr = scripts(docs)
            for owner, pod in pods(docs):
                for v in (pod.get("spec") or {}).get("volumes") or []:
                    if admin_secret and (v.get("secret") or {}).get("secretName") == admin_secret:
                        fail("28: %s: %s mounts %s, the client.admin keyring" % (label, owner, admin_secret))
                for c in containers(pod):
                    cmd = " ".join(c.get("command") or []) + " " + " ".join(c.get("args") or [])
                    subs = " ".join(m.get("subPath") or "" for m in c.get("volumeMounts") or [])
                    if "ceph-admin-keyring" in cmd or "ceph-admin-keyring" in subs:
                        fail("28: %s: %s %s runs or mounts ceph-admin-keyring.sh" % (label, owner, c["name"]))
                    for e in c.get("env") or []:
                        if "KEYRING" in e.get("name", "") and e.get("value"):
                            fail("28: %s: %s %s carries a key in env %s" % (label, owner, c["name"], e["name"]))
                    for tok in (c.get("command") or []):
                        base = os.path.basename(tok)
                        for body in scr.get(base, []):
                            for bad in ("ceph auth", "osd pool create"):
                                if bad in body:
                                    fail("28: %s: %s %s runs %s, which contains `%s`" % (label, owner, c["name"], base, bad))
                count("28")
            # 18c. readers restart
            if mode == list(spec["modes"])[0] or chart == "cinder":
                restart_check(tmp, chart, cdir, label, values, spec)
        # refusals
        base = deep_merge(json.loads(json.dumps(list(spec["modes"].values())[0])), json.loads(json.dumps(refs)))
        for path, val in spec["literals"].items():
            v = deep_merge(json.loads(json.dumps(base)), nested(path, val))
            rc, out, err = helm_template(w, chart + "-gate1234", v)
            if rc == 0:
                fail("28: %s ceph-managed with %s set renders; it must refuse" % (chart, path))
            elif path not in err:
                fail("28: %s refuses %s without naming it: %s" % (chart, path, err.strip()[-200:]))
            count("28")
        for half in ("keyringFrom", "monHostFrom"):
            v = json.loads(json.dumps(base))
            del v["krateo"]["ceph"][half]
            rc, out, err = helm_template(w, chart + "-gate1234", v)
            if rc == 0 or "come together" not in err:
                fail("28: %s with krateo.ceph without %s %s" % (chart, half, "renders" if rc == 0 else
                                                                 "is refused without saying both are needed"))
            count("28")
        for what, mv in spec["mismatch"]:
            v = deep_merge(json.loads(json.dumps(base)), mv)
            rc, out, err = helm_template(w, chart + "-gate1234", v)
            if rc == 0:
                fail("28: %s with krateo.ceph references and %s renders; it must refuse" % (chart, what))
            count("28")


def restart_check(tmp, chart, cdir, label, values, spec):
    tk = open(os.path.join(TOOLKIT, "templates", "manifests", "_ceph_client.tpl")).read()
    if tk.count(LOOKUP) != 1:
        fail("18c: helm-toolkit's ceph_key_checksum must read the projection with exactly one `%s` (found %d)"
             % (LOOKUP, tk.count(LOOKUP)))
        return
    renders = {}
    for tag, stub in (("a", '(dict "data" (dict "key" $name))'),
                      ("b", '(dict "data" (dict "key" (printf "%s-rotated" $name)))')):
        def patch(w, stub=stub):
            p = os.path.join(w, "charts", "helm-toolkit", "templates", "manifests", "_ceph_client.tpl")
            body = open(p).read()
            open(p, "w").write(body.replace(LOOKUP, stub))
        w = stage(cdir, tmp, "%s-restart-%s-%s" % (chart, tag, abs(hash(label)) % 10000), patch)
        rc, out, err = helm_template(w, chart + "-gate1234", values)
        if rc != 0:
            fail("18c: %s does not render with a stand-in live Secret: %s" % (label, err.strip()[-200:]))
            return
        renders[tag] = {o: p for o, p in pods(docs_of(out))}
    import hashlib
    for owner in sorted(set(renders["a"]) | set(renders["b"])):
        a, b = renders["a"].get(owner), renders["b"].get(owner)
        if owner in spec["restart"]:
            ann = ((a or {}).get("metadata") or {}).get("annotations") or {}
            want = hashlib.sha256(spec["keyring"].encode()).hexdigest()
            if ann.get("krateo.io/ceph-key-checksum") != want:
                fail("18c: %s: %s's checksum is %r, want the checksum of %s, the key Secret it mounts"
                     % (label, owner, ann.get("krateo.io/ceph-key-checksum"), spec["keyring"]))
            if a == b:
                fail("18c: %s: %s's pod template does not change when the live key does, so it keeps the old key"
                     % (label, owner))
            count("18c")
        elif a != b:
            fail("18c: %s: %s's pod template changes with the Ceph key, which it does not read" % (label, owner))


def load_umbrella_spec():
    import importlib.util
    sp = importlib.util.spec_from_file_location("umbrella_spec", os.path.join(ROOT, "hack", "umbrella_spec.py"))
    mod = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(mod)
    return mod


def ini_files(docs, name):
    import configparser
    out = []
    for body in scripts(docs).get(name, []):
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        try:
            cp.read_string(body)
        except configparser.Error as e:
            fail("29: %s does not parse: %s" % (name, e))
            continue
        out.append(cp)
    return out


def no_admin(label, docs, admin_secret):
    scr = scripts(docs)
    for owner, pod in pods(docs):
        for v in (pod.get("spec") or {}).get("volumes") or []:
            if admin_secret and (v.get("secret") or {}).get("secretName") == admin_secret:
                fail("28: %s: %s mounts %s, the client.admin keyring" % (label, owner, admin_secret))
        for c in containers(pod):
            cmd = " ".join(c.get("command") or []) + " " + " ".join(c.get("args") or [])
            subs = " ".join(m.get("subPath") or "" for m in c.get("volumeMounts") or [])
            if "ceph-admin-keyring" in cmd or "ceph-admin-keyring" in subs:
                fail("28: %s: %s %s runs or mounts ceph-admin-keyring.sh" % (label, owner, c["name"]))
            for e in c.get("env") or []:
                if "KEYRING" in e.get("name", "") and e.get("value"):
                    fail("28: %s: %s %s carries a key in env %s" % (label, owner, c["name"], e["name"]))
            for tok in (c.get("command") or []):
                for body in scr.get(os.path.basename(tok), []):
                    for bad in ("ceph auth", "osd pool create"):
                        if bad in body:
                            fail("28: %s: %s %s runs %s, which contains `%s`" % (label, owner, c["name"],
                                                                               os.path.basename(tok), bad))
    count("28")


def check29(tmp, uv):
    us = load_umbrella_spec()
    items = {i["id"]: i for i in uv["credentials"]["items"]}
    comps = {c["name"]: c for c in uv["components"]}
    consumers = {n: c.get("ceph") for n, c in comps.items() if c.get("ceph")}
    if sorted(consumers) != ["cinder", "glance", "libvirt"]:
        fail("29: the umbrella's Ceph consumers (components[].ceph) are %s, this gate checks cinder, glance and libvirt"
             % sorted(consumers))
    try:
        none = us.specs(ROOT, {})
    except us.SpecError as e:
        fail("29: %s" % e)
        return
    for n, sp in none.items():
        if (sp.get("krateo") or {}).get("ceph"):
            fail("29: with storage.backend none, %s receives krateo.ceph references" % n)
    custom = {"sourceNamespace": "gate-rook", "clients": {"cinder": "gate-cinder", "glance": "gate-glance"},
              "pools": {"volumes": "gate-volumes", "images": "gate-images"},
              "libvirtSecretUUID": "29292929-2929-4292-9292-292929292929", "glanceStore": "rbd"}
    ext = us.ceph_external_values()
    cases = [("ceph-rook, custom storage.ceph, glanceStore rbd", {"storage": {"backend": "ceph-rook", "ceph": custom}}),
             ("ceph-external, defaults, glanceStore pvc", {"storage": ext})]
    for label, values in cases:
        try:
            got = us.specs(ROOT, values)
        except us.SpecError as e:
            fail("29: %s: %s" % (label, e))
            continue
        ceph = deep_merge(json.loads(json.dumps(uv["storage"]["ceph"])), values["storage"].get("ceph") or {})
        rbd_glance = ceph.get("glanceStore") == "rbd"
        want_changed = {"cinder", "libvirt", "nova"} | ({"glance"} if rbd_glance else set())
        changed = {n for n in got if got[n] != none.get(n)}
        if changed != want_changed:
            fail("29: %s: the specs that differ from storage.backend none are %s, want %s" % (
                label, sorted(changed), sorted(want_changed)))
        for n, sp in got.items():
            refs = (sp.get("krateo") or {}).get("ceph")
            wants = n in consumers and (consumers[n] == "volumes" or rbd_glance)
            if wants:
                k = items["ceph-client-glance" if n == "glance" else "ceph-client-cinder"]
                exp = {"keyringFrom": {"store": uv["credentials"]["store"], "secret": k["secret"], "key": k["key"]},
                       "monHostFrom": {"store": uv["credentials"]["store"], "secret": items["ceph-mon-host"]["secret"],
                                       "key": items["ceph-mon-host"]["key"]}}
                if refs != exp:
                    fail("29: %s: %s receives krateo.ceph %s, want %s" % (label, n, refs, exp))
            elif refs:
                fail("29: %s: %s receives krateo.ceph references and consumes no Ceph here" % (label, n))
        count("29")
        uuid, cuser, guser = ceph["libvirtSecretUUID"], ceph["clients"]["cinder"], ceph["clients"]["glance"]
        for n in sorted(want_changed):
            cdir = os.path.join(BP, n, "chart")
            w = stage(cdir, tmp, "%s-29-%d" % (n, cases.index((label, values))))
            rc, out, err = helm_template(w, n + "-gate1234", got[n])
            if rc != 0:
                fail("29: %s: %s does not render with what the umbrella delivers: %s" % (label, n, err.strip()[-300:]))
                continue
            docs = docs_of(out)
            cvals = yaml.safe_load(open(os.path.join(cdir, "values.yaml")))
            no_admin("%s (%s)" % (n, label), docs, (cvals.get("ceph_client") or {}).get("user_secret_name"))
            seen = []
            if n == "cinder":
                for cp in ini_files(docs, "backends.conf"):
                    seen.append({k: cp.get("rbd1", k, fallback=None) for k in ("rbd_pool", "rbd_user", "rbd_secret_uuid")})
                want = [{"rbd_pool": ceph["pools"]["volumes"], "rbd_user": cuser, "rbd_secret_uuid": uuid}]
            elif n == "glance":
                for cp in ini_files(docs, "glance-api.conf"):
                    for sec in ("rbd", "glance_store"):
                        seen.append({k: cp.get(sec, k, fallback=None) for k in ("rbd_store_pool", "rbd_store_user")})
                    if cp.get("DEFAULT", "show_image_direct_url", fallback="").lower() != "true":
                        fail("29: %s: glance does not set show_image_direct_url, so images are not cloned" % label)
                    if cp.get("glance_store", "default_backend", fallback="") != "rbd":
                        fail("29: %s: glance's default_backend is %r, want rbd" % (
                            label, cp.get("glance_store", "default_backend", fallback="")))
                want = [{"rbd_store_pool": ceph["pools"]["images"], "rbd_store_user": guser}] * 2
            elif n == "libvirt":
                for owner, pod in pods(docs):
                    for c in containers(pod):
                        env = {e["name"]: e.get("value") for e in c.get("env") or []}
                        if "LIBVIRT_CEPH_CINDER_SECRET_UUID" in env:
                            seen.append({"uuid": env["LIBVIRT_CEPH_CINDER_SECRET_UUID"], "user": env.get("CEPH_CINDER_USER")})
                want = [{"uuid": uuid, "user": cuser}] * 2
            else:
                for cp in ini_files(docs, "nova.conf"):
                    seen.append({k: cp.get("libvirt", k, fallback=None) for k in ("rbd_user", "rbd_secret_uuid")})
                want = [{"rbd_user": cuser, "rbd_secret_uuid": uuid}]
            if not seen:
                fail("29: %s: nothing in %s's render carries the values storage.ceph sets: nothing was measured" % (label, n))
            for x in seen:
                if x != want[0]:
                    fail("29: %s: %s renders %s, want %s (storage.ceph)" % (label, n, x, want[0]))
            if len(seen) < len(want):
                fail("29: %s: %s renders those values %d time(s), want %d" % (label, n, len(seen), len(want)))
            count("29")
    # componentValues may not set what storage.ceph sets
    u = stage(UMBRELLA, tmp, "umbrella-29")
    rook = {"backend": "ceph-rook", "ceph": {"glanceStore": "rbd"}}
    refused = [
        ("cinder", "conf.backends.rbd1.rbd_pool", "other", rook),
        ("cinder", "conf.backends.rbd1.rbd_secret_uuid", "11111111-1111-4111-8111-111111111111", rook),
        ("cinder", "krateo.ceph", {"keyringFrom": {"secret": "x"}}, rook),
        ("glance", "conf.glance.rbd.rbd_store_pool", "other", rook),
        ("libvirt", "conf.ceph.cinder.secret_uuid", "11111111-1111-4111-8111-111111111111", rook),
        ("nova", "conf.nova.libvirt.rbd_secret_uuid", "11111111-1111-4111-8111-111111111111", rook),
        ("libvirt", "conf.ceph.enabled", True, {"backend": "none"}),
        ("nova", "conf.ceph.enabled", True, {"backend": "none"}),
        ("glance", "storage", "rbd", {"backend": "none"}),
        ("glance", "storage", "radosgw", {"backend": "none"}),
    ]
    for comp, path, val, storage in refused:
        v = {"profile": "full", "storage": storage, "componentValues": {comp: nested(path, val)}}
        rc, out, err = helm_template(u, "openstack-gate", v)
        name = "componentValues.%s.%s" % (comp, path)
        if rc == 0:
            fail("29: %s (storage.backend %s) renders; the umbrella must refuse it" % (name, storage["backend"]))
        elif name not in err:
            fail("29: %s is refused without naming it: %s" % (name, err.strip()[-250:]))
        count("29")
    for comp, path, val in (("libvirt", "conf.ceph.enabled", False), ("glance", "storage", "pvc")):
        v = {"profile": "full", "componentValues": {comp: nested(path, val)}}
        rc, out, err = helm_template(u, "openstack-gate", v)
        if rc != 0:
            fail("29: componentValues.%s.%s %r is refused; only what reaches Ceph by hand is" % (comp, path, val))
        count("29")


ES_LOOKUP = '{{- lookup "external-secrets.io/v1" "ExternalSecret" $top.Release.Namespace (index . 1) | default dict | toJson -}}'
STORE_LOOKUP = ('{{- lookup "external-secrets.io/v1" "SecretStore" $top.Release.Namespace '
                '(printf "%s-ceph" $top.Values.credentials.store) | default dict | toJson -}}')


def ready_obj(status, message=""):
    return {"status": {"conditions": [{"type": "Ready", "status": status, "message": message}]}}


def check30(tmp, uv):
    helpers = open(os.path.join(UMBRELLA, "templates", "_helpers.tpl")).read()
    comp = open(os.path.join(UMBRELLA, "templates", "compositions.yaml")).read()
    import re
    for lk in (ES_LOOKUP, STORE_LOOKUP):
        if helpers.count(lk) != 1:
            fail("30: _helpers.tpl must read with exactly one `%s` (found %d)" % (lk[:70], helpers.count(lk)))
            return
    for kind in ("ExternalSecret", "SecretStore"):
        n = sum(len(re.findall(r'lookup\s+"external-secrets\.io/v1"\s+"%s"' % kind, open(os.path.join(dp, f)).read()))
                for dp, _, fs in os.walk(os.path.join(UMBRELLA, "templates")) for f in fs)
        if n != 1:
            fail("30: the umbrella reads %s %d time(s); osh.lookup* must be the only read" % (kind, n))
    if '{{- $cephReady := include "osh.cephRootsReady" (list $ $c) }}' not in comp:
        fail("30: compositions.yaml does not compute $cephReady from osh.cephRootsReady for each component")
    gate = [l for l in comp.splitlines() if l.startswith('{{- if and (eq $crd "true")')]
    # The structure, not the exact text: other creation gates sit in the same `and` (osh.messagingProjectionReady,
    # #309), so the check is that $cephReady is one of the conjuncts beside the dependency gates, inside the `and` that
    # an existing component bypasses (`or ... (eq $exists "true")`).
    m = re.search(r'\(or \(and ((?:\(eq \$[A-Za-z]+ "true"\) ?)+)\) \(eq \$exists "true"\)\)', gate[0]) if len(gate) == 1 else None
    conj = set(re.findall(r'\$([A-Za-z]+)', m.group(1))) if m else set()
    if not {"strictReady", "relaxedExist", "cephReady"} <= conj:
        fail("30: the creation gate must hold a component on $cephReady beside its dependencies, and never an existing "
             "one: want $strictReady, $relaxedExist and $cephReady in the `and` that `(eq $exists \"true\")` bypasses, "
             "in `%s`" % gate)
    if 'include "osh.requireCephReadGrant" (list $)' not in comp:
        fail("30: compositions.yaml does not call osh.requireCephReadGrant")

    def stubbed(w):
        p = os.path.join(w, "templates", "_helpers.tpl")
        t = open(p).read()
        t = t.replace(ES_LOOKUP, '{{- index (($top.Values.gateLookup | default dict).es | default dict) (index . 1) | default dict | toJson -}}')
        t = t.replace(STORE_LOOKUP, '{{- ($top.Values.gateLookup | default dict).store | default dict | toJson -}}')
        open(p, "w").write(t)
        open(os.path.join(w, "templates", "zz-ceph-hold-probe.yaml"), "w").write(
            '{{- $dr := include "osh.deregistration" (list $ (splitList " " (include "osh.selected" (list $))) (list)) | fromYaml }}\n{{- range $c := .Values.components }}\n'
            '{{ printf "\\n# CEPHREADY %s %s\\n" $c.name (include "osh.cephRootsReady" (list $ $c)) }}\n{{- end }}\n')
    u = stage(UMBRELLA, tmp, "umbrella-30", stubbed)
    items = {i["id"]: i for i in uv["credentials"]["items"]}
    roots = {k: items[k]["secret"] for k in ("ceph-client-cinder", "ceph-client-glance", "ceph-mon-host")}
    rook_rbd = {"backend": "ceph-rook", "ceph": {"glanceStore": "rbd", "sourceNamespace": "rook-ceph"}}
    allready = {n: ready_obj("True") for n in roots.values()}
    cases = [
        ("none, nothing copied", {}, {}, set()),
        ("ceph-rook, glanceStore rbd, nothing copied yet", {"storage": rook_rbd}, {}, {"cinder", "libvirt", "glance"}),
        ("ceph-rook, every copy Ready", {"storage": rook_rbd}, {"es": allready}, set()),
        ("ceph-rook, mon_host's copy Ready=False", {"storage": rook_rbd},
         {"es": dict(allready, **{roots["ceph-mon-host"]: ready_obj("False", "x")})}, {"cinder", "libvirt", "glance"}),
        ("ceph-rook, glance's copy missing", {"storage": rook_rbd},
         {"es": {k: v for k, v in allready.items() if k != roots["ceph-client-glance"]}}, {"glance"}),
        ("ceph-rook, glanceStore pvc, nothing copied", {"storage": {"backend": "ceph-rook"}}, {}, {"cinder", "libvirt"}),
    ]
    for label, values, stub, held in cases:
        v = dict(values, gateLookup=stub, profile="full")
        rc, out, err = helm_template(u, "openstack-gate", v)
        if rc != 0:
            fail("30: %s: the umbrella does not render: %s" % (label, err.strip()[-250:]))
            continue
        got = {l.split()[2]: l.split()[3] for l in out.splitlines() if l.startswith("# CEPHREADY ")}
        if not got:
            fail("30: %s: the probe printed nothing" % label)
            continue
        now_held = {n for n, r in got.items() if r != "true"}
        if now_held != held:
            fail("30: %s: held %s, want %s" % (label, sorted(now_held), sorted(held)))
        count("30")
    # the read grant
    src = ["rook-ceph-client-cinder", "rook-ceph-client-glance", "rook-ceph-config"]
    for label, store, refused in (("store Ready=False", ready_obj("False", "client is not allowed to get secrets"), True),
                                  ("store Ready=True", ready_obj("True"), False),
                                  ("store not validated yet", {}, False)):
        v = {"profile": "full", "storage": rook_rbd, "gateLookup": {"store": store, "es": allready}}
        rc, out, err = helm_template(u, "openstack-gate", v)
        if refused:
            if rc == 0:
                fail("30: %s: renders; a store ESO reports unable to read must refuse the render" % label)
            else:
                for frag in ["rook-ceph", "%s/%s-ceph-reader" % (NS, uv["credentials"]["store"]),
                             ", ".join(src), "client is not allowed to get secrets"]:
                    if frag not in err:
                        fail("30: %s: the refusal does not name %r: %s" % (label, frag, err.strip()[-300:]))
        elif rc != 0:
            fail("30: %s: refused; only a store ESO reports Ready=False refuses: %s" % (label, err.strip()[-200:]))
        count("30")
    v = {"profile": "full", "gateLookup": {"store": ready_obj("False", "x")}}
    rc, out, err = helm_template(u, "openstack-gate", v)
    if rc != 0:
        fail("30: with storage.backend none a copy store's state refuses the render: %s" % err.strip()[-200:])
    count("30")


def check18d(tmp, uv):
    for chart, spec in CHARTS.items():
        cdir = os.path.join(BP, chart, "chart")
        w = stage(cdir, tmp, chart + "-18d")
        values = deep_merge(json.loads(json.dumps(list(spec["modes"].values())[0])), ceph_refs(uv, spec["item"]))
        rc, out, err = helm_template(w, chart + "-gate1234", values)
        if rc != 0:
            fail("18d: %s does not render ceph-managed: %s" % (chart, err.strip()[-200:]))
            continue
        if not [d for d in docs_of(out) if d.get("kind") == "Secret" and d.get("apiVersion") == "v1"]:
            fail("18d: %s's ceph-managed render has no v1 Secret, so composition-dynamic-controller grants its release "
                 "nothing on Secrets and the key checksum's lookup is Forbidden" % chart)
        count("18d")


def main():
    uv = yaml.safe_load(open(os.path.join(UMBRELLA, "values.yaml")))
    with tempfile.TemporaryDirectory() as tmp:
        check27(tmp, uv)
        check_charts(tmp, uv)
        check29(tmp, uv)
        check30(tmp, uv)
        check18d(tmp, uv)
    for k in ("27", "P", "22c", "18c", "18d", "28", "29", "30"):
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
