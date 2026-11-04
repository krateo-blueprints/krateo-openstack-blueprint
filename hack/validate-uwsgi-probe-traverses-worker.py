#!/usr/bin/env python3
"""A container served by uwsgi may not be probed with a bare tcpSocket.

`uwsgi --http` (and `http-socket`) runs an HTTP router in the MASTER process, separate from the
workers. The router accepts the TCP connection itself. So a `tcpSocket` probe against the service
port succeeds while every worker is wedged, blocked on a database, or never loaded the
application at all -- the probe is answered by the process that is still fine.

Three measurements from this repo, each a different way for the same blind spot to pay off:

  * gnocchi-api shipped with a bare tcpSocket readiness probe and reported Ready while the API
    returned 500 to every request (#110).
  * designate-api and magnum-api answered "no python application found" for 38 hours -- the
    master accepting connections, no application loaded, pods Running and never restarted
    (#149).
  * cyborg-api had tcpSocket on BOTH probes. It was crash-looping for an unrelated reason
    (#158), so the probes were not what hid it -- but once cyborg-api is served by uwsgi those
    probes would report a healthy container the moment the master bound the port.

An HTTP GET traverses a worker, which is the thing the probe is supposed to be about.

This is the general form of hack/validate-probe-not-uwsgi-stats.py, which forbids probing uwsgi's
stats port (1717) because the stats endpoint also answers from the master. Same failure, different
door.

Derived, not listed: a container counts as uwsgi-served when its command runs a script in this
chart's own ConfigMaps that contains `exec uwsgi`. A chart that moves to uwsgi and keeps its old
tcpSocket probe fails here rather than on someone's cluster. 16 containers qualify today and none
has a bare tcpSocket probe, so there are no waivers and none should be needed.
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    sys.exit("FAILED MEASUREMENT: PyYAML unavailable, so no chart was inspected")

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 \
    else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet")
PROBES = ("readinessProbe", "livenessProbe", "startupProbe")


def render(chart_dir):
    tmp = tempfile.mkdtemp()
    try:
        dst = os.path.join(tmp, "c")
        shutil.copytree(chart_dir, dst)
        cy = os.path.join(dst, "Chart.yaml")
        meta = open(cy).read()
        open(cy, "w").write(meta.replace("CHART_VERSION", "0.0.0-gate"))
        tk = os.path.join(ROOT, "charts", "helm-toolkit")
        if os.path.isdir(tk):
            os.makedirs(os.path.join(dst, "charts"), exist_ok=True)
            shutil.copytree(tk, os.path.join(dst, "charts", "helm-toolkit"))
        p = subprocess.run(["helm", "template", "gate", dst], capture_output=True, text=True)
        if p.returncode:
            return None
        return [x for x in yaml.safe_load_all(p.stdout) if isinstance(x, dict)]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    bp = os.path.join(ROOT, "blueprints")
    if not os.path.isdir(bp):
        sys.exit(f"FAIL: no blueprints directory at {bp} -- nothing was measured")
    charts = sorted(d for d in os.listdir(bp) if os.path.isdir(os.path.join(bp, d, "chart")))
    if not charts:
        sys.exit("FAIL: no charts under blueprints/ -- nothing was measured")

    served, problems, rendered = 0, [], 0
    for c in charts:
        docs = render(os.path.join(bp, c, "chart"))
        if docs is None:
            continue
        rendered += 1
        uwsgi_scripts = set()
        for x in docs:
            if x.get("kind") != "ConfigMap":
                continue
            for k, v in (x.get("data") or {}).items():
                if isinstance(v, str) and "exec uwsgi" in v:
                    uwsgi_scripts.add(os.path.basename(k))
        if not uwsgi_scripts:
            continue
        for x in docs:
            if x.get("kind") not in WORKLOADS:
                continue
            for container in x["spec"]["template"]["spec"].get("containers") or []:
                cmd = " ".join(container.get("command") or [])
                if not any(script in cmd for script in uwsgi_scripts):
                    continue
                served += 1
                for pk in PROBES:
                    probe = container.get(pk) or {}
                    if "tcpSocket" not in probe:
                        continue
                    problems.append(
                        f"{c}: {x['metadata']['name']}/{container['name']} {pk} is a bare "
                        f"tcpSocket on port {probe['tcpSocket'].get('port')}. uwsgi's HTTP "
                        f"router accepts the connection in the MASTER, so this passes with "
                        f"every worker wedged or no application loaded. Use an httpGet against "
                        f"a path the application serves."
                    )

    if rendered == 0:
        sys.exit("FAIL: no chart rendered, so no probe was inspected")
    if served == 0:
        print("FAIL: found no container served by `exec uwsgi` at all. Either the charts stopped "
              "using uwsgi or this gate no longer recognises how they start it -- which would "
              "make every run of it a pass over nothing.")
        return 1
    if problems:
        print(f"FAIL: {len(problems)} uwsgi-served container probe(s) cannot see a wedged worker "
              f"({served} uwsgi-served containers across {rendered} charts)")
        for p in sorted(set(problems)):
            print("  " + p)
        return 1
    print(f"OK: {served} containers are served by uwsgi across {rendered} charts, and none is "
          "probed with a bare tcpSocket")
    return 0


if __name__ == "__main__":
    sys.exit(main())
