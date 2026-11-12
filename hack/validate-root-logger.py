#!/usr/bin/env python3
"""Every rendered logging config must print what no other logger handles, and print nothing twice
(#203).

20 component charts sent the root logger to `logging.NullHandler`, so an error logged anywhere
outside the service's own logger (keystoneauth, oslo, a library) was discarded. watcher's
decision engine exited 1 five seconds after every start with an empty log: the cause, a
keystoneauth `DiscoveryFailure`, went to root and vanished. Measured on GKE, Kubernetes 1.36.4,
core-provider 2.13.72, blueprint 0.3.45 (#213).

For every rendered ConfigMap/Secret value with a `[logger_root]` section (a Python logging
fileConfig), two rules:

  1. root has at least one handler whose class is not `logging.NullHandler`;
  2. every other ACTIVE logger (listed in `[loggers] keys`) that has a handler of its own sets
     `propagate = 0`. Otherwise each of its records reaches root's handler too and is printed
     twice, which is why root was silenced in the first place.

Fails when no chart is found, when a chart does not render, or when no logging config is found.

Usage: hack/validate-root-logger.py [repo-root] [component ...]
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

NULL_CLASSES = {"logging.NullHandler", "NullHandler"}


def problems_in(component, where, text):
    cp = configparser.RawConfigParser(strict=False, default_section="__no_default__")
    try:
        cp.read_string(text)
    except configparser.Error as e:
        return [f"{component}: {where} does not parse: {e}"]

    def handlers_of(logger):
        raw = cp.get(f"logger_{logger}", "handlers", fallback="")
        return [h.strip() for h in raw.split(",") if h.strip()]

    def real(handler):
        return cp.get(f"handler_{handler}", "class", fallback="").strip() not in NULL_CLASSES

    out = []
    root_real = [h for h in handlers_of("root") if real(h)]
    if not root_real:
        out.append(f"{component}: {where}: the root logger has no handler but NullHandler, so an "
                   "error logged outside the service's own logger is discarded (#203)")
    keys = [k.strip() for k in cp.get("loggers", "keys", fallback="").split(",") if k.strip()]
    for k in keys:
        if k == "root":
            continue
        own = [h for h in handlers_of(k) if real(h)]
        prop = cp.get(f"logger_{k}", "propagate", fallback="1").strip()
        if own and root_real and prop != "0":
            out.append(f"{component}: {where}: logger '{k}' has its own handler and propagates "
                       "to root, so each of its records is printed twice; set propagate = 0")
    return out


def check(component, docs):
    found, n = [], 0
    for d in docs:
        if d.get("kind") not in ("ConfigMap", "Secret"):
            continue
        for key, val in (d.get("data") or {}).items():
            text = str(val)
            if d["kind"] == "Secret":
                try:
                    text = base64.b64decode(val).decode("utf-8", "replace")
                except Exception:
                    continue
            if "[logger_root]" not in text:
                continue
            n += 1
            found += problems_in(component, f"{d['kind']}/{d['metadata']['name']}:{key}", text)
    return found, n


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
            print(f"{'FAIL' if found else 'OK  '} {component}: {n} logging config(s)")
    for u in unrendered:
        print(f"FAIL {u}: did not render, so nothing was measured")
    if total == 0:
        print("FAIL: no logging config found in any chart -- nothing was measured")
        return 1
    for f in failures:
        print(f"FAIL {f}")
    if failures or unrendered:
        print(f"FAIL: {len(failures)} problem(s), {len(unrendered)} unrendered, {total} logging configs")
        return 1
    print(f"OK: {len(charts)} charts, {total} logging configs, root prints and nothing prints twice")
    return 0


if __name__ == "__main__":
    sys.exit(main())
