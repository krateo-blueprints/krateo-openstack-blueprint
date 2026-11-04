#!/usr/bin/env python3
"""Every config file a start script names must actually be delivered to the container.

Three defects found on a live cluster share one shape: the chart tells a process to read a
file, and nothing in the chart ever puts that file there.

  * cyborg's three start scripts passed `--log-config-append /tmp/logging-cyborg.conf`, but the
    chart had no conf.logging, no logging.conf key in its secret and no mount. oslo_log raises
    LogConfigError on a missing file, so cyborg-api, cyborg-conductor and cyborg-agent crashed
    at startup on every cluster. 520 restarts in 38 hours on the cluster where it was found.
  * designate's and magnum's uwsgi ini named `wsgi-file /var/lib/openstack/bin/<svc>-api-wsgi`,
    which the 2026.1 images do not ship. uwsgi answered every request with "no python
    application found" while its master kept the socket open, so the pod stayed Running.

This gate renders every chart and checks that each path named by `--config-file`,
`--config-dir`, `--log-config-append` or a uwsgi `wsgi-file` is delivered by a volumeMount in
the container that runs it, or is declared in hack/config-file-ref-waivers.yaml with a reason.

A missing waiver file, an unreadable chart or an empty scan are all failures: a check that can
report success without measuring anything is worse than no check.
"""
import os
import re
import subprocess
import sys
import tempfile
import shutil

try:
    import yaml
except ImportError:
    sys.exit("FAIL: PyYAML is required by validate-config-file-refs.py and is not installed")

# Optional repo root, so this is invoked the same way as the sibling gates
# (`python3 hack/validate-config-file-refs.py .`).
ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 \
    else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAIVERS = os.path.join(ROOT, "hack", "config-file-ref-waivers.yaml")

# `--flag /path` or `--flag=/path`, with the value optionally quoted.
FLAG_RE = re.compile(
    r"--(?:config-file|config-dir|log-config-append)[=\s]+[\"']?(/[^\s\"'\\]+)")
# uwsgi ini line: `wsgi-file = /path`
WSGI_RE = re.compile(r"^\s*wsgi-file\s*=\s*(\S+)\s*$", re.M)


def render(chart_dir):
    tmp = tempfile.mkdtemp()
    try:
        dst = os.path.join(tmp, "chart")
        shutil.copytree(chart_dir, dst)
        cy = os.path.join(dst, "Chart.yaml")
        # Read fully before opening for write: `open(w).write(open(r).read())` truncates the
        # file before the read runs, which rendered every chart nameless and made the gate
        # report that it had nothing to inspect.
        meta = open(cy).read()
        open(cy, "w").write(meta.replace("CHART_VERSION", "0.0.0-gate"))
        tk = os.path.join(ROOT, "charts", "helm-toolkit")
        if os.path.isdir(tk):
            os.makedirs(os.path.join(dst, "charts"), exist_ok=True)
            shutil.copytree(tk, os.path.join(dst, "charts", "helm-toolkit"))
        p = subprocess.run(["helm", "template", "gate", dst],
                           capture_output=True, text=True)
        if p.returncode:
            return None, p.stderr.strip().splitlines()[-1] if p.stderr else "helm template failed"
        return list(yaml.safe_load_all(p.stdout)), None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def payloads(docs):
    """name -> {key: text} for every ConfigMap and Secret in the render."""
    import base64
    out = {}
    for d in docs:
        if not isinstance(d, dict) or d.get("kind") not in ("ConfigMap", "Secret"):
            continue
        name = d.get("metadata", {}).get("name")
        body = {}
        for k, v in (d.get("data") or {}).items():
            if d["kind"] == "Secret":
                try:
                    v = base64.b64decode(v).decode()
                except Exception:
                    continue
            body[k] = v
        out[name] = body
    return out


def workloads(docs):
    for d in docs:
        if not isinstance(d, dict):
            continue
        kind = d.get("kind")
        spec = None
        if kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"):
            spec = d.get("spec", {}).get("template", {}).get("spec")
        elif kind == "Job":
            spec = d.get("spec", {}).get("template", {}).get("spec")
        elif kind == "CronJob":
            spec = (d.get("spec", {}).get("jobTemplate", {})
                     .get("spec", {}).get("template", {}).get("spec"))
        elif kind == "Pod":
            spec = d.get("spec")
        if not spec:
            continue
        name = d.get("metadata", {}).get("name", "?")
        for c in (spec.get("initContainers") or []) + (spec.get("containers") or []):
            yield kind, name, spec, c


