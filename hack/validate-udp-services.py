#!/usr/bin/env python3
"""A Service port in front of a UDP-only listener must declare UDP, and so must its containerPort.

Why this gate exists. `protocol` on a ServicePort and on a ContainerPort defaults to TCP when it
is omitted. That is the API default, not "whatever the backend speaks" -- so a port entry written
without a protocol in front of a UDP-only daemon produces a Service that kube-proxy programs for
TCP and a pod that advertises a TCP port, while the process behind it has no TCP listener at all.
Every datagram sent to the Service is dropped before it reaches a pod.

That failure is silent in both directions, which is why it needs a gate and not just a fix:

  * UDP has no reply, so a sender learns nothing. There is no connection to refuse, no RST, no
    timeout that distinguishes "dropped" from "accepted and ignored".
  * The receiver reports nothing either. gnocchi's Stats.flush() wraps each individual measure in
    `except Exception as e: LOG.error("Unable to add measure %s: %s", ...)` -- per measure, caught
    and logged -- so even a datagram that did arrive and failed to store would only be logged.

Meanwhile the Service has endpoints, the pod is Ready, and the metric pipeline is empty. This is
the same shape as #110 and #115: a layer that looks healthy at every observable point and carries
nothing. Issue #127.

The gate has two halves, because one cannot do the job.

HALF A -- the UDP_ONLY registry, below. Whether a daemon is UDP-only is a property of the
application's source code, not of anything in the chart, so no rule over the chart can derive it.
The registry is where that evidence lives: each entry names the image it was measured in and the
source lines that settle it, the same way hack/upstream-divergences.yaml holds the reasoning for a
divergence. For each entry this asserts the ServicePort is UDP, the container's port is UDP, the
two actually resolve to each other, the Service selects the workload, no TCP port is advertised on
the same number, and no probe on that container tries to connect to it -- a tcpSocket or httpGet
on a datagram socket is a probe that can never pass.

HALF B -- every Service in every chart, with no configuration at all. A ServicePort and the
ContainerPort it resolves to must agree on protocol. This needs no knowledge of which daemons are
UDP-only, and it is what catches the half-revert: fix the Service and not the DaemonSet, or the
reverse, and the two disagree. It reports only genuine disagreements, so a chart that declares no
container ports (they are optional and informational) produces nothing.

Half A alone would not notice a new UDP service elsewhere. Half B alone would not have caught the
defect this gate was written for, because there both halves agreed -- on TCP. Together, reverting
either half fails, and reverting both fails too.

What half B deliberately does NOT check, found by differential-testing it. designate's mdns
exposes the SAME port number twice, as d-mdns (TCP) and d-mdns-udp (UDP), in both the Service and
the Deployment. A ServicePort with no targetPort resolves by number, so flipping the Service's
d-mdns-udp entry to TCP still resolves to a real TCP containerPort on that number, and half B
reports nothing -- correctly, because nothing is unreachable. Only the port's NAME says it was
meant to be the UDP one, and keying a gate on port names would be a guess. What half B does catch
there, verified, is the reachable form of the mistake: a UDP ServicePort whose number or name
resolves to no UDP containerPort at all.

Renders every chart and inspects the objects the API server would receive, rather than grepping
templates, because a protocol can arrive from values, from a snippet or from a template default,
and only the rendered object settles it.

Refuses to pass on zero input. Four gates in this repo printed "SKIP: PyYAML unavailable" and
exited 0 for weeks (#120), so every case where this gate would measure nothing -- no charts, a
chart that will not render, a registry entry whose Service, workload or container is not there,
an empty registry, no Service port inspected -- exits non-zero as a FAILED MEASUREMENT rather
than reporting success over nothing. hack/validate-upstream-divergences.py is the reference.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
TOOLKIT = ROOT / "charts/helm-toolkit"

# The umbrella renders Compositions for the other charts and carries no pod of its own; it is
# skipped by hack/validate-component-dag.py and hack/validate-probe-not-uwsgi-stats.py for the
# same reason. Named rather than discovered, so a chart becoming unrenderable is a measurement
# failure and not a silent skip.
SKIP_RENDER = {"openstack"}

WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet")
PROBES = ("livenessProbe", "readinessProbe", "startupProbe")

# Daemons that speak ONLY UDP, with where that was measured. A new entry needs the image and the
# source lines, not an assertion: this is the one fact about the application that the chart cannot
# express and a reader cannot check by reading the chart.
#
# designate's mdns is deliberately NOT here, and is the reason this registry is not just "every
# port named like statsd". mdns binds TCP *and* UDP on 5354 -- DNS requires both, zone transfers
# are TCP -- and upstream's service-mdns.yaml/deployment-mdns.yaml correctly declare a d-mdns
# (TCP) port beside a d-mdns-udp one. Its TCP readiness probe is valid. Half B still covers it.
UDP_ONLY = (
    {
        "component": "gnocchi",
        "service": "gnocchi-statsd",
        "port_name": "gn-stats",
        "kind": "DaemonSet",
        "workload": "gnocchi-statsd",
        "container": "gnocchi-statsd",
        "image": "quay.io/openstack.kolla/gnocchi-statsd:2026.1-ubuntu-noble",
        "evidence": (
            "gnocchi 4.7.0, gnocchi/statsd.py start():\n"
            "          loop = asyncio.get_event_loop()\n"
            "          # TODO(jd) Add TCP support\n"
            "          listen = loop.create_datagram_endpoint(\n"
            "              lambda: StatsdServer(stats),\n"
            "              local_addr=(conf.statsd.host, conf.statsd.port))\n"
            "        create_datagram_endpoint, with the TODO beside it. There is no TCP listener\n"
            "        on the statsd port and there never has been."
        ),
    },
)

findings = []


def fail(msg):
    findings.append(msg)


def measurement_failed(lines):
    print("FAILED MEASUREMENT: a pass would be a guess, so this is not a pass:")
    for line in lines:
        print(f"  {line}")
    sys.exit(2)


try:
    import yaml
except ImportError:
    measurement_failed(
        ["PyYAML unavailable, so not one Service port was ever inspected"]
    )


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
        try:
            out = subprocess.run(
                # Without --kube-version a kubeVersion-constrained chart renders nothing, and a
                # gate that accepts an empty render accepts anything.
                ["helm", "template", "gate", str(work), "--kube-version", "1.36.0"],
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            return None, "helm is not installed, so nothing could be rendered"
    if out.returncode != 0:
        last = out.stderr.strip().splitlines()
        return None, f"helm template failed: {last[-1][:200] if last else 'no stderr'}"
    docs = [d for d in yaml.safe_load_all(out.stdout) if isinstance(d, dict)]
    if not docs:
        return None, "rendered 0 objects"
    return docs, None


def container_ports(container):
    """The container's declared ports as a list of (name, number, protocol, was_declared).

    was_declared distinguishes "protocol: TCP" from an omitted protocol. Both produce TCP, but
    only the second is the defect this gate is about, and a message that cannot tell them apart
    sends a reader looking for a line that is not there.
    """
    out = []
    for p in container.get("ports") or []:
        if not isinstance(p, dict):
            continue
        number = p.get("containerPort")
        if not isinstance(number, int):
            continue
        out.append((p.get("name"), number, p.get("protocol") or "TCP", "protocol" in p))
    return out


def selects(selector, labels):
    """True if a Service selector matches a pod's labels, as the endpoints controller would."""
    if not selector:
        return False
    return all(labels.get(k) == v for k, v in selector.items())


