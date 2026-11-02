#!/usr/bin/env python3
"""No kubelet probe may target a uwsgi stats port.

Why this gate exists. uwsgi's `stats` option opens a second socket served by the MASTER
process, and `stats-http = true` makes it answer plain HTTP. The master is the process that
binds the listening socket, forks workers and reaps them; it does not import the application
and it does not handle requests. So that endpoint answers 200 with a JSON blob whenever the
master is alive -- with every worker wedged, with every worker stopped, and with the
application failing to import. There is no state of the service in which it says no.

Pointed at by LIVENESS, which is the probe that restarts things, that inverts the probe's
purpose: liveness becomes strictly weaker than the readiness probe on the same container.
The practical failure shape is a hung API that presents as endpoints empty, pod Running,
zero restarts -- readiness correctly withdraws it from the Service while liveness reports
healthy forever and nothing restarts it. Four containers shipped exactly that: placement-api,
neutron-server, heat-api and heat-cfn (issue #110's family of "Ready means nothing" defects).

Pointed at by READINESS or a STARTUP probe it is no better, so this gate covers all three
rather than only liveness. Nothing legitimately listens on a uwsgi stats port but uwsgi's own
master, and no probe anywhere should be satisfied by it.

The forbidden ports are DERIVED, not hardcoded. 1717 is only openstack-helm's convention; a
chart that moves its stats socket must not quietly fall outside the gate. They are read from
the rendered uwsgi ini files -- the bytes the container actually runs -- and from each chart's
values, and the union is applied to every chart: the stats socket is a uwsgi convention shared
across all of them, and no OpenStack API listens on such a port, so a probe pointing there is
wrong in whichever chart it appears.

Renders every chart and inspects the probes as the kubelet would receive them, rather than
grepping templates, because the port can arrive from values, from a template default or from a
named container port, and only the rendered object settles which.

Refuses to pass on zero input. Four gates in this repo printed "SKIP: PyYAML unavailable" and
exited 0 for weeks (#120), so every case where this gate would measure nothing -- no charts, no
stats ports to compare against, no probes, a chart that will not render -- exits non-zero as a
FAILED MEASUREMENT instead.
"""
import base64
import binascii
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
TOOLKIT = ROOT / "charts/helm-toolkit"
# The umbrella renders Compositions for the other charts and carries no pod of its own; it is
# skipped by hack/validate-component-dag.py for the same reason. Named rather than discovered,
# so that a chart becoming unrenderable is a measurement failure and not a silent skip.
SKIP_RENDER = {"openstack"}
PROBES = ("livenessProbe", "readinessProbe", "startupProbe")
WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod")
# `stats = 0.0.0.0:1717` in a rendered ini, or `stats: 0.0.0.0:1717` in values. uwsgi spells the
# option `stats` and `stats-server`; both take <addr>:<port> or a bare socket path, and only the
# former yields a port.
STATS_LINE = re.compile(r"^\s*stats(?:-server)?\s*[:=]\s*\S*:(\d{1,5})\s*$", re.M)

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so no probe was ever inspected")
    sys.exit(2)


