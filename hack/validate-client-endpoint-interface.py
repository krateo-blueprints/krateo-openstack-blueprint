#!/usr/bin/env python3
"""A component's OpenStack clients call the internal catalog endpoint, never the public one (#222).

watcher-decision-engine looks up Keystone's service catalog at start-up (keystone services.list,
via `[keystone_client] interface`). Upstream defaults that and every other watcher client
(`<svc>_client.endpoint_type`) to the public endpoint. In this blueprint a public endpoint is
`<svc>.openstack.svc.cluster.local:80`, a Service selecting the ingress (`app=ingress-api`), which
has no pods unless an ingress controller is installed. So the lookup failed:

    keystoneauth1.exceptions.connection.ConnectFailure: Unable to establish connection to
    http://keystone.openstack.svc.cluster.local/v3/services?type=block-storage: ... ECONNREFUSED

and the decision engine exited 1 on every start. Measured on a 0.3.47 cold install (GKE,
Kubernetes 1.36.4, core-provider 2.13.82), the one component of 32 not Ready. It was hidden behind
#203 until 0.3.47 gave the clients credentials and stopped the logging config discarding errors.

This gate checks two things:
  1. For every component in hack/client-endpoint-interfaces.yaml, every pod template it names
     mounts a config whose merged value for each listed option is the internal value. The config
     is merged as the pod sees it (every *.conf it mounts, as hack/validate-oslo-auth-sections.py
     merges them). A listed pod that is not rendered, or that mounts no config, fails: it measured
     nothing.
  2. No chart renders an explicit public value (`public`, `publicURL`) for an `interface`,
     `endpoint_type`, `valid_interfaces` or `os_endpoint_type` option in any pod's config, unless it
     is listed in ALLOWED with the reason; an ALLOWED entry nothing renders fails. Defaults that are public are invisible to this scan,
     which is why (1) lists them per component, measured from the upstream source.

Fails when the data file is missing or empty, when a chart does not render, or when nothing is
measured.

Usage: hack/validate-client-endpoint-interface.py [repo-root]
"""
import argparse
import configparser
import importlib.util
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


job_hooks = _load("job_hooks", "validate-job-hooks.py")
oslo_auth = _load("oslo_auth", "validate-oslo-auth-sections.py")
yaml = job_hooks.yaml

DATA = "hack/client-endpoint-interfaces.yaml"
ENDPOINT_OPTIONS = {"interface", "endpoint_type", "valid_interfaces", "os_endpoint_type"}
PUBLIC = {"public", "publicurl"}

# (component, section, option) -> why an explicit public value there is not this defect.
ALLOWED = {
    ("heat", "clients_heat", "endpoint_type"):
        "set to publicURL deliberately (as upstream OpenStack-Helm does): heat's own heat client "
        "(heat/engine/clients/os/heat_plugin.py) builds URLs handed to instances and is used by "
        "OS::Heat::Stack, so it names the endpoint instances and users reach. heat starts and is "
        "Ready without it. That OS::Heat::Stack calls made by the engine itself then need the "
        "public endpoint to be reachable is a real limitation without an ingress controller; it "
        "is recorded in the PR that added this gate, not hidden here",
}


def configs_of(docs):
    import base64
    out = {}
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
        out[d["metadata"]["name"]] = data
    return out


