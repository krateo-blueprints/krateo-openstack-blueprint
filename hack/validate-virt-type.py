#!/usr/bin/env python3
"""compute.virtType kvm runs guests on KVM, refuses a node without /dev/kvm, and qemu changes nothing (issue #303).

    hack/validate-virt-type.py [repo-root]

Measured 2026-11-17 on GKE: an N2 Ubuntu node pool created with --enable-nested-virtualization shows vmx, /dev/kvm and a
working KVM_CREATE_VM; a pool without the flag shows none of these. The umbrella's compute.virtType (qemu | kvm, default
qemu) chooses between software emulation and KVM.

  1. THE UMBRELLA. The lines of compositions.yaml that build a component's spec are rendered for every component with
     compute absent, compute.virtType qemu and compute.virtType kvm. qemu: every spec is exactly the spec without compute.
     kvm: nova's spec gains conf.nova.libvirt {virt_type: kvm, cpu_mode: host-model}, libvirt's conf.libvirt.virt_type
     kvm, and every other spec is unchanged. An unknown virtType is refused by the schema.
  2. THE CHARTS, WITH KVM. nova, rendered with the spec the umbrella builds, sets virt_type = kvm and cpu_mode =
     host-model in every [libvirt] it renders (nova.conf, nova-compute.conf, the per-node overrides); libvirt renders
     virt_type "kvm" and, in its DaemonSet, an init container that runs kvm-check.sh from an executable ConfigMap mount
     (mode 0555) with the host /dev mounted.
  3. THE REFUSAL RUNS. kvm-check.sh, as rendered, is RUN: against a path that is not a character device it exits non-zero
     and says what to enable on any node (the device, hardware virtualization, nested virtualization on cloud VMs, the
     qemu fallback) without naming a cloud provider (the blueprint is provider-agnostic; examples live in the README);
     against a character device it exits 0.
  4. WITH QEMU. nova and libvirt rendered with the qemu spec set virt_type = qemu and cpu_mode = none, and render no
     kvm-check.sh and no such init container.

Fails when helm or PyYAML is missing, and when any part finds nothing to check.
"""
import base64
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
BP = os.path.join(ROOT, "blueprints")
UMBRELLA = os.path.join(BP, "openstack", "chart")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)
failures = []
counts = {}


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


def text_of(d, k):
    v = (d.get("data") or {}).get(k)
    if v is None:
        return ""
    return base64.b64decode(v).decode("utf-8", "replace") if d.get("kind") == "Secret" else str(v)


def ini_libvirt(text):
    """[(virt_type, cpu_mode)] of every [libvirt] section of an ini text."""
    out, section, cur = [], None, None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if cur is not None:
                out.append(cur)
            section = s[1:-1]
            cur = {} if section == "libvirt" else None
        elif cur is not None and "=" in s and not s.startswith(("#", ";")):
            k, _, v = s.partition("=")
            cur[k.strip()] = v.strip()
    if cur is not None:
        out.append(cur)
    return [(c.get("virt_type"), c.get("cpu_mode")) for c in out]


def umbrella_specs(u, vals):
    rc, out, err = helm_template(u, "openstack-gate", vals)
    if rc != 0:
        return None, err
    return {l.split(" ", 3)[2]: json.loads(l.split(" ", 3)[3]) for l in out.splitlines() if l.startswith("# VTSPEC ")}, ""


def check_nova(docs, want, label):
    seen = 0
    for d in docs:
        if d.get("kind") != "Secret":
            continue
        for k in (d.get("data") or {}):
            if not k.endswith(".conf"):
                continue
            for vt, cm in ini_libvirt(text_of(d, k)):
                seen += 1
                if (vt, cm) != want:
                    fail("2: nova%s: %s %s [libvirt] has virt_type=%s cpu_mode=%s, want %s" % (label, d["metadata"]["name"],
                                                                                            k, vt, cm, want))
    if not seen:
        fail("2: nova%s renders no [libvirt] section; the check had no input" % label)
    counts["nova" + label] = seen


