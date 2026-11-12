#!/usr/bin/env python3
"""Every workload's init container waits on what its chart declares, and nothing silently waits on
nothing.

Why this exists (#236). helm-toolkit.snippets.kubernetes_entrypoint_init_container reads
dependencies.static.<component> for the component name a template passes. When that key does not
exist the init container gets an EMPTY DEPENDENCY_SERVICE and no DEPENDENCY_JOBS, so the workload
starts at once and nothing reports it. Measured on a fresh 0.3.48 GKE install: watcher's decision
engine passed "decision-engine" (the key is decision_engine), started at 15:01:47, and crashed at
15:04:00 on "Table 'watcher.goals' doesn't exist" ten seconds before watcher-db-sync ran -- five
restarts. 20 of 176 init-container calls named a key their chart did not define.

The same snippet reads only these keys of an entry: jobs, services, daemonset, pod, container,
custom_resources. designate's api and central wrote `service:` for `services:`, so their wait on the
database, keystone and the message bus was ignored too -- a second, independent way to wait on
nothing, also checked here.

Checks, for every blueprints/*/chart:
  1. each component name passed to the init-container snippet is a key of dependencies.static, or
     is listed in ALLOWED below with the reason it legitimately waits on nothing;
  2. each dependencies.static entry uses only the keys the snippet reads;
  3. each service an entry waits on is defined under that chart's endpoints: an undefined one
     renders an EMPTY name into DEPENDENCY_SERVICE (cyborg's agent waited on `placement`, which its
     values never defined, so the rendered list ended in a bare comma);
  4. an ALLOWED entry that is no longer needed (the key now exists, or the call is gone) fails, so
     the allowlist cannot outlive its reason.
It fails, rather than passing, when it finds no init-container call at all.
"""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
CALL = re.compile(r'tuple\s+\$\w+\s+"([^"]+)"[^|]*\|\s*include\s+"helm-toolkit\.snippets\.kubernetes_entrypoint_init_container"')
KNOWN_KEYS = {"jobs", "services", "daemonset", "pod", "container", "custom_resources"}
# (chart, component) -> why it may wait on nothing. Each must stay true; see check 4.
ALLOWED = {
    ("mariadb", "mariadb"): "the database server itself: it has nothing to wait for, and upstream "
                            "openstack-helm passes the same name with no static entry",
}


def main():
    problems, calls = [], 0
    used_allow = set()
    for chart in sorted(ROOT.glob("blueprints/*/chart")):
        name = chart.parent.name
        vf = chart / "values.yaml"
        if not vf.is_file():
            continue
        values = yaml.safe_load(vf.read_text()) or {}
        static = (values.get("dependencies") or {}).get("static") or {}
        endpoints = values.get("endpoints") or {}
        for comp, entry in static.items():
            if isinstance(entry, dict):
                for svc in entry.get("services") or []:
                    sname = (svc or {}).get("service") if isinstance(svc, dict) else None
                    if sname and not isinstance(endpoints.get(sname), dict):
                        problems.append(f"{vf.relative_to(ROOT)}: dependencies.static.{comp} waits on service "
                                        f"{sname!r}, but endpoints has no {sname!r}, so it resolves to an empty "
                                        "name in DEPENDENCY_SERVICE")
                bad = sorted(set(entry) - KNOWN_KEYS)
                if bad:
                    problems.append(f"{vf.relative_to(ROOT)}: dependencies.static.{comp} has {bad}; the "
                                    f"init-container snippet reads only {sorted(KNOWN_KEYS)}, so that part "
                                    "of the wait is silently ignored")
        for t in sorted(chart.glob("templates/**/*.yaml")):
            for m in CALL.finditer(t.read_text()):
                calls += 1
                comp = m.group(1)
                if comp in static or comp == "pod_dependency":
                    continue
                if (name, comp) in ALLOWED:
                    used_allow.add((name, comp))
                    continue
                problems.append(f"{t.relative_to(ROOT)}: passes component {comp!r}, which is not a key of "
                                f"{name}'s dependencies.static, so its init container waits on nothing")
    for key, why in ALLOWED.items():
        if key not in used_allow:
            problems.append(f"ALLOWED entry {key} ({why}) is no longer needed: remove it")
    if calls == 0:
        print("FAIL: no init-container call found in any chart template: the scan matched nothing, "
              "which is a broken gate, not a clean tree")
        return 1
    if problems:
        print(f"FAIL: {len(problems)} problem(s) in {calls} init-container calls")
        for p in problems:
            print("  - " + p)
        return 1
    print(f"OK: {calls} init-container calls each wait on a declared dependencies.static entry "
          f"({len(ALLOWED)} documented exception); every entry uses only keys the snippet reads and waits only "
          f"on services its chart defines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
