#!/usr/bin/env python3
"""Every Job that calls an OpenStack API waits for it (issue #299).

    hack/validate-job-service-dependencies.py [repo-root] [chart ...]

Measured on GKE 2026-11-17 (fresh 0.3.71, full-strict): manila's bootstrap Job declared no dependency at all. Its script
calls glance and nova, so on a fresh install it failed 7 times, until nova-api was up. An upgrade never shows this,
because every API already exists then.

Every chart (or the ones named) is rendered at its defaults and with every workload manifest on. For every Job and
CronJob container whose scripts run the openstack client (an inline `bash -c`, a mounted script its command names, or
helm-toolkit's openstack_count helper), each CLI noun is mapped to the OpenStack service it calls. Its pod's
kubernetes-entrypoint init must wait for that service: the Service the chart's endpoints.<type> names, rendered the
way helm-toolkit renders DEPENDENCY_SERVICE (service_name_endpoint_with_namespace_lookup). The map is explicit: a noun it
does not know fails, so a new command can never pass by not being recognised. A service the chart defines no endpoint
for fails too, because the Job could not declare it.

Fails when helm or PyYAML is missing, when a chart does not render, and when no Job calls the openstack client.
"""
import base64
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
ONLY = sys.argv[2:]
BP = os.path.join(ROOT, "blueprints")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)

# CLI noun (longest phrase first) -> the endpoints.<type> keys that name its service, in order of preference.
NOUNS = {
    "token": ("identity",), "user": ("identity",), "role assignment": ("identity",), "role": ("identity",),
    "project": ("identity",), "domain": ("identity",), "service": ("identity",), "endpoint": ("identity",),
    "catalog": ("identity",), "trust": ("identity",), "region": ("identity",), "group": ("identity",),
    "credential": ("identity",), "application credential": ("identity",), "ec2 credentials": ("identity",),
    "image": ("image",),
    "flavor": ("compute",), "server": ("compute",), "compute service": ("compute",), "keypair": ("compute",),
    "hypervisor": ("compute",), "aggregate": ("compute",),
    "network": ("network",), "subnet": ("network",), "router": ("network",), "port": ("network",),
    "security group rule": ("network",), "security group": ("network",), "floating ip": ("network",),
    "network agent": ("network",), "subnet pool": ("network",),
    "volume type": ("volumev3", "volumev2", "volume"), "volume qos": ("volumev3", "volumev2", "volume"),
    "volume backup": ("volumev3", "volumev2", "volume"), "volume snapshot": ("volumev3", "volumev2", "volume"),
    "volume": ("volumev3", "volumev2", "volume"),
    "share group type": ("sharev2",), "share type": ("sharev2",), "share network": ("sharev2",), "share": ("sharev2",),
    "object": ("object_store",), "container": ("object_store",),
    "secret": ("key_manager",),
    "queue": ("messaging",), "message": ("messaging",),
    "stack": ("orchestration",),
    "zone": ("dns",), "recordset": ("dns",),
    "loadbalancer": ("load_balancer",),
}
VERBS = {"list", "show", "create", "set", "unset", "delete", "add", "remove", "issue", "get", "save", "associate",
         "disassociate", "post", "store", "revoke", "purge"}
OPENSTACK = re.compile(r"(?:^|[\s;&|(`$])openstack(?:_count\s+(?:\"[^\"]*\"|'[^']*'|\S+))?\s+(.*)$")
failures = []
CHECKED = []
FLAGGED = set()


# What the umbrella delivers for a chart's storage (#308): cinder's default refuses, so a chart render carries
# hack/gate_values.py's arguments (nothing for every other chart).
import importlib.util as _gv_util
import os as _gv_os
_gv_spec = _gv_util.spec_from_file_location(
    "gate_values", _gv_os.path.join(_gv_os.path.dirname(_gv_os.path.abspath(__file__)), "gate_values.py"))
gate_values = _gv_util.module_from_spec(_gv_spec)
_gv_spec.loader.exec_module(gate_values)


def fail(chart, m):
    failures.append(m)
    FLAGGED.add(chart)


def stage(chart_dir, tmp, name):
    w = os.path.join(tmp, name)
    shutil.copytree(chart_dir, w)
    cy = os.path.join(w, "Chart.yaml")
    txt = open(cy).read()
    open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
    if "helm-toolkit" in txt:
        os.makedirs(os.path.join(w, "charts"), exist_ok=True)
        shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
        # how helm-toolkit names each endpoint type's Service in DEPENDENCY_SERVICE
        open(os.path.join(w, "templates", "zz-gate-svc-probe.yaml"), "w").write(
            '{{- range $k, $v := .Values.endpoints }}{{- if and (kindIs "map" $v) (hasKey $v "hosts") }}\n'
            '# SVCHOST {{ $k }} {{ tuple $k "internal" $ | include "helm-toolkit.endpoints.service_name_endpoint_with_namespace_lookup" }}\n'
            '{{- end }}{{- end }}\n')
    return w