def render(chart):
    """helm template a chart into a fresh temporary copy. Returns (docs, error)."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "chart"
        shutil.copytree(chart, work)
        cy = work / "Chart.yaml"
        # The chart version is a literal placeholder the release stamps, and helm needs semver.
        cy.write_text(cy.read_text().replace("CHART_VERSION", "0.0.0-gate"))
        if TOOLKIT.is_dir():
            (work / "charts").mkdir(exist_ok=True)
            shutil.copytree(TOOLKIT, work / "charts/helm-toolkit", dirs_exist_ok=True)
        out = subprocess.run(
            # Without --kube-version a kubeVersion-constrained chart renders nothing, and a
            # gate that accepts an empty render accepts anything.
            ["helm", "template", "gate", str(work), "--kube-version", "1.36.0"],
            capture_output=True, text=True,
        )
    if out.returncode != 0:
        last = out.stderr.strip().splitlines()
        return None, (last[-1][:200] if last else "no stderr")
    docs = [d for d in yaml.safe_load_all(out.stdout) if isinstance(d, dict)]
    if not docs:
        return None, "rendered 0 objects"
    return docs, None


def stats_ports_in_render(docs):
    """Every uwsgi stats port in a rendered chart's ConfigMap and Secret payloads.

    This reads the ini the container is handed, which is the only place that settles where the
    stats socket ends up: the values key and the ini key are not the same spelling, and a chart
    may template the ini directly.
    """
    ports = set()
    for d in docs:
        if d.get("kind") not in ("ConfigMap", "Secret"):
            continue
        for key, raw in (d.get("data") or {}).items():
            if not isinstance(raw, str):
                continue
            if d["kind"] == "Secret":
                try:
                    raw = base64.b64decode(raw, validate=True).decode("utf-8", "replace")
                except (binascii.Error, ValueError):
                    continue
            for m in STATS_LINE.finditer(raw):
                ports.add(int(m.group(1)))
    return ports


def stats_ports_in_values(node):
    """Every uwsgi stats port in a chart's values tree.

    Belt and braces alongside the rendered ini: a stats socket configured in values but not yet
    reaching an ini is still the port a probe must not use, and the values are also where a
    re-vendor reintroduces one.
    """
    ports = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("stats", "stats-server") and isinstance(v, str):
                m = re.fullmatch(r"\S*:(\d{1,5})", v.strip())
                if m:
                    ports.add(int(m.group(1)))
            ports |= stats_ports_in_values(v)
    elif isinstance(node, list):
        for v in node:
            ports |= stats_ports_in_values(v)
    return ports


def pod_specs(doc):
    """Yield (path, podSpec) for the pod template a workload object carries."""
    kind = doc.get("kind")
    spec = doc.get("spec") or {}
    if kind == "Pod":
        yield "spec", spec
    elif kind == "CronJob":
        tmpl = ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}
        yield "spec.jobTemplate.spec.template.spec", tmpl.get("spec") or {}
    else:
        yield "spec.template.spec", (spec.get("template") or {}).get("spec") or {}


def probe_ports(probe, container):
    """The ports a probe reaches, as (port, how) pairs.

    httpGet/tcpSocket/grpc carry a port directly, and it may be a NAME, which resolves against
    the container's own ports. An exec probe carries the port inside a command string, so those
    are returned as text for the caller to search; returning text rather than parsing it keeps
    this from inventing a port that is not there.
    """
    named = {
        p["name"]: p["containerPort"]
        for p in (container.get("ports") or [])
        if isinstance(p, dict) and p.get("name") and p.get("containerPort")
    }
    found, text = [], []
    for scheme in ("httpGet", "tcpSocket", "grpc"):
        port = (probe.get(scheme) or {}).get("port")
        if port is None:
            continue
        if isinstance(port, int):
            found.append((port, scheme))
        elif isinstance(port, str):
            if port.isdigit():
                found.append((int(port), scheme))
            elif port in named:
                found.append((named[port], f"{scheme} via port name {port!r}"))
    for arg in (probe.get("exec") or {}).get("command") or []:
        if isinstance(arg, str):
            text.append(arg)
    return found, "\n".join(text)


charts = sorted(
    p.parent for p in ROOT.glob("blueprints/*/chart/Chart.yaml")
)
if not charts:
    print(f"FAILED MEASUREMENT: no charts under {ROOT}/blueprints -- wrong path?")
    sys.exit(2)

renders, stats_ports, measurement = {}, set(), []
for chart in charts:
    name = chart.parent.name
    values_file = chart / "values.yaml"
    if values_file.is_file():
        try:
            stats_ports |= stats_ports_in_values(yaml.safe_load(values_file.read_text()))
        except yaml.YAMLError as e:
            measurement.append(f"{name}: values.yaml does not parse ({e.__class__.__name__})")
    if name in SKIP_RENDER:
        continue
    docs, err = render(chart)
    if err:
        measurement.append(f"{name}: {err}, so its probes were never inspected")
        continue
    renders[name] = docs
    stats_ports |= stats_ports_in_render(docs)

if measurement:
    print("FAILED MEASUREMENT: some charts could not be inspected, so a pass would be a guess:")
    for m in measurement:
        print(f"  {m}")
    sys.exit(2)

if not stats_ports:
    print(
        "FAILED MEASUREMENT: no uwsgi stats port is configured in any chart, so this gate has\n"
        "  nothing to compare the probes against. If uwsgi stats have genuinely been removed\n"
        "  everywhere, delete this gate in the same change; do not let it report success over\n"
        "  an empty forbidden set."
    )
    sys.exit(2)

findings, inspected = [], 0
for name, docs in sorted(renders.items()):
    for doc in docs:
        if doc.get("kind") not in WORKLOADS:
            continue
        obj = f"{doc['kind']}/{(doc.get('metadata') or {}).get('name', '?')}"
        for path, pod in pod_specs(doc):
            containers = (pod.get("containers") or []) + (pod.get("initContainers") or [])
            for c in containers:
                for kind in PROBES:
                    probe = c.get(kind)
                    if not isinstance(probe, dict):
                        continue
                    inspected += 1
                    ports, text = probe_ports(probe, c)
                    for port, how in ports:
                        if port in stats_ports:
                            findings.append(
                                f"{name}: {obj} container {c.get('name')!r} {kind} targets "
                                f"port {port} ({how})"
                            )
                    for port in sorted(stats_ports):
                        # A colon before the port, so an unrelated 1717 in a path or a timeout
                        # cannot be read as a socket. `(?!\d)` stops 17170 matching 1717.
                        if re.search(rf":{port}(?!\d)", text):
                            findings.append(
                                f"{name}: {obj} container {c.get('name')!r} {kind} reaches "
                                f"port {port} from its exec command"
                            )

if not inspected:
    print(
        f"FAILED MEASUREMENT: {len(renders)} charts rendered and not one probe was found. "
        f"Every API container in this repo has at least a readiness probe, so this means the "
        f"probes were not read, not that none exist."
    )
    sys.exit(2)

if findings:
    print(f"FAIL: {len(findings)} probe(s) target a uwsgi stats port "
          f"({', '.join(str(p) for p in sorted(stats_ports))}).\n")
    for f in findings:
        print(f"  - {f}")
    print(
        "\nThe uwsgi stats socket is served by the MASTER process with stats-http = true. It"
        "\nanswers 200 whenever the master is alive -- with every worker wedged, with every"
        "\nworker stopped, and with the application failing to import -- so a probe on it can"
        "\nnever be false. On liveness that means a hung API is never restarted; on readiness"
        "\nit means a broken API reports Ready at every layer."
        "\n\nPoint the probe at the service's own endpoint port instead, where a worker must"
        "\nserve the request. GET / returns the static version-discovery document, which needs"
        "\nno database, no keystone and no rabbit, so it stays safe for liveness."
    )
    sys.exit(1)

print(f"OK: {inspected} probes across {len(renders)} charts, none targeting a uwsgi stats port "
      f"({', '.join(str(p) for p in sorted(stats_ports))})")
