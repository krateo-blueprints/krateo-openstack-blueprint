#!/usr/bin/env python3
"""oslo_middleware's Healthcheck must be a paste APP, never a filter.

`oslo_middleware:Healthcheck.factory` -- the paste *filter* entry point -- raises
NotImplementedError unconditionally in the shipped library:

    def factory(cls, global_conf, **local_conf):
        raise NotImplementedError(
            'HealthcheckMiddleware should be deployed as an app, not a filter')

So a chart that declares `filter:healthcheck` with that factory cannot build its WSGI
application at all. magnum shipped exactly that: a hand-rolled `pipeline:main` predating
upstream's composite restructure, which left magnum-api Running and 0/1 forever with uwsgi
reporting "no python application found" and its listen queue filling with probe traffic.
Eleven other API charts here already use the correct `app:healthcheck` +
`Healthcheck.app_factory` form, and the image's own api-paste.ini uses it too.

Checked against values.yaml rather than a render, because `conf.paste` is plain data: no
template can turn a filter entry into an app entry.
"""
import os
import sys

try:
    import yaml
except ImportError:
    sys.exit("FAIL: PyYAML is required by validate-paste-healthcheck.py and is not installed")

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 \
    else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BAD = "Healthcheck.factory"
GOOD = "Healthcheck.app_factory"


def main():
    bp = os.path.join(ROOT, "blueprints")
    if not os.path.isdir(bp):
        sys.exit(f"FAIL: no blueprints directory at {bp} -- nothing was measured")
    charts = sorted(d for d in os.listdir(bp)
                    if os.path.isfile(os.path.join(bp, d, "chart", "values.yaml")))
    if not charts:
        sys.exit("FAIL: no chart values.yaml found under blueprints/ -- nothing was measured")

    problems = []
    with_paste = 0
    for c in charts:
        path = os.path.join(bp, c, "chart", "values.yaml")
        try:
            v = yaml.safe_load(open(path)) or {}
        except yaml.YAMLError as e:
            problems.append(f"{c}: values.yaml does not parse: {e}")
            continue
        paste = ((v.get("conf") or {}).get("paste") or {})
        if not isinstance(paste, dict) or not paste:
            continue
        with_paste += 1
        for section, body in paste.items():
            if not isinstance(body, dict):
                continue
            for key, val in body.items():
                if not isinstance(val, str) or BAD not in val or GOOD in val:
                    continue
                problems.append(
                    f"{c}: conf.paste['{section}'].{key} = {val} -- Healthcheck.factory "
                    f"raises NotImplementedError; declare it as app:healthcheck with {GOOD}")

    if with_paste == 0:
        print("FAIL: no chart declared conf.paste -- the scan inspected nothing")
        for p in problems:
            print("  " + p)
        return 1
    if problems:
        print(f"FAIL: {len(problems)} paste healthcheck misdeclarations "
              f"({with_paste} charts with a paste pipeline inspected)")
        for p in sorted(set(problems)):
            print("  " + p)
        return 1
    print(f"OK: {with_paste} charts declare a paste pipeline and none runs Healthcheck "
          "as a filter")
    return 0


if __name__ == "__main__":
    sys.exit(main())