def delivered(spec, container, cms):
    """The exact file paths this container can read, and the directories it mounts.

    A mountPath is not enough on its own. `/tmp` is an emptyDir in every chart here, so the
    first version of this gate declared cyborg's `--log-config-append /tmp/logging-cyborg.conf`
    "delivered" and printed OK for the very crash it was written to catch. An emptyDir, a
    hostPath or a PVC supplies a directory and no known contents; only a ConfigMap or Secret
    lets us name the files that will actually be there.

    Returns (files, opaque_dirs): files are paths we can prove exist, opaque_dirs are mounted
    directories whose contents are produced at runtime and cannot be checked statically.
    """
    vols = {v.get("name"): v for v in (spec.get("volumes") or [])}
    files, opaque = set(), set()
    for m in container.get("volumeMounts") or []:
        mp = (m.get("mountPath") or "").rstrip("/")
        if not mp:
            continue
        v = vols.get(m.get("name")) or {}
        src = v.get("configMap") or v.get("secret") or {}
        src_name = src.get("name") or src.get("secretName")
        keys = set(cms.get(src_name, {})) if src_name else set()
        items = src.get("items")
        if items:
            keys = {i.get("key") for i in items if i.get("key")}
        if not src_name:
            # emptyDir / hostPath / PVC / projected: a directory with unknown contents.
            opaque.add(mp)
            continue
        if m.get("subPath"):
            # One file, mounted at mountPath -- but only if the source actually has that key.
            if m["subPath"] in keys:
                files.add(mp)
            continue
        for k in keys:
            files.add(mp + "/" + k)
    return files, opaque


def covered(path, files, opaque):
    p = path.rstrip("/")
    if p in files:
        return True
    # A directory reference (--config-dir) is satisfied by mounting that directory, whatever
    # backs it: the service tolerates it being empty.
    if p in opaque:
        return True
    # A file inside a runtime-populated directory is fine if something in the pod writes it --
    # openstack-helm's established idiom is an init container dropping a snippet into a shared
    # emptyDir at /tmp/pod-shared. That is the discriminator against the cyborg bug, where no
    # script in the pod ever mentioned the file the service was told to read.
    return False


def written_in_pod(path, spec, cms):
    """True if any OTHER script in this pod names the path, i.e. something creates it."""
    for c in (spec.get("initContainers") or []) + (spec.get("containers") or []):
        for text in script_for(c, cms):
            for line in text.splitlines():
                if path in line and "--config-file" not in line \
                        and "--config-dir" not in line and "--log-config-append" not in line:
                    return True
    return False


def script_for(container, cms):
    """The text of the start script this container runs, if it comes from a ConfigMap."""
    cmd = container.get("command") or []
    texts = []
    for token in cmd:
        if not isinstance(token, str) or not token.startswith("/"):
            continue
        base = os.path.basename(token)
        for body in cms.values():
            if base in body:
                texts.append(body[base])
    return texts


def main():
    if not os.path.isfile(WAIVERS):
        sys.exit(f"FAIL: missing waiver declaration {WAIVERS} -- "
                 "the gate refuses to run without it")
    waivers = yaml.safe_load(open(WAIVERS)) or {}
    allowed = set()
    for chart, entries in (waivers.get("waivers") or {}).items():
        for e in entries:
            if not e.get("reason"):
                sys.exit(f"FAIL: waiver for {chart}:{e.get('path')} has no reason")
            allowed.add((chart, e["path"]))

    bp = os.path.join(ROOT, "blueprints")
    charts = sorted(d for d in os.listdir(bp)
                    if os.path.isdir(os.path.join(bp, d, "chart")))
    if not charts:
        sys.exit("FAIL: no charts found under blueprints/ -- nothing was measured")

    problems = []
    checked = 0
    for c in charts:
        docs, err = render(os.path.join(bp, c, "chart"))
        if docs is None:
            problems.append(f"{c}: render failed: {err}")
            continue
        cms = payloads(docs)
        for kind, wname, spec, container in workloads(docs):
            files, opaque = delivered(spec, container, cms)
            refs = set()
            for text in script_for(container, cms):
                refs.update(FLAG_RE.findall(text))
            # A uwsgi wsgi-file belongs to the container that mounts that ini, not to every
            # container in the chart: attributing designate's api entry point to
            # designate-central would be a finding against a pod that never reads it.
            for m in container.get("volumeMounts") or []:
                sub = m.get("subPath") or ""
                if not sub.endswith(".ini"):
                    continue
                for body in cms.values():
                    if sub in body:
                        refs.update(WSGI_RE.findall(body[sub]))
            for r in sorted(refs):
                checked += 1
                if covered(r, files, opaque):
                    continue
                if any(r.startswith(d + "/") for d in opaque) \
                        and written_in_pod(r, spec, cms):
                    continue
                if (c, r) in allowed:
                    continue
                problems.append(
                    f"{c}: {kind}/{wname} container {container.get('name')} reads {r}, "
                    "which the chart does not deliver")

    if checked == 0:
        print("FAIL: zero config-file references inspected -- the scan found nothing to "
              "check, which means it is not working")
        for q in sorted(set(problems)):
            print("  " + q)
        return 1

    if problems:
        print(f"FAIL: {len(problems)} undelivered config references "
              f"({checked} references inspected across {len(charts)} charts)")
        for p in sorted(set(problems)):
            print("  " + p)
        return 1
    print(f"OK: {checked} config-file references across {len(charts)} charts are all "
          "delivered by a mount or waived")
    return 0


if __name__ == "__main__":
    sys.exit(main())