def resolve_target(service_port):
    """What a ServicePort resolves to in the pod: ('name', str) or ('number', int).

    An absent targetPort defaults to the Service's own port number, which is the shape every
    chart in this repo uses.
    """
    target = service_port.get("targetPort")
    if target is None:
        return "number", service_port["port"]
    if isinstance(target, int):
        return "number", target
    if isinstance(target, str) and target.isdigit():
        return "number", int(target)
    return "name", target


def probe_targets(probe):
    """The (port_or_name, scheme) pairs a connecting probe tries to reach.

    Only the schemes that open a connection: an exec probe runs a command and says nothing about
    the port's protocol.
    """
    out = []
    for scheme in ("httpGet", "tcpSocket", "grpc"):
        port = (probe.get(scheme) or {}).get("port")
        if port is not None:
            out.append((port, scheme))
    return out


# -------------------------------------------------------------------------------------------
# Render every chart. Any chart that will not render is a measurement failure, not a skip.
# -------------------------------------------------------------------------------------------
charts = sorted(p.parent for p in ROOT.glob("blueprints/*/chart/Chart.yaml"))
if not charts:
    measurement_failed([f"no charts under {ROOT}/blueprints -- wrong path?"])

if not UDP_ONLY:
    measurement_failed([
        "the UDP_ONLY registry is empty, so half A of this gate checks nothing. If every",
        "UDP-only daemon has genuinely left this repo, delete the gate in the same change.",
    ])