def pod_configs(docs):
    """[(workload kind/name, merged RawConfigParser, number of .conf files)] per pod template."""
    configs = configs_of(docs)
    out = []
    for d in docs:
        tpl = None
        if d.get("kind") in job_hooks.WORKLOADS:
            tpl = (d.get("spec") or {}).get("template")
        elif d.get("kind") == "CronJob":
            tpl = d["spec"]["jobTemplate"]["spec"]["template"]
        if not tpl:
            continue
        files = oslo_auth.mounted_files(tpl.get("spec") or {}, configs)
        cp = configparser.RawConfigParser(strict=False, default_section="__no_default__")
        n = 0
        for path in sorted(files):
            if path.endswith(".conf") and not path.endswith("logging.conf"):
                try:
                    cp.read_string(files[path], source=path)
                    n += 1
                except configparser.Error:
                    pass
        out.append((d["kind"], d["metadata"]["name"], cp, n))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", nargs="?", default=".")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    data_file = root / DATA
    if not data_file.is_file():
        print(f"FAIL: no {DATA} -- nothing was measured")
        return 1
    data = yaml.safe_load(data_file.read_text()) or {}
    if not data:
        print(f"FAIL: {DATA} lists no component -- nothing was measured")
        return 1
    charts = sorted(p.parent for p in (root / "blueprints").glob("*/chart/Chart.yaml"))
    charts = [c for c in charts if c.parent.name != "openstack"]
    if not charts:
        print("FAIL: no component charts found -- nothing was measured")
        return 1
    failures, unrendered, checked_opts, scanned_pods = [], [], 0, 0
    allowed_seen = set()
    toolkit = root / "charts" / "helm-toolkit"
    with tempfile.TemporaryDirectory() as td:
        for chart in charts:
            component = chart.parent.name
            try:
                docs = job_hooks.render(chart, toolkit, component, Path(td) / "r", [])
            except Exception as e:
                unrendered.append(f"{component}: {str(e)[:300]}")
                continue
            pods = pod_configs(docs)
            scanned_pods += len(pods)
            # (2) explicit public values anywhere.
            for kind, name, cp, _ in pods:
                for sec in cp.sections():
                    for opt in set(cp.options(sec)) & ENDPOINT_OPTIONS:
                        val = (cp.get(sec, opt) or "").strip().strip('"').strip("'")
                        vals = {v.strip().lower() for v in val.split(",")}
                        if vals & PUBLIC and (component, sec, opt) in ALLOWED:
                            allowed_seen.add((component, sec, opt))
                        elif vals & PUBLIC:
                            failures.append(f"{component}: {kind}/{name} reads [{sec}] {opt} = {val}: "
                                            "a public endpoint, which has no backend without an "
                                            "ingress controller")
            # (1) listed defaults, at the pods that read them.
            entry = data.get(component)
            if not entry:
                continue
            opts = entry.get("options") or []
            want_pods = entry.get("pods") or []
            if not opts or not want_pods:
                failures.append(f"{component}: {DATA} entry has no options or no pods -- nothing to check")
                continue
            bad = []
            for pod in want_pods:
                match = [(k, n, cp, c) for k, n, cp, c in pods if n == pod]
                if not match:
                    bad.append(f"{component}: no rendered workload named {pod}, so it was not measured")
                    continue
                for kind, name, cp, nconf in match:
                    if nconf == 0:
                        bad.append(f"{component}: {kind}/{name} mounts no .conf, so it was not measured")
                        continue
                    for o in opts:
                        g, opt, want = o["group"], o["option"], o["internal"]
                        got = cp.get(g, opt) if cp.has_option(g, opt) else None
                        checked_opts += 1
                        if got is None or got.strip() != want:
                            bad.append(f"{component}: {kind}/{name} reads [{g}] {opt} = "
                                       f"{got if got is not None else '<unset: upstream ' + str(o['upstream_default']) + '>'}"
                                       f", want {want}")
            failures += bad
            print(f"{'FAIL' if bad else 'OK  '} {component}: {len(want_pods)} pod(s) x {len(opts)} "
                  "client endpoint option(s)")
    for c in data:
        if not (root / "blueprints" / c / "chart" / "Chart.yaml").is_file():
            failures.append(f"{DATA} lists {c}, which has no chart")
    for key in sorted(set(ALLOWED) - allowed_seen):
        failures.append(f"ALLOWED entry {key} matches nothing any chart renders: remove it")
    for u in unrendered:
        print(f"FAIL {u}: did not render, so nothing was measured")
    if checked_opts == 0 or scanned_pods == 0:
        print("FAIL: no client endpoint option was checked -- nothing was measured")
        return 1
    for f in failures:
        print(f"FAIL {f}")
    if failures or unrendered:
        print(f"FAIL: {len(failures)} problem(s), {len(unrendered)} unrendered")
        return 1
    print(f"OK: {checked_opts} listed client endpoint option(s) internal at the pods that read them; "
          f"no explicit public endpoint in {scanned_pods} pod template(s) across {len(charts)} charts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
