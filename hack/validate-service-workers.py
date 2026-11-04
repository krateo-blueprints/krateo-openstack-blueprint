#!/usr/bin/env python3
"""A service whose config section omits `workers` crashes on startup, so require it.

designate registers `workers` in each of its six `[service:*]` sections as
`cfg.IntOpt('workers', help=...)` with NO default, and each binary passes it straight through:

    service.serve(server, workers=CONF['service:central'].workers)

oslo_service's threading backend lets None past both of its own guards --
`if workers is not None and not isinstance(workers, int)` and
`if workers is not None and workers <= 0` -- then takes the `else` branch into ProcessLauncher
and hands None to cotyledon, which raises:

    ValueError: 'workers' must be an int >= 1, not: None (NoneType)

The chart set it for service:api only, so central, mdns, producer, sink and worker crashed on
every startup: 447-452 restarts each over 38 hours on the cluster where this was found, with
designate-worker additionally stuck in Init waiting on a central that would never come up.
Five of six services, since the chart first shipped.

The rule, stated generally: when a chart's config carries `service:<name>` sections, every
section must set `workers` to an int >= 1, and every workload the chart deploys for such a
service must have a section. A workload with no section at all is the worse case -- it reads as
"upstream's default is fine" when there is no default.
"""
import os
import re
import sys

try:
    import yaml
except ImportError:
    sys.exit("FAIL: PyYAML is required by validate-service-workers.py and is not installed")

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 \
    else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WORKLOAD_RE = re.compile(r"^(?:deployment|statefulset|daemonset)-(.+)\.yaml$")


def main():
    bp = os.path.join(ROOT, "blueprints")
    if not os.path.isdir(bp):
        sys.exit(f"FAIL: no blueprints directory at {bp} -- nothing was measured")
    charts = sorted(d for d in os.listdir(bp)
                    if os.path.isfile(os.path.join(bp, d, "chart", "values.yaml")))
    if not charts:
        sys.exit("FAIL: no chart values.yaml found under blueprints/ -- nothing was measured")

    problems = []
    inspected = 0
    for c in charts:
        v = yaml.safe_load(open(os.path.join(bp, c, "chart", "values.yaml"))) or {}
        conf = v.get("conf") or {}
        for project, body in conf.items():
            if not isinstance(body, dict):
                continue
            sections = {k.split(":", 1)[1]: body[k] for k in body
                        if k.startswith("service:") and isinstance(body[k], dict)}
            if not sections:
                continue
            for svc, sec in sorted(sections.items()):
                inspected += 1
                w = sec.get("workers")
                if not isinstance(w, int) or isinstance(w, bool) or w < 1:
                    problems.append(
                        f"{c}: conf.{project}['service:{svc}'].workers = {w!r} -- must be an "
                        "int >= 1; an unset value reaches cotyledon as None and raises")

            # A deployed service with no section at all never gets a workers value either.
            tdir = os.path.join(bp, c, "chart", "templates")
            if not os.path.isdir(tdir):
                continue
            for f in sorted(os.listdir(tdir)):
                m = WORKLOAD_RE.match(f)
                if not m:
                    continue
                svc = m.group(1)
                # Workload files are named after the service; strip the chart's own prefix if
                # the file repeats it, and ignore the non-service workloads a chart carries.
                svc = svc[len(project) + 1:] if svc.startswith(project + "-") else svc
                if svc in sections or svc in ("bootstrap", "test", "rabbit-init"):
                    continue
                if f"service:{svc}" in body:
                    continue
                problems.append(
                    f"{c}: templates/{f} deploys '{svc}' but conf.{project} has no "
                    f"'service:{svc}' section, so its workers value is unset")

    if inspected == 0:
        print("FAIL: no chart declared any service:<name> config section -- the scan "
              "inspected nothing, which means it is not finding them")
        for p in problems:
            print("  " + p)
        return 1
    if problems:
        print(f"FAIL: {len(problems)} service sections without a usable workers value "
              f"({inspected} sections inspected)")
        for p in sorted(set(problems)):
            print("  " + p)
        return 1
    print(f"OK: all {inspected} service:<name> config sections set workers >= 1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
