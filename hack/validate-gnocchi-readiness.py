#!/usr/bin/env python3
"""Gnocchi's probes must check its backends; nothing that restarts the container may.

Why this gate exists. Gnocchi is the component in this repository whose failure is hardest to
see, and the reason is in the code shipped in the Kolla images the chart installs (gnocchi
4.7.0), not in anybody's opinion:

  * gnocchi/cli/metricd.py, MetricProcessBase.run(), wraps each pass in
    `except Exception: LOG.error(...)` and loops forever. metricd is the only thing that
    aggregates measures, so a metricd that cannot reach its indexer, its measure store or its
    coordinator keeps running, keeps being counted Ready, and aggregates nothing -- while
    measures are still accepted. No API call ever returns an error; a query just quietly
    returns fewer points.
  * gnocchi/statsd.py, Stats.flush(), wraps each individual measure in
    `except Exception as e: LOG.error("Unable to add measure %s: %s", ...)`. statsd is a UDP
    receiver, so a broken write path means every datagram is accepted and dropped, with no
    reply for the sender to inspect.

Both containers had NO probe of any kind, which is why this is the same defect as keystone's
fernet keys (#99) and glance's image store (#110), only quieter: a Ready that the dependency
gate, the Composition and the umbrella all read as "safe to build on", and that means nothing.

That also makes a revert of these probes completely invisible. Nothing crashes, nothing turns
red, no dashboard changes; metrics simply stop being aggregated. A gate is the only thing that
would notice.

It asserts the converse too, for every probe that can restart the container: liveness and
startup must NOT check a backend. A database, a ceph pool or a memcached that is down lives in
another pod and is not repaired by restarting this one, so such a probe converts a visible
unready pod into a CrashLoop and repairs nothing. startupProbe is named explicitly because it
is the trap: it suppresses both liveness and readiness until it succeeds, so a backend check
there removes the very signal this gate is about.

Two uwsgi-specific assertions. The API is served by `uwsgi --http`, whose HTTP router is a
separate process from the workers and accepts the TCP connection itself:

  * gnocchi-api's readiness must not be a bare tcpSocket, which is what it was. That probe
    passes while every worker is wedged.
  * no probe in this chart may target port 1717, uwsgi's stats endpoint. It reports on the
    MASTER and answers 200 with every worker stuck -- the mistake heat, neutron and placement
    currently make, verified, and not one to copy here.

Finally it asserts that api-paste.ini still names the composite gnocchi actually asks for.
That is not probe decoration: with the previous values, `uwsgi --need-app` in
quay.io/openstack.kolla/gnocchi-api:2026.1-ubuntu-noble ended in `LookupError: No section
'gnocchi+keystone' ... *** no app loaded. GAME OVER ***`, so the API could not serve one
request and every probe above would be moot.

Renders the chart and inspects the probes as the kubelet would receive them, rather than
grepping the templates, because what matters is the object the kubelet acts on.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
CHART = ROOT / "blueprints/gnocchi/chart"

# Where the templates mount the shipped checker, and the ConfigMap key that supplies it.
PROBE_PATH = "/tmp/gnocchi-probe.py"
PROBE_KEY = "gnocchi-probe.py"
BIN_CONFIGMAP = "gnocchi-bin"

# The flags that make a real round trip to a real backend. These are the ones a restarting
# probe must never carry.
BACKEND_FLAGS = ("--indexer", "--storage", "--coordination")

# uwsgi's stats endpoint. It reports on the master, so it is satisfied by a process whose every
# worker is wedged.
UWSGI_STATS_PORT = 1717

# container -> (rendered workload kind, workload name, flags its readiness must carry).
#
# Which flags, and why each: metricd's MetricProcessBase._configure() builds a coordinator, a
# storage driver, an incoming driver and an indexer, in that order, and cannot proceed without
# any of them. statsd's Stats.__init__ and Stats.flush() use only the incoming driver and the
# indexer -- it never builds a coordinator, so requiring one of statsd would report a failure
# it does not have. The API needs all three (gnocchi/rest/app.py GnocchiHook.on_route builds
# all four drivers for EVERY request) plus --http, because the backend checks run in a separate
# process and say nothing about whether a uwsgi worker can answer.
EXPECTED = {
    "gnocchi-api": ("Deployment", "gnocchi-api",
                    ("--http", "--indexer", "--storage", "--coordination")),
    "gnocchi-metricd": ("DaemonSet", "gnocchi-metricd",
                        ("--indexer", "--storage", "--coordination")),
    "gnocchi-statsd": ("DaemonSet", "gnocchi-statsd",
                       ("--indexer", "--storage")),
}

failures = []


def fail(msg):
    failures.append(msg)


def measurement_failed(msg):
    print(f"FAILED MEASUREMENT: {msg}")
    sys.exit(2)


if not CHART.is_dir():
    measurement_failed(f"no gnocchi chart at {CHART} -- wrong path?")

# FAILED MEASUREMENT and not SKIP, deliberately. The gates here that skipped on a missing
# PyYAML had never once run in CI: setup-python ships no PyYAML, so each printed
# `SKIP: PyYAML unavailable`, exited 0, and the job went green -- a gate reporting success
# without measuring anything, which is the same bug as a probe that cannot fail (#120). The
# workflow now installs it; this exits 2 so that if it stops doing so, the answer is "not
# measured" and not "fine".
try:
    import yaml
except ImportError:
    measurement_failed("PyYAML unavailable, so the rendered probes were never inspected")

values = yaml.safe_load((CHART / "values.yaml").read_text()) or {}
conf = values.get("conf") or {}
health_path = (
    ((values.get("endpoints") or {}).get("metric") or {}).get("path", {}).get("healthcheck")
)
auth_mode = ((conf.get("gnocchi") or {}).get("api") or {}).get("auth_mode")
if not health_path or not auth_mode:
    measurement_failed(
        "gnocchi values.yaml no longer declares endpoints.metric.path.healthcheck and "
        "conf.gnocchi.api.auth_mode, so this gate has nothing to compare the rendered probes "
        "and paste pipeline against"
    )

with tempfile.TemporaryDirectory() as tmp:
    work = Path(tmp) / "chart"
    shutil.copytree(CHART, work)
    cy = work / "Chart.yaml"
    cy.write_text(cy.read_text().replace("CHART_VERSION", "0.0.0-gate"))
    toolkit = ROOT / "charts/helm-toolkit"
    if toolkit.is_dir():
        (work / "charts").mkdir(exist_ok=True)
        shutil.copytree(toolkit, work / "charts/helm-toolkit", dirs_exist_ok=True)
    # kubeVersion is constrained, so an unset --kube-version renders zero objects and the gate
    # would pass on an empty document set rather than on a correct one.
    out = subprocess.run(
        ["helm", "template", "gate", str(work), "--kube-version", "1.36.0"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        measurement_failed(f"helm template failed: {out.stderr.strip()[:400]}")

docs = [d for d in yaml.safe_load_all(out.stdout) if d]
if not docs:
    measurement_failed("chart rendered 0 objects -- a gate over nothing passes everything")

workloads = {
    (d["kind"], d["metadata"]["name"]): d
    for d in docs
    if d.get("kind") in ("Deployment", "DaemonSet", "StatefulSet")
}
if not workloads:
    measurement_failed("no Deployment or DaemonSet rendered, so no probe was inspected")


def container(kind, name, container_name):
    doc = workloads.get((kind, name))
    if doc is None:
        return None
    pod = doc["spec"]["template"]["spec"]
    for c in pod["containers"]:
        if c["name"] == container_name:
            return c
    return None


def probe_command(probe):
    return list(((probe or {}).get("exec") or {}).get("command") or [])


# ---------------------------------------------------------------------------------------------
# 1. Every long-lived gnocchi container's readiness must make a real round trip to every backend
#    it cannot work without.
# ---------------------------------------------------------------------------------------------
checked = 0
for container_name, (kind, workload, required) in EXPECTED.items():
    c = container(kind, workload, container_name)
    if c is None:
        fail(
            f"no {container_name} container in the rendered {kind}/{workload}. If it was "
            f"renamed, rename it here too; if it was removed, this gate is measuring nothing."
        )
        continue
    checked += 1

    readiness = c.get("readinessProbe")
    if not readiness:
        fail(
            f"{container_name} has no readinessProbe.\n"
            f"      This is where it started: metricd and statsd had none at all, and both "
            f"swallow every backend error and keep running, so a live process meant nothing."
        )
        continue

    if "tcpSocket" in readiness:
        fail(
            f"{container_name}'s readinessProbe is a tcpSocket.\n"
            f"      For the API this is the exact probe that was there before: `uwsgi --http` "
            f"accepts the connection in its HTTP router, not in a worker, so the probe passes "
            f"with every worker wedged. A tcpSocket cannot check a backend at all.\n"
            f"      got: {json.dumps(readiness)[:300]}"
        )
        continue

    command = probe_command(readiness)
    if PROBE_PATH not in command:
        fail(
            f"{container_name}'s readinessProbe does not run {PROBE_PATH}.\n"
            f"      got: {json.dumps(readiness)[:300]}"
        )
        continue

    missing = [f for f in required if f not in command]
    if missing:
        fail(
            f"{container_name}'s readinessProbe no longer asks for {', '.join(missing)}.\n"
            f"      Each flag is a backend the container cannot do its job without, and each "
            f"can be unreachable while the process is perfectly alive -- which is the whole "
            f"failure mode here.\n"
            f"      got: {' '.join(command)}"
        )

    # The script's own budget must expire before the kubelet's, or an unreachable backend is
    # reported as a killed check with no message instead of a named failure.
    if "--deadline" in command:
        try:
            deadline = int(command[command.index("--deadline") + 1])
        except (IndexError, ValueError):
            fail(f"{container_name}'s readinessProbe passes a non-numeric --deadline")
        else:
            timeout = readiness.get("timeoutSeconds", 1)
            if deadline >= timeout:
                fail(
                    f"{container_name}'s readinessProbe has --deadline {deadline} and "
                    f"timeoutSeconds {timeout}.\n"
                    f"      The script must fail itself first, so the kubelet records why the "
                    f"probe failed instead of killing the check halfway through."
                )
    else:
        fail(
            f"{container_name}'s readinessProbe passes no --deadline, so a backend that hangs "
            f"leaves the check to be killed with no message."
        )

    # Detection is worthless if the checker is not there. A missing mount fails the probe
    # forever, for the wrong reason, with a message about a missing file.
    mounts = [
        m for m in (c.get("volumeMounts") or [])
        if m.get("mountPath") == PROBE_PATH and m.get("subPath") == PROBE_KEY
    ]
    if not mounts:
        fail(
            f"{container_name} runs {PROBE_PATH} but does not mount it "
            f"(subPath {PROBE_KEY} from the {BIN_CONFIGMAP} ConfigMap)."
        )

if checked == 0:
    measurement_failed(
        "found none of the gnocchi containers this gate is about, so nothing was inspected"
    )

# ---------------------------------------------------------------------------------------------
# 2. No probe that restarts the container may depend on a backend, and none may use a
#    startupProbe at all.
# ---------------------------------------------------------------------------------------------
for container_name, (kind, workload, _required) in EXPECTED.items():
    c = container(kind, workload, container_name)
    if c is None:
        continue
    for probe_kind in ("livenessProbe", "startupProbe"):
        probe = c.get(probe_kind)
        if not probe:
            continue
        used = [f for f in BACKEND_FLAGS if f in probe_command(probe)]
        if used:
            fail(
                f"{container_name}'s {probe_kind} checks {', '.join(used)}.\n"
                f"      None of those is repaired by restarting this container -- the "
                f"database, the ceph pool and the memcached all live in other pods -- so this "
                f"turns a visible unready pod into a CrashLoop and fixes nothing. Check "
                f"backends in readiness."
            )
    if c.get("startupProbe"):
        fail(
            f"{container_name} has a startupProbe.\n"
            f"      A startupProbe suppresses BOTH liveness and readiness until it succeeds, "
            f"so it hides the readiness signal this whole gate is about."
        )

# ---------------------------------------------------------------------------------------------
# 3. The API's liveness must traverse a uwsgi worker, and nothing may point at uwsgi's stats
#    port.
# ---------------------------------------------------------------------------------------------
api = container("Deployment", "gnocchi-api", "gnocchi-api")
if api is not None:
    liveness = api.get("livenessProbe")
    if not liveness:
        fail(
            "gnocchi-api has no livenessProbe. A wedged uwsgi worker set IS repaired by a "
            "restart, which is what liveness is for here."
        )
    elif "httpGet" not in liveness:
        fail(
            f"gnocchi-api's livenessProbe is not an httpGet.\n"
            f"      It has to be a real request: uwsgi's HTTP router accepts the TCP "
            f"connection itself, so only a response proves a worker ran Python.\n"
            f"      got: {json.dumps(liveness)[:300]}"
        )
    elif liveness["httpGet"].get("path") != health_path:
        fail(
            f"gnocchi-api's livenessProbe fetches {liveness['httpGet'].get('path')!r}, not "
            f"{health_path!r} from endpoints.metric.path.healthcheck.\n"
            f"      /healthcheck is oslo.middleware's app, mounted outside pecan, so it "
            f"restarts on a wedged worker without CrashLooping on a backend outage."
        )

for (kind, name), doc in workloads.items():
    for c in doc["spec"]["template"]["spec"]["containers"]:
        for probe_kind in ("livenessProbe", "readinessProbe", "startupProbe"):
            probe = c.get(probe_kind) or {}
            for scheme in ("httpGet", "tcpSocket"):
                port = (probe.get(scheme) or {}).get("port")
                if port in (UWSGI_STATS_PORT, str(UWSGI_STATS_PORT)):
                    fail(
                        f"{kind}/{name} container {c['name']} has a {probe_kind} on port "
                        f"{UWSGI_STATS_PORT}.\n"
                        f"      That is uwsgi's own stats endpoint. It reports on the MASTER "
                        f"and answers 200 with every worker wedged, so it cannot fail for the "
                        f"reason a probe exists. heat, neutron and placement do this; gnocchi "
                        f"must not."
                    )

# ---------------------------------------------------------------------------------------------
# 4. The shipped checker must exist, and must refuse to pass when asked to check nothing.
# ---------------------------------------------------------------------------------------------
script = None
for d in docs:
    if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == BIN_CONFIGMAP:
        script = (d.get("data") or {}).get(PROBE_KEY)
if not script:
    fail(
        f"the {BIN_CONFIGMAP} ConfigMap carries no {PROBE_KEY}, so every probe above mounts "
        f"nothing and fails forever for the wrong reason."
    )
else:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / PROBE_KEY
        path.write_text(script)
        # Run it, rather than grepping it. The point is behaviour: a checker invoked with no
        # checks must exit non-zero. Everything up to that branch is stdlib, so this runs
        # anywhere -- it never reaches sqlalchemy, tooz or rados.
        #
        # --config-file matters, and the first version of this gate got it wrong. Without it
        # the script falls back to /etc/gnocchi/gnocchi.conf, which does not exist on a CI
        # runner, so a script whose zero-check guard had been DELETED still exited non-zero --
        # for the wrong reason -- and this check passed. Handing it a readable config file
        # leaves the guard as the only thing that can fail. Confirmed by deleting the guard and
        # watching this flip from pass to fail.
        config = Path(tmp) / "gnocchi.conf"
        config.write_text("[DEFAULT]\n[storage]\ndriver = file\n")
        run = subprocess.run(
            [sys.executable, str(path), "--config-file", str(config)],
            capture_output=True, text=True, timeout=60,
        )
        if run.returncode == 0:
            fail(
                f"{PROBE_KEY} exits 0 when asked to perform no checks.\n"
                f"      A probe that measures nothing must not report Ready; that is the "
                f"defect this whole gate is about, one level down.\n"
                f"      output: {(run.stdout + run.stderr).strip()[:300]}"
            )
        for flag in BACKEND_FLAGS + ("--http",):
            if flag not in script:
                fail(f"{PROBE_KEY} no longer implements {flag}, which a probe above requests.")

# ---------------------------------------------------------------------------------------------
# 5. The rendered api-paste.ini must name the composite gnocchi asks for, and must still mount
#    the healthcheck app the probes use. Without this the API loads no app at all and nothing
#    above can run.
# ---------------------------------------------------------------------------------------------
import base64  # noqa: E402  -- only needed here

paste = None
for d in docs:
    if d.get("kind") == "Secret" and d["metadata"]["name"] == "gnocchi-etc":
        raw = (d.get("data") or {}).get("api-paste.ini")
        if raw:
            paste = base64.b64decode(raw).decode()
if paste is None:
    fail("the gnocchi-etc Secret carries no api-paste.ini, so conf.paste was not rendered")
else:
    composite = f"[composite:gnocchi+{auth_mode}]"
    if composite not in paste:
        fail(
            f"the rendered api-paste.ini has no {composite}.\n"
            f"      gnocchi/rest/app.py load_app() asks paste for \"gnocchi+\" + "
            f"conf.api.auth_mode, so without it the API loads no app at all: measured in "
            f"quay.io/openstack.kolla/gnocchi-api:2026.1-ubuntu-noble as\n"
            f"      LookupError: No section 'gnocchi+{auth_mode}' ... *** no app loaded. "
            f"GAME OVER ***"
        )
    if f"{health_path} = healthcheck" not in paste:
        fail(
            f"the rendered api-paste.ini no longer maps {health_path} to the healthcheck app, "
            f"which both of gnocchi-api's probes fetch."
        )
    for controller in ("gnocchi.rest.api.VersionsController", "gnocchi.rest.api.V1Controller"):
        if controller not in paste:
            fail(
                f"the rendered api-paste.ini no longer names {controller}.\n"
                f"      gnocchi/rest/__init__.py is empty in gnocchi 4.7; the controllers live "
                f"in gnocchi.rest.api, and paste resolves the root at app load."
            )

if failures:
    print(f"FAIL: {len(failures)} problem(s) with gnocchi's probes")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print(f"OK: readiness on all {checked} long-lived gnocchi containers makes a real round trip "
      f"to every backend it needs")
print("OK: no restarting probe checks a backend, none is a startupProbe, and none points at "
      f"uwsgi's stats port {UWSGI_STATS_PORT}")
print(f"OK: {PROBE_KEY} refuses to pass when asked to check nothing")
print(f"OK: api-paste.ini still names composite gnocchi+{auth_mode} and maps {health_path}")