renders, problems = {}, []
for chart in charts:
    name = chart.parent.name
    if name in SKIP_RENDER:
        continue
    docs, err = render(chart)
    if err:
        problems.append(f"{name}: {err}, so its Service ports were never inspected")
        continue
    renders[name] = docs

if problems:
    measurement_failed(problems)

# -------------------------------------------------------------------------------------------
# Index the rendered objects.
# -------------------------------------------------------------------------------------------
services = {}    # component -> {name: service doc}
workloads = {}   # component -> {(kind, name): workload doc}
for name, docs in renders.items():
    services[name] = {}
    workloads[name] = {}
    for d in docs:
        meta = d.get("metadata") or {}
        if d.get("kind") == "Service" and meta.get("name"):
            services[name][meta["name"]] = d
        elif d.get("kind") in WORKLOADS and meta.get("name"):
            workloads[name][(d["kind"], meta["name"])] = d

# -------------------------------------------------------------------------------------------
# HALF A. Each registered UDP-only daemon, end to end.
# -------------------------------------------------------------------------------------------
checked_entries = 0
for entry in UDP_ONLY:
    comp = entry["component"]
    where = f"{comp}: {entry['kind']}/{entry['workload']} container {entry['container']!r}"
    if comp not in renders:
        measurement_failed([
            f"{comp} is in the UDP_ONLY registry but no chart for it rendered, so the one",
            "defect this gate was written for was never looked at.",
        ])

    svc = services[comp].get(entry["service"])
    if svc is None:
        measurement_failed([
            f"{comp}: no Service {entry['service']!r} in the rendered chart.",
            "If it was renamed, rename it in the UDP_ONLY registry too; if it was removed,",
            f"this gate is no longer measuring the {entry['container']} datagram path at all.",
        ])

    ports = (svc.get("spec") or {}).get("ports") or []
    named = [p for p in ports if isinstance(p, dict) and p.get("name") == entry["port_name"]]
    if not named:
        measurement_failed([
            f"{comp}: Service {entry['service']} has no port named {entry['port_name']!r}.",
            f"            got: {[p.get('name') for p in ports]}",
            "Rename it here as well, or this gate checks a port that no longer exists.",
        ])
    svc_port = named[0]
    checked_entries += 1

    # 1. The ServicePort itself.
    proto = svc_port.get("protocol")
    if proto != "UDP":
        fail(
            f"{comp}: Service {entry['service']} port {entry['port_name']!r} has protocol "
            f"{proto or 'TCP (defaulted -- no protocol declared)'}, not UDP.\n"
            f"      {entry['container']} is UDP-only. Measured in {entry['image']}:\n"
            f"        {entry['evidence']}\n"
            f"      kube-proxy programs this protocol, so a TCP port here means every datagram "
            f"sent to {entry['service']}:{svc_port.get('port')} is dropped before it reaches a "
            f"pod -- with no error at either end."
        )

    # 2. No TCP port advertised on the same number. The listener does not exist, so such a port
    #    only invites clients to connect to nothing.
    for p in ports:
        if not isinstance(p, dict) or p is svc_port:
            continue
        if p.get("port") == svc_port.get("port") and (p.get("protocol") or "TCP") != "UDP":
            fail(
                f"{comp}: Service {entry['service']} also advertises port "
                f"{svc_port.get('port')} as {p.get('protocol') or 'TCP'} (port name "
                f"{p.get('name')!r}).\n"
                f"      There is no TCP listener behind it. See the evidence above."
            )

    # 3. The workload, the container and its port.
    wl = workloads[comp].get((entry["kind"], entry["workload"]))
    if wl is None:
        measurement_failed([
            f"{comp}: no {entry['kind']}/{entry['workload']} rendered, so the container port "
            "behind the Service was never inspected.",
        ])
    pod = ((wl.get("spec") or {}).get("template") or {}).get("spec") or {}
    matches = [c for c in (pod.get("containers") or []) if c.get("name") == entry["container"]]
    if not matches:
        measurement_failed([
            f"{comp}: no container {entry['container']!r} in {entry['kind']}/"
            f"{entry['workload']}, so its port was never inspected.",
        ])
    container = matches[0]

    cports = container_ports(container)
    same_number = [p for p in cports if p[1] == svc_port.get("port")]
    if not same_number:
        fail(
            f"{where} declares no containerPort {svc_port.get('port')}.\n"
            f"      The chart declares one, and it is the declaration that makes the pod "
            f"describe its own datagram socket correctly -- what a named targetPort resolves "
            f"by, what kubectl reports, and what a NetworkPolicy reads. Losing it removes the "
            f"second half of the fix this gate protects."
        )
    for _pname, number, cproto, declared in same_number:
        if cproto != "UDP":
            how_tcp = cproto if declared else f"{cproto} (defaulted -- no protocol declared)"
            fail(
                f"{where} declares containerPort {number} as {how_tcp}, not UDP.\n"
                f"      A ContainerPort's protocol defaults to TCP exactly as a ServicePort's "
                f"does, so this is the same defect one layer down. See the evidence above."
            )

    # 4. The ServicePort must actually resolve to that container port.
    how, target = resolve_target(svc_port)
    if how == "number":
        if target != svc_port.get("port") and not any(p[1] == target for p in cports):
            fail(
                f"{comp}: Service {entry['service']} port {entry['port_name']!r} targets "
                f"port {target}, which {where} does not declare."
            )
    else:
        # Kubernetes matches a named targetPort on name AND protocol, so a UDP ServicePort
        # pointing at a TCP containerPort of that name resolves to nothing.
        if not any(p[0] == target and p[2] == "UDP" for p in cports):
            fail(
                f"{comp}: Service {entry['service']} port {entry['port_name']!r} targets the "
                f"named port {target!r}, and {where} declares no UDP port with that name.\n"
                f"      A named targetPort is matched on name AND protocol."
            )

    # 5. The Service must be in front of that pod at all.
    selector = (svc.get("spec") or {}).get("selector") or {}
    labels = ((wl.get("spec") or {}).get("template") or {}).get("metadata", {}).get("labels") or {}
    if not selects(selector, labels):
        fail(
            f"{comp}: Service {entry['service']}'s selector does not match the pod labels of "
            f"{entry['kind']}/{entry['workload']}, so it has no endpoints and fronts nothing.\n"
            f"      selector: {selector}\n"
            f"      labels:   {labels}"
        )

    # 6. No probe may try to connect to a datagram socket.
    port_names = {p[0] for p in cports if p[1] == svc_port.get("port") and p[0]}
    for kind in PROBES:
        probe = container.get(kind)
        if not isinstance(probe, dict):
            continue
        for target_port, scheme in probe_targets(probe):
            hit = target_port in (svc_port.get("port"), str(svc_port.get("port")))
            hit = hit or target_port in port_names
            if hit:
                fail(
                    f"{where} has a {kind} with a {scheme} on port {target_port!r}.\n"
                    f"      That port is a datagram socket, so there is nothing to connect to "
                    f"and the probe can never pass. It would hold the pod unready forever, "
                    f"or -- on liveness -- CrashLoop a perfectly healthy daemon."
                )

