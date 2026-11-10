#!/usr/bin/env python3
"""No rendered configuration may carry Helm's "<no value>" placeholder (#202).

Helm prints Go's nil placeholder, "<no value>", when a template interpolates a value that is
missing or YAML null. Written into an OpenStack config file it is not "unset": the service reads
the literal text. Measured on GKE, Kubernetes 1.36.4, core-provider 2.13.72:

  - magnum-api-uwsgi.ini carried "processes = <no value>", so uWSGI started in
    "Operational MODE: no-workers", its listen queue filled, and magnum-api never became Ready
    on any install from 0.3.34 to 0.3.44 (#202);
  - six charts in all carried it: barbican, cyborg, gnocchi, magnum, octavia and watcher, the
    last as TLS file paths for its service clients (cafile = <no value>).

Each component chart is rendered under a release name unlike its chart name, as Krateo renders
it: with its defaults, and with its off-by-default manifests.* switches on, so that configs a
default install does not render are covered too. All switches on at once is tried first. Some
switches cannot render without settings the defaults do not carry (TLS issuers for ingress and
certificates, for example), so when that fails each off-by-default switch is rendered on its own,
and any switch that still cannot render is printed as NOT MEASURED with its error -- listed, never
skipped silently. Every ConfigMap's data and every Secret's data (base64-decoded) is scanned.

Fails when no chart is found, when a chart does not render with its defaults, or when no
ConfigMap or Secret is rendered at all.

Usage: hack/validate-rendered-config.py [repo-root] [component ...]
"""
import argparse
import base64
import importlib.util
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("job_hooks", HERE / "validate-job-hooks.py")
job_hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job_hooks)
yaml = job_hooks.yaml

PLACEHOLDER = "<no value>"


def off_switches(chart: Path):
    values = yaml.safe_load((chart / "values.yaml").read_text()) or {}
    manifests = values.get("manifests") or {}
    return sorted(k for k, v in manifests.items() if v is False)


def scan(component, docs):
    found, configs = [], 0
    for d in docs:
        kind = d.get("kind")
        if kind not in ("ConfigMap", "Secret"):
            continue
        configs += 1
        name = (d.get("metadata") or {}).get("name", "?")
        sources = []
        for key, val in (d.get("data") or {}).items():
            if kind == "Secret":
                try:
                    val = base64.b64decode(str(val)).decode("utf-8", "replace")
                except Exception:  # not base64: scan it as it is
                    pass
            sources.append((key, str(val)))
        for key, val in (d.get("stringData") or {}).items():
            sources.append((key, str(val)))
        for key, text in sources:
            for line in text.splitlines():
                if PLACEHOLDER in line:
                    found.append(f"{component}: {kind}/{name} {key}: {line.strip()[:120]}")
    return found, configs


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
    failures, unrendered, unmeasured, total_configs = [], [], [], 0
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        toolkit = root / "charts" / "helm-toolkit"
        for chart in charts:
            component = chart.parent.name
            seen = set()

            def measure(extra):
                nonlocal total_configs
                docs = job_hooks.render(chart, toolkit, component, work / "r", extra)
                found, configs = scan(component, docs)
                total_configs += configs
                for f in found:
                    if f not in seen:
                        seen.add(f)
                        failures.append(f)

            try:
                measure([])
            except Exception as e:  # the defaults must render: otherwise nothing was measured
                unrendered.append(f"{component} (defaults): {str(e)[:300]}")
                print(f"FAIL {component}: defaults did not render")
                continue
            switches = off_switches(chart)
            if switches:
                every = [x for k in switches for x in ("--set", f"manifests.{k}=true")]
                try:
                    measure(every)
                except Exception:
                    for k in switches:
                        try:
                            measure(["--set", f"manifests.{k}=true"])
                        except Exception as e:
                            first = str(e).strip().splitlines()[0][:160] if str(e).strip() else "?"
                            unmeasured.append(f"{component}: manifests.{k} -- {first}")
            print(f"{'FAIL' if seen else 'OK  '} {component}: {len(seen)} placeholder line(s)")
    for u in unrendered:
        print(f"FAIL {u}: did not render, so nothing was measured")
    for u in unmeasured:
        print(f"NOT MEASURED {u}")
    if unmeasured:
        print(f"NOTE: {len(unmeasured)} off-by-default switch(es) cannot render on their own "
              "and were not measured (listed above)")
    if total_configs == 0:
        print("FAIL: no ConfigMap or Secret rendered in any chart -- nothing was measured")
        return 1
    for f in failures:
        print(f"FAIL {f}")
    if failures or unrendered:
        print(f"FAIL: {len(failures)} placeholder line(s), {len(unrendered)} unrendered, "
              f"{total_configs} configs scanned")
        return 1
    print(f"OK: {len(charts)} charts, {total_configs} configs scanned, no {PLACEHOLDER!r} anywhere")
    return 0


if __name__ == "__main__":
    sys.exit(main())
