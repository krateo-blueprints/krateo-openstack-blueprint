#!/usr/bin/env python3
"""cinder renders only with the block-storage backend the umbrella's storage.backend names (#196).

    hack/validate-storage-backend.py [repo-root]

cinder's only shipped backend is Ceph RBD. With no Ceph, cinder-volume and cinder-storage-init mounted the ConfigMap
ceph-etc and the keyring Secrets, which nothing creates, and waited in Init forever: measured on #179 and on a cold
0.3.42 install (#204). Init is the state a pod census can only count. So the umbrella's storage.backend (none by default)
reaches every component that requires block-storage as its krateo.storageBackend, and the chart refuses to render
without a backend, naming the value. Each half is checked here, by rendering:

  1. THE UMBRELLA DELIVERS IT. The lines of compositions.yaml that build a component's spec (from `{{- $ov := ` through
     the storageBackend merge) are rendered for every component, once per storage.backend the schema accepts: each
     component whose components[].requires names block-storage receives exactly that value as krateo.storageBackend,
     none included, and no other component carries one. Which specs a ceph backend changes at all (the Ceph consumers'
     references and storage values, #308) is hack/validate-ceph-consumer.py's check 29. The component set must be
     non-empty and every backend must render, or nothing was measured.
  2. ONE PLACE SAYS IT. A componentValues.<component>.krateo.storageBackend is refused for each such component, with a
     message naming storage.backend, whatever its value (unmanaged included: it would install upstream's wiring under a
     full-strict that left cinder out). The umbrella's schema refuses a backend it does not list.
  3. THE CHART REFUSES. Rendered with krateo.storageBackend none, cinder fails, naming storage.backend and #196; with each
     ceph backend but without the krateo.ceph references the umbrella passes with it, it fails naming that backend and
     the references (#308); with them it renders, which hack/validate-ceph-consumer.py checks. Its own
     default is `unmanaged` (the chart as OpenStack-Helm ships it, for a standalone install), which renders
     cinder-volume: the other gates render each chart at its defaults and must keep seeing cinder's pods.
  4. ONE LIBVIRT SECRET UUID. nova-compute's DaemonSet read `.Values.conf.ceph.secret_uuid`, which nova's values never
     define, so LIBVIRT_CEPH_SECRET_UUID rendered with no value (`value:`, upstream's defect). nova and libvirt are rendered with
     conf.ceph.enabled and a sentinel at conf.ceph.cinder.secret_uuid: every LIBVIRT_CEPH_SECRET_UUID (nova) and
     LIBVIRT_CEPH_CINDER_SECRET_UUID (libvirt) must be the sentinel, and at least two of nova's and one of libvirt's must
     exist. The defaults that must name the same libvirt secret agree: cinder's conf.backends.rbd1.rbd_secret_uuid,
     nova's conf.ceph.cinder.secret_uuid and conf.nova.libvirt.rbd_secret_uuid, libvirt's conf.ceph.cinder.secret_uuid.

Fails when helm or PyYAML is missing and when any render it needs does not happen.
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

SENTINEL_UUID = "0b5e7a1c-5e47-4c1e-9f00-196196196196"
REFUSE_NONE = "storage.backend is none (#196)"
REFUSE_LATER = "renders only with krateo.ceph.keyringFrom and krateo.ceph.monHostFrom (#308)"
failures = []
counts = {}


def fail(m):
    failures.append(m)


def count(k):
    counts[k] = counts.get(k, 0) + 1


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


def pod_specs(docs):
    for d in docs:
        spec = d.get("spec") or {}
        if d.get("kind") == "CronJob":
            spec = (spec.get("jobTemplate") or {}).get("spec") or {}
        if d.get("kind") in ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"):
            yield d, (spec.get("template") or {}).get("spec") or {}


def env_values(docs, name):
    out = []
    for d, pod in pod_specs(docs):
        for c in (pod.get("initContainers") or []) + (pod.get("containers") or []):
            for e in c.get("env") or []:
                if e.get("name") == name:
                    out.append(("%s/%s %s" % (d["kind"], d["metadata"]["name"], c.get("name")), e.get("value")))
    return out


def storage_values(backend):
    """storage values that render with `backend`: ceph-external names its source Secrets or is refused (#308)."""
    v = {"backend": backend}
    if backend == "ceph-external":
        v["ceph"] = {"keys": {k: {"secret": "gate-" + k, "key": "key"} for k in ("cinder", "glance", "monHost")}}
    return v


def dig(d, path):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def main():
    uv = yaml.safe_load(open(os.path.join(UMBRELLA, "values.yaml")))
    comps = uv.get("components") or []
    names = [c["name"] for c in comps]
    block = sorted(c["name"] for c in comps if "block-storage" in (c.get("requires") or []))
    schema = json.load(open(os.path.join(UMBRELLA, "values.schema.json")))
    backends = (dig(schema, "properties.storage.properties.backend") or {}).get("enum") or []
    if not names or not block or "none" not in backends or len(backends) < 2:
        print("FAILED MEASUREMENT: components %s, block-storage consumers %s, backends %s: need all three, with none "
              "and another backend" % (len(names), block, backends))
        sys.exit(2)
    with tempfile.TemporaryDirectory() as tmp:
        u = stage(UMBRELLA, tmp, "umbrella")
        # 1. the umbrella delivers it
        comp = open(os.path.join(UMBRELLA, "templates", "compositions.yaml")).read()
        start = comp.find("{{- $ov := ")
        mark = comp.find('"storageBackend"', start)
        end = comp.find("{{- end }}", mark) if mark >= 0 else -1
        if start < 0 or end < 0:
            fail("1: compositions.yaml builds no krateo.storageBackend into a component spec (the span from "
                 "`{{- $ov := ` through the storageBackend merge), so no component receives storage.backend")
        else:
            span = comp[start:end + len("{{- end }}")]
            probe = os.path.join(u, "templates", "zz-gate-storage-probe.yaml")
            open(probe, "w").write(
                '{{- $sel := splitList " " (include "osh.selected" (list $)) }}\n'
                '{{- range $c := .Values.components }}\n' + span + '\n'
                '# SBSPEC {{ $c.name }} {{ $ov | toJson }}\n{{- end }}\n')
            specs = {}
            for b in backends:
                rc, out, err = helm_template(u, "openstack-gate", {"profile": "full", "storage": storage_values(b)})
                if rc != 0:
                    fail("1: the umbrella probe does not render with storage.backend %s: %s" % (b, err.strip()[:300]))
                    continue
                specs[b] = {l.split(" ", 3)[2]: json.loads(l.split(" ", 3)[3])
                            for l in out.splitlines() if l.startswith("# SBSPEC ")}
                if sorted(specs[b]) != sorted(names):
                    fail("1: with storage.backend %s the probe saw %d component spec(s), the umbrella lists %d"
                         % (b, len(specs[b]), len(names)))
            for b, got in specs.items():
                for n in names:
                    sb = dig(got.get(n) or {}, "krateo.storageBackend")
                    if n in block:
                        if sb != b:
                            fail("1: with storage.backend %s, %s receives krateo.storageBackend %r" % (b, n, sb))
                    elif sb is not None:
                        fail("1: %s requires no block-storage and still receives krateo.storageBackend %r" % (n, sb))
                    count("1")
            os.unlink(probe)
        # 2. one place says it
        for n in block:
            for v in ("unmanaged", "none", backends[-1]):
                rc, out, err = helm_template(u, "openstack-gate", {
                    "profile": "full", "componentValues": {n: {"krateo": {"storageBackend": v}}}})
                if rc == 0:
                    fail("2: componentValues.%s.krateo.storageBackend %s renders; the umbrella must refuse it" % (n, v))
                elif "Set storage.backend instead" not in err:
                    fail("2: componentValues.%s.krateo.storageBackend %s is refused without naming storage.backend: %s"
                         % (n, v, err.strip()[-300:]))
                count("2")
        rc, out, err = helm_template(u, "openstack-gate", {"profile": "full"})
        if rc != 0:
            fail("2: the umbrella does not render at its defaults: %s" % err.strip()[:300])
        rc, out, err = helm_template(u, "openstack-gate", {"storage": {"backend": "lvm"}})
        if rc == 0:
            fail("2: storage.backend lvm renders; the umbrella's schema must refuse a backend it does not list")
        count("2")
        # 3. the chart refuses
        for n in block:
            cdir = os.path.join(BP, n, "chart")
            cvals = yaml.safe_load(open(os.path.join(cdir, "values.yaml")))
            if dig(cvals, "krateo.storageBackend") != "unmanaged":
                fail("3: %s's values.yaml krateo.storageBackend is %r, want unmanaged: every gate renders each chart "
                     "at its defaults and must keep seeing %s's pods" % (n, dig(cvals, "krateo.storageBackend"), n))
            c = stage(cdir, tmp, n)
            rc, out, err = helm_template(c, n + "-gate1234", {})
            if rc != 0:
                fail("3: %s does not render at its default (unmanaged): %s" % (n, err.strip()[:300]))
            elif not [d for d in yaml.safe_load_all(out) if isinstance(d, dict)
                      and d.get("kind") in ("Deployment", "StatefulSet") and "volume" in d["metadata"]["name"]]:
                fail("3: %s at its default (unmanaged) renders no volume workload" % n)
            for b in backends:
                rc, out, err = helm_template(c, n + "-gate1234", {"krateo": {"storageBackend": b}})
                want = REFUSE_NONE if b == "none" else REFUSE_LATER
                if rc == 0:
                    fail("3: %s renders with krateo.storageBackend %s; it must refuse (%r)" % (n, b, want))
                elif want not in err or (b != "none" and b not in err):
                    fail("3: %s refuses krateo.storageBackend %s without its message (%r, naming the backend): %s"
                         % (n, b, want, err.strip()[-300:]))
                count("3")
        # 4. one libvirt secret UUID
        for chart, var, least in (("nova", "LIBVIRT_CEPH_SECRET_UUID", 2), ("libvirt", "LIBVIRT_CEPH_CINDER_SECRET_UUID", 1)):
            c = stage(os.path.join(BP, chart, "chart"), tmp, chart)
            rc, out, err = helm_template(c, chart + "-gate1234", {
                "conf": {"ceph": {"enabled": True, "cinder": {"secret_uuid": SENTINEL_UUID}}}})
            if rc != 0:
                fail("4: %s does not render with conf.ceph.enabled: %s" % (chart, err.strip()[:300]))
                continue
            vals = env_values([d for d in yaml.safe_load_all(out) if isinstance(d, dict)], var)
            if len(vals) < least:
                fail("4: %s renders %d %s, want at least %d: nothing was measured" % (chart, len(vals), var, least))
            for where, v in vals:
                if v != SENTINEL_UUID:
                    fail("4: %s %s is %r, not conf.ceph.cinder.secret_uuid (%s)" % (where, var, v, SENTINEL_UUID))
                count("4")
        defaults = {}
        for chart, path in (("cinder", "conf.backends.rbd1.rbd_secret_uuid"), ("nova", "conf.ceph.cinder.secret_uuid"),
                            ("nova", "conf.nova.libvirt.rbd_secret_uuid"), ("libvirt", "conf.ceph.cinder.secret_uuid")):
            v = dig(yaml.safe_load(open(os.path.join(BP, chart, "chart", "values.yaml"))), path)
            if not v:
                fail("4: %s values.yaml has no %s" % (chart, path))
            defaults["%s %s" % (chart, path)] = v
        if len(set(defaults.values())) != 1:
            fail("4: the libvirt secret UUID differs between charts, so a volume or disk would name a secret libvirt "
                 "never defined: %s" % defaults)
        count("4")
    for k in ("1", "2", "3", "4"):
        if not counts.get(k):
            fail("%s: measured nothing" % k)
    if failures:
        print("FAIL: %d problem(s)" % len(failures))
        for m in failures:
            print("  " + m)
        sys.exit(1)
    print("OK: storage.backend reaches %s alone for %d backends (%d specs), one place sets it (%d refusals), %s refuses "
          "without a backend (%d renders), and one libvirt secret UUID (%d checks)"
          % (", ".join(block), len(backends), counts["1"], counts["2"], ", ".join(block), counts["3"], counts["4"]))


if __name__ == "__main__":
    main()