if checked_entries == 0:
    measurement_failed([
        "no UDP_ONLY registry entry was inspected, so half A measured nothing.",
    ])

# -------------------------------------------------------------------------------------------
# HALF B. Every Service port in every chart must agree on protocol with the container port it
# resolves to. No configuration, so a new UDP service anywhere is covered the day it lands.
# -------------------------------------------------------------------------------------------
inspected_ports = 0
for comp in sorted(renders):
    for svc_name, svc in sorted(services[comp].items()):
        spec = svc.get("spec") or {}
        selector = spec.get("selector") or {}
        if not selector:
            # No selector means no endpoints controller and no pod to compare against:
            # ExternalName, or endpoints managed by hand.
            continue
        for svc_port in spec.get("ports") or []:
            if not isinstance(svc_port, dict) or not isinstance(svc_port.get("port"), int):
                continue
            inspected_ports += 1
            svc_proto = svc_port.get("protocol") or "TCP"
            how, target = resolve_target(svc_port)
            for (kind, wl_name), wl in sorted(workloads[comp].items()):
                tmpl = ((wl.get("spec") or {}).get("template") or {})
                labels = (tmpl.get("metadata") or {}).get("labels") or {}
                if not selects(selector, labels):
                    continue
                for container in (tmpl.get("spec") or {}).get("containers") or []:
                    cports = container_ports(container)
                    if how == "name":
                        candidates = [p for p in cports if p[0] == target]
                    else:
                        candidates = [p for p in cports if p[1] == target]
                    if not candidates:
                        # containerPorts are optional and purely informational; a chart that
                        # declares none is not a defect and must not be reported as one.
                        continue
                    if any(p[2] == svc_proto for p in candidates):
                        continue
                    got = ", ".join(sorted({p[2] for p in candidates}))
                    fail(
                        f"{comp}: Service {svc_name} port {svc_port.get('name')!r} is "
                        f"{svc_proto}, but {kind}/{wl_name} container "
                        f"{container.get('name')!r} declares the port it resolves to "
                        f"({target!r}) as {got}.\n"
                        f"      One of the two was changed and the other was not. Both "
                        f"default to TCP when omitted, so the protocol has to be declared in "
                        f"both places or they drift."
                    )