def check_libvirt(docs, kvm, tmp, label):
    vts = set()
    script = None
    for d in docs:
        if d.get("kind") == "ConfigMap":
            for k, v in (d.get("data") or {}).items():
                vts |= set(re.findall(r'virt_type\s*=\s*"?(\w+)"?', str(v)))
                if k == "kvm-check.sh":
                    script = str(v)
    want = "kvm" if kvm else "qemu"
    if vts != {want}:
        fail("2: libvirt%s renders virt_type %s, want only %s" % (label, sorted(vts), want))
    inits = []
    for d in docs:
        if d.get("kind") != "DaemonSet":
            continue
        pod = d["spec"]["template"]["spec"]
        vols = {v["name"]: v for v in pod.get("volumes") or []}
        for c in pod.get("initContainers") or []:
            if (c.get("command") or [None])[0] != "/tmp/kvm-check.sh":
                continue
            inits.append(c["name"])
            mounts = {m["mountPath"]: m for m in c.get("volumeMounts") or []}
            m = mounts.get("/tmp/kvm-check.sh")
            v = vols.get((m or {}).get("name")) or {}
            mode = (v.get("configMap") or {}).get("defaultMode")
            if not m or m.get("subPath") != "kvm-check.sh" or mode is None or not mode & 0o111:
                fail("2: libvirt%s: %s runs kvm-check.sh, but not from an executable ConfigMap mount (mode %s)"
                     % (label, c["name"], oct(mode) if mode else mode))
            dev = mounts.get("/dev")
            if not dev or ((vols.get(dev["name"]) or {}).get("hostPath") or {}).get("path") != "/dev":
                fail("2: libvirt%s: %s does not see the host /dev, so it cannot find /dev/kvm" % (label, c["name"]))
    if kvm:
        if not inits:
            fail("2: libvirt%s: no init container runs kvm-check.sh: a node without /dev/kvm would start libvirt, and every "
                 "guest would fail later, far from the cause" % label)
        if not script:
            fail("2: libvirt%s renders no kvm-check.sh" % label)
        else:
            run_check(script, tmp)
    else:
        if inits or script:
            fail("4: libvirt%s renders the kvm refusal with virt_type qemu (%s)" % (label, inits or "kvm-check.sh"))


def run_check(script, tmp):
    p = os.path.join(tmp, "kvm-check.sh")
    open(p, "w").write(script)
    os.chmod(p, 0o755)
    missing = os.path.join(tmp, "no-such-kvm")
    r = subprocess.run(["bash", p], env={"PATH": os.environ.get("PATH", ""), "KVM_DEVICE": missing, "NODE_NAME": "gate-node"},
                       capture_output=True, text=True)
    if r.returncode == 0:
        fail("3: kvm-check.sh exits 0 on a node without the device; it must refuse")
    else:
        msg = " ".join(r.stderr.split())
        missing_terms = [t for t in (missing, "hardware virtualization", "nested virtualization", "compute.virtType: qemu")
                         if t not in msg]
        if missing_terms:
            fail("3: kvm-check.sh refuses without saying %s: %s" % (missing_terms, msg[:300]))
        provider = re.findall(r"\b(GKE|GCP|gcloud|Google|AWS|EC2|Amazon|Azure|Microsoft|OpenStack-on-\w+)\b|--enable-nested",
                              msg)
        if provider:
            fail("3: kvm-check.sh's refusal names a cloud provider (%s): the blueprint is provider-agnostic, and examples "
                 "belong in the README" % sorted(set(p for p in provider if p) or {"--enable-nested"}))
    r = subprocess.run(["bash", p], env={"PATH": os.environ.get("PATH", ""), "KVM_DEVICE": "/dev/null"},
                       capture_output=True, text=True)
    if r.returncode != 0:
        fail("3: kvm-check.sh refuses a character device (/dev/null stands in for /dev/kvm): %s" % r.stderr.strip()[:200])
    counts["3"] = counts.get("3", 0) + 2


