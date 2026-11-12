#!/usr/bin/env python3
"""Every keystoneauth credential section a service reads at runtime must name its auth plugin
and its keystone endpoint (#203).

keystoneauth builds a client from an oslo section only when it can load a plugin: `auth_type`
names the plugin and the identity plugins need `auth_url`. watcher's `[watcher_clients_auth]`
rendered with nothing but `insecure`, so `load_auth_from_conf_options` returned no plugin and
watcher-decision-engine exited 1 five seconds after every start:

    keystoneauth1.exceptions.discovery.DiscoveryFailure: Not enough information to determine URL
    watcher.common.exception.AuthorizationFailure: keystone connection failed

Measured on GKE, Kubernetes 1.36.4, core-provider 2.13.72, blueprint 0.3.45 (#213).

A section is judged as the service sees it, not file by file: OpenStack-Helm splits a service's
config across a main file and credential drop-ins in another Secret (`<svc>-ks-etc`), mounted
into the same directory and merged by oslo.config. So for every pod template the gate collects
every `*.conf` the pod mounts (whole Secret/ConfigMap mounts and subPath mounts alike), merges
them in one parser, and then checks each section that looks like a keystoneauth section:

  * the section name ends in `_clients_auth`, or
  * it sets `auth_type` / `auth_plugin`, or
  * it carries `username` + `password` plus a project or domain option.

Such a section must have `auth_type` (or `auth_plugin`) AND `auth_url`. Sections that look like
that but are NOT keystoneauth groups, or are only read when an off-by-default integration is
enabled, are listed in ALLOWED below, each with the reason. Add to it only with a reason.

Fails when no chart is found, when a chart does not render, or when no auth section is found.

Usage: hack/validate-oslo-auth-sections.py [repo-root] [component ...]
"""
import argparse
import base64
import configparser
import importlib.util
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("job_hooks", HERE / "validate-job-hooks.py")
job_hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job_hooks)
yaml = job_hooks.yaml

IDENTITY_KEYS = {"project_name", "project_id", "user_domain_name", "user_domain_id",
                 "project_domain_name", "project_domain_id"}

# (component, section) -> why it is not a missing-credentials defect.
ALLOWED = {
    ("tacker", "alarm_auth"):
        "tacker's own option group for alarm actions (url/username/password/project_name), "
        "read by tacker itself, not loaded through keystoneauth",
    ("neutron", "designate"):
        "read only when neutron's external DNS driver is set to designate, which is off by default",
    ("neutron", "ironic"):
        "read only by neutron's ironic integration (baremetal port binding), off by default",
    ("zaqar", "keystone_authtoken"):
        "carries the legacy keystonemiddleware `identity_uri`; zaqar's API is Ready on every "
        "cluster run so far. Whether it should move to auth_url is open, not hidden: see the PR",
}


def mounted_files(pod_spec, configs):
    """{path: text} for every Secret/ConfigMap file a pod template mounts."""
    vols = {}
    for v in pod_spec.get("volumes") or []:
        src = v.get("secret") or v.get("configMap")
        if not src:
            continue
        name = src.get("secretName") or src.get("name")
        vols[v["name"]] = (name, src.get("items"))
    files = {}
    containers = (pod_spec.get("containers") or []) + (pod_spec.get("initContainers") or [])
    for c in containers:
        for m in c.get("volumeMounts") or []:
            if m["name"] not in vols:
                continue
            name, items = vols[m["name"]]
            data = configs.get(name, {})
            if m.get("subPath"):
                key = m["subPath"]
                if items:
                    key = next((i["key"] for i in items if i.get("path") == key), key)
                if key in data:
                    files[m["mountPath"]] = data[key]
            else:
                for key, text in data.items():
                    files[m["mountPath"].rstrip("/") + "/" + key] = text
    return files


def is_auth_section(section, opts):
    return (section.endswith("_clients_auth") or bool(opts & {"auth_type", "auth_plugin"})
            or ({"username", "password"} <= opts and bool(opts & IDENTITY_KEYS)))


def check(component, docs):
    configs = {}
    for d in docs:
        if d.get("kind") not in ("ConfigMap", "Secret"):
            continue
        data = {}
        for k, v in (d.get("data") or {}).items():
            if d["kind"] == "Secret":
                try:
                    v = base64.b64decode(v).decode("utf-8", "replace")
                except Exception:
                    continue
            data[k] = str(v)
        configs[d["metadata"]["name"]] = data
    problems, sections = [], 0
    for d in docs:
        tpl = None
        if d.get("kind") in job_hooks.WORKLOADS:
            tpl = (d.get("spec") or {}).get("template")
        elif d.get("kind") == "CronJob":
            tpl = d["spec"]["jobTemplate"]["spec"]["template"]
        if not tpl:
            continue
        files = mounted_files(tpl.get("spec") or {}, configs)
        cp = configparser.RawConfigParser(strict=False, default_section="__no_default__")
        for path in sorted(files):
            if path.endswith(".conf") and not path.endswith("logging.conf"):
                try:
                    cp.read_string(files[path], source=path)
                except configparser.Error:
                    pass
        for sec in cp.sections():
            opts = set(cp.options(sec))
            if not is_auth_section(sec, opts):
                continue
            sections += 1
            if (component, sec) in ALLOWED:
                continue
            missing = [o for o, alts in (("auth_type", {"auth_type", "auth_plugin"}),
                                         ("auth_url", {"auth_url"})) if not opts & alts]
            if missing:
                problems.append(f"{component}: {d['kind']}/{d['metadata']['name']} reads "
                                f"[{sec}] without {' or '.join(missing)}: keystoneauth loads no "
                                "plugin from it, so that client cannot reach keystone (#203)")
    return sorted(set(problems)), sections


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("components", nargs="*")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    charts = sorted(p.parent for p in (root / "blueprints").glob("*/chart/Chart.yaml"))
    charts = [c for c in charts if c.parent.name != "openstack"]
    if a.components:
        charts = [c for c in charts if c.parent.name in a.components]
    if not charts:
        print("FAIL: no component charts found -- nothing was measured")
        return 1
    failures, unrendered, total = [], [], 0
    with tempfile.TemporaryDirectory() as td:
        toolkit = root / "charts" / "helm-toolkit"
        for chart in charts:
            component = chart.parent.name
            try:
                docs = job_hooks.render(chart, toolkit, component, Path(td) / "r", [])
            except Exception as e:
                unrendered.append(f"{component}: {str(e)[:300]}")
                continue
            found, n = check(component, docs)
            total += n
            failures += found
            print(f"{'FAIL' if found else 'OK  '} {component}: {n} auth section(s) read by pods")
    for u in unrendered:
        print(f"FAIL {u}: did not render, so nothing was measured")
    if total == 0:
        print("FAIL: no keystoneauth section found in any chart -- nothing was measured")
        return 1
    for f in failures:
        print(f"FAIL {f}")
    if failures or unrendered:
        print(f"FAIL: {len(failures)} problem(s), {len(unrendered)} unrendered, {total} sections")
        return 1
    print(f"OK: {len(charts)} charts, {total} keystoneauth sections, each names its plugin and "
          "endpoint (or is listed with its reason)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