if inspected_ports == 0:
    measurement_failed([
        f"{len(renders)} charts rendered and not one selector-backed Service port was found.",
        "Every API in this repo is fronted by one, so this means the ports were not read,",
        "not that none exist.",
    ])

# -------------------------------------------------------------------------------------------
if findings:
    print(f"FAIL: {len(findings)} problem(s) with a UDP port declaration\n")
    for f in findings:
        print(f"  - {f}")
    print(
        "\nprotocol on a ServicePort and on a ContainerPort defaults to TCP, and the default is"
        "\nwhat produced this defect: a Service kube-proxy programs for TCP in front of a socket"
        "\nthat only ever accepts datagrams. Nothing reports it -- UDP sends no reply, and"
        "\ngnocchi's Stats.flush() logs each failed measure and carries on -- so the Service has"
        "\nendpoints, the pod is Ready, and no measure is ever delivered. Declare protocol: UDP"
        "\nin BOTH places. See issue #127."
    )
    sys.exit(1)

print(
    f"OK: all {checked_entries} registered UDP-only listener(s) carry protocol: UDP on the "
    f"Service port and on the containerPort, resolve to each other, and are probed by nothing "
    f"that opens a connection"
)
print(
    f"OK: {inspected_ports} selector-backed Service ports across {len(renders)} charts all "
    f"agree on protocol with the container port they resolve to"
)