def main():
    uv = yaml.safe_load(open(os.path.join(UMBRELLA, "values.yaml")))
    names = [c["name"] for c in uv.get("components") or []]
    if not names:
        print("FAILED MEASUREMENT: the umbrella lists no component")
        sys.exit(2)
    with tempfile.TemporaryDirectory() as tmp:
        u = stage(UMBRELLA, tmp, "umbrella")
        comp = open(os.path.join(UMBRELLA, "templates", "compositions.yaml")).read()
        start = comp.find("{{- $ov := ")
        mark = comp.find("$.Values.compute", start)
        mark = comp.find("$.Values.kogCredentials", mark) if mark >= 0 else -1
        end = comp.find("{{- end }}", mark) if mark >= 0 else -1
        if start < 0 or end < 0:
            fail("1: compositions.yaml builds no spec from $.Values.compute (the span from `{{- $ov := ` through the "
                 "compute and kogCredentials merges), so compute.virtType reaches no component")
            specs = {}
        else:
            open(os.path.join(u, "templates", "zz-gate-vt-probe.yaml"), "w").write(
                '{{- $sel := splitList " " (include "osh.selected" (list $)) }}\n'
                '{{- range $c := .Values.components }}\n' + comp[start:end + len("{{- end }}")] + '\n'
                '# VTSPEC {{ $c.name }} {{ $ov | toJson }}\n{{- end }}\n')
            specs = {}
            for key, vals in (("absent", {"compute": None}), ("qemu", {"compute": {"virtType": "qemu"}}),
                              ("kvm", {"compute": {"virtType": "kvm"}})):
                s, err = umbrella_specs(u, vals)
                if s is None:
                    fail("1: the umbrella probe does not render with compute %s: %s" % (key, err.strip()[:200]))
                else:
                    specs[key] = s
            rc, _, _ = helm_template(u, "openstack-gate", {"compute": {"virtType": "xen"}})
            if rc == 0:
                fail("1: compute.virtType xen renders; the schema must allow only qemu and kvm")
        if len(specs) == 3:
            for n in names:
                if specs["qemu"].get(n) != specs["absent"].get(n):
                    fail("1: with compute.virtType qemu %s's spec differs from the spec without compute: qemu must change "
                         "nothing" % n)
                want = json.loads(json.dumps(specs["absent"].get(n) or {}))
                if n == "nova":
                    want.setdefault("conf", {}).setdefault("nova", {}).setdefault("libvirt", {}).update(
                        {"virt_type": "kvm", "cpu_mode": "host-model"})
                elif n == "libvirt":
                    want.setdefault("conf", {}).setdefault("libvirt", {})["virt_type"] = "kvm"
                if (specs["kvm"].get(n) or {}) != want:
                    fail("1: with compute.virtType kvm %s's spec is %s, want %s" % (n, json.dumps(specs["kvm"].get(n))[:200],
                                                                                  json.dumps(want)[:200]))
            counts["1"] = len(names)
            for mode in ("kvm", "qemu"):
                label = " (compute.virtType %s)" % mode
                for chart in ("nova", "libvirt"):
                    w = stage(os.path.join(BP, chart, "chart"), tmp, "%s-%s" % (chart, mode))
                    rc, out, err = helm_template(w, chart + "-gate1234", specs[mode].get(chart) or {})
                    if rc != 0:
                        fail("2: %s%s does not render with the spec the umbrella builds: %s" % (chart, label, err.strip()[:200]))
                        continue
                    docs = [d for d in yaml.safe_load_all(out) if d]
                    if chart == "nova":
                        check_nova(docs, ("kvm", "host-model") if mode == "kvm" else ("qemu", "none"), label)
                    else:
                        check_libvirt(docs, mode == "kvm", tmp, label)
    if not counts.get("3"):
        fail("3: kvm-check.sh was never run; the check had no input")
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: compute.virtType: %d component spec(s) compared (qemu changes nothing, kvm moves nova and libvirt only); "
          "nova's [libvirt] sections checked (%s); kvm-check.sh run %d time(s), refusing a node without /dev/kvm"
          % (counts.get("1", 0), ", ".join("%s: %d" % (k, v) for k, v in sorted(counts.items()) if k.startswith("nova")),
             counts.get("3", 0)))


main()