def helm_template(chart, release, values):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values or {}, f)
    try:
        r = subprocess.run(["helm", "template", release, chart, "--namespace", "openstack", "--kube-version", "1.36.0",
                            *gate_values.helm_args(chart), "-f", f.name], capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    return r.returncode, r.stdout, r.stderr


def text_of(d, key):
    v = (d.get("data") or {}).get(key)
    if v is None:
        return None
    if d.get("kind") == "Secret":
        try:
            return base64.b64decode(v).decode("utf-8", "replace")
        except Exception:
            return None
    return str(v)


def scripts(pod, cont, docs):
    vols = {v["name"]: v for v in pod.get("volumes") or []}
    cmd = (cont.get("command") or []) + (cont.get("args") or [])
    out = []
    if len(cmd) >= 3 and cmd[0].endswith("sh") and cmd[1] == "-c":
        out.append(cmd[2])
    words = set(re.findall(r"/[\w./-]+", " ".join(cmd)))
    for m in cont.get("volumeMounts") or []:
        if m["mountPath"] in words and m.get("subPath"):
            v = vols.get(m["name"]) or {}
            kind, name = (("ConfigMap", (v.get("configMap") or {}).get("name")) if "configMap" in v else
                          ("Secret", (v.get("secret") or {}).get("secretName")) if "secret" in v else (None, None))
            for d in docs:
                if d.get("kind") == kind and d["metadata"]["name"] == name:
                    t = text_of(d, m["subPath"])
                    if t:
                        out.append(t)
    return out


def nouns_of(text):
    """[(noun, line)] for every openstack client invocation in a shell script."""
    found = []
    logical = re.sub(r"\\\n\s*", " ", text)
    for line in logical.splitlines():
        code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
        if re.match(r"^\s*openstack_count\s*\(\)", code):
            continue  # the helper's definition; its callers are parsed
        for m in OPENSTACK.finditer(code):
            rest = m.group(1).strip()
            if rest.startswith(('"$@"', "$@")) or rest.startswith("()"):
                continue  # helm-toolkit's openstack_count body, and its definition: their callers are parsed
            toks = rest.split()
            noun = None
            for i, tok in enumerate(toks[:6]):
                if tok in VERBS:
                    noun = " ".join(toks[:i])
                    break
            found.append((noun, line.strip()[:120]))
    return found


def check(chart, label, docs, svchost):
    for d in docs:
        kind = d.get("kind")
        if kind not in ("Job", "CronJob"):
            continue
        spec = d["spec"]["jobTemplate"]["spec"]["template"]["spec"] if kind == "CronJob" else d["spec"]["template"]["spec"]
        waits = set()
        for ic in spec.get("initContainers") or []:
            for ev in ic.get("env") or []:
                if ev["name"] == "DEPENDENCY_SERVICE":
                    waits |= {x.strip() for x in (ev.get("value") or "").split(",") if x.strip()}
        for cont in spec.get("containers") or []:
            needed = {}
            for t in scripts(spec, cont, docs):
                for noun, line in nouns_of(t):
                    if noun not in NOUNS:
                        fail(chart, "%s%s: %s/%s container %s runs `%s`: the CLI noun %r is not in this gate's map, so "
                                    "which service it calls is unknown; add it" % (chart, label, kind, d["metadata"]["name"],
                                                                                  cont["name"], line, noun))
                        continue
                    needed.setdefault(noun, line)
            for noun, line in sorted(needed.items()):
                keys = [k for k in NOUNS[noun] if k in svchost]
                where = "%s%s: %s/%s container %s" % (chart, label, kind, d["metadata"]["name"], cont["name"])
                CHECKED.append((chart, d["metadata"]["name"], noun))
                if not keys:
                    fail(chart, "%s runs `%s`, which calls the %s service, but the chart defines no endpoints.%s, so the "
                                "Job cannot wait for it" % (where, line, NOUNS[noun][0], "|".join(NOUNS[noun])))
                    continue
                if not any(svchost[k] in waits or svchost[k].split(":", 1)[-1] in {w.split(":", 1)[-1] for w in waits}
                           for k in keys):
                    fail(chart, "%s runs `%s`, which calls %s (Service %s), but its init waits only for %s: on a fresh "
                                "install it fails until that API is up (#299)" % (where, line, keys[0], svchost[keys[0]],
                                                                                   sorted(waits) or "nothing"))


def main():
    charts = sorted(d for d in os.listdir(BP) if d != "openstack" and os.path.isfile(os.path.join(BP, d, "chart",
                                                                                                    "values.yaml")))
    if ONLY:
        charts = [c for c in charts if c in ONLY]
    if not charts:
        print("FAILED MEASUREMENT: no chart to check")
        sys.exit(2)
    with tempfile.TemporaryDirectory() as tmp:
        for c in charts:
            v = yaml.safe_load(open(os.path.join(BP, c, "chart", "values.yaml"))) or {}
            man = v.get("manifests") or {}
            on = {k: True for k, x in man.items() if x is False and (k.startswith(("daemonset_", "deployment_",
                                                                                   "statefulset_", "cron_", "job_"))
                                                                    or k.endswith("_monitor"))
                  and k != "job_image_repo_sync"}
            w = stage(os.path.join(BP, c, "chart"), tmp, c)
            for label, vals in (("", {}), (" (every workload on)", {"manifests": on})):
                rc, out, err = helm_template(w, c + "-gate1234", vals)
                if rc != 0:
                    fail(c, "%s%s: does not render: %s" % (c, label, err.strip()[:200]))
                    continue
                svchost = dict(re.findall(r"^# SVCHOST (\S+) (\S+)$", out, re.M))
                check(c, label, [d for d in yaml.safe_load_all(out) if d], svchost)
    if not CHECKED:
        failures.append("no Job calls the openstack client in the charts checked; the check had no input")
    if failures:
        for f in sorted(set(failures)):
            print("FAIL: " + f)
        print("charts flagged: %s" % ", ".join(sorted(FLAGGED)))
        sys.exit(1)
    print("OK: %d (Job, CLI noun) pair(s) in %d chart(s): every Job waits for the service its script calls"
          % (len(set(CHECKED)), len({c for c, _, _ in CHECKED})))


main()
