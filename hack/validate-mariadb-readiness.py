#!/usr/bin/env python3
"""mariadb's controller must be able to report a wedged reconcile loop, and the server's probe
must keep checking the cluster.

Why this gate exists. mariadb-controller runs mariadb_controller.py and nothing else: a loop
that lists the mariadb server pods, picks the first Ready one, and narrows the selector of the
master service -- the `mariadb` Service every OpenStack service in the namespace resolves to --
from "all server pods" to that single pod, so Galera takes writes on one primary. Every call it
makes is to the kubernetes API, so a loop wedged in one of them leaves the process alive, the
container Running, the Deployment Available and the StatefulSet beside it reporting a perfectly
healthy database, while the selector keeps whatever it last held: broad on a fresh install, so
writes land on several primaries at once, or pinned to a pod a failover has already replaced,
so every service gets connection refused from the one endpoint they all share.

The container shipped with no probe of any kind -- the no-probe half of issue #110, on the
component at the bottom of every dependency graph in this repo, and directly beside the most
carefully built probe here. So the loop now reports each completed pass to a heartbeat file
carrying the deadline for its next one, and readiness compares that deadline with the clock.

Three things have to hold together for that to mean anything, and each fails silently alone:

  * the probe must exist and read the heartbeat -- a probe over anything else is satisfied by a
    wedged controller. `pgrep -f mariadb_controller.py` would be the obvious shallow version:
    the process is exactly what stays alive through the failure this detects.
  * the loop must still refresh the heartbeat on every pass. A heartbeat written once at
    startup, or outside the loop, is a probe that passes forever.
  * the path must be the same in all three places that name it (the values env, the script's
    default, the probe's default). Any drift and the probe watches a file nothing writes, which
    fails closed on the component everything else waits for.

It also asserts the converse, as glance's gate does: no probe that can restart or suppress must
read the heartbeat. A stale heartbeat almost always means the apiserver is slow or unreachable,
which killing the one process whose job is to re-link the master service does not repair, and a
startupProbe suppresses readiness until it succeeds -- which would hide the very signal this
change adds.

Finally it asserts the server's own probes have not been weakened. That probe (bin/_health.sh.tpl)
is the best one in this repo and the easiest to lose by accident while editing the chart beside
it: its readiness checks wsrep state, not just a TCP connect, and its startupProbe carries the
630s budget measured in #97 against a start-to-ready of 81s. A regression there is invisible --
a node that cannot receive queries reports Ready, which is the same class of defect as the
missing probe above.

Renders the chart and inspects the probes as the kubelet would receive them, and parses the
shipped script rather than grepping it, because what matters is the object the kubelet acts on
and the code the interpreter runs.
"""
import ast
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
CHART = ROOT / "blueprints/mariadb/chart"

# The startup budget measured on native amd64 in #97: 81s start-to-ready, 2 of 60 startupProbe
# failures. The budget may grow; it may not shrink, because the kill it prevents is not
# recoverable -- a bootstrap killed halfway leaves a datadir that every restart then dies on.
STARTUP_BUDGET_SECONDS = 630
# The four wsrep variables that distinguish a node that can serve queries from a process that
# merely accepts connections. A readiness probe without them reports a node that is up and
# outside the cluster as Ready.
WSREP_CHECKS = (
    "wsrep_ready",
    "wsrep_connected",
    "wsrep_cluster_status",
    "wsrep_local_state_comment",
)


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def measurement_failed(msg):
    # Exit 2 and never 0. The gates here that skipped on a missing PyYAML had never once run in
    # CI: setup-python ships no PyYAML, so each printed `SKIP: PyYAML unavailable`, exited 0 and
    # the job went green -- a gate reporting success without measuring anything, which is the
    # same defect as a probe that cannot fail (#120). Anything that leaves this gate with
    # nothing to inspect is "not measured", not "fine".
    print(f"FAILED MEASUREMENT: {msg}")
    sys.exit(2)


if not CHART.is_dir():
    measurement_failed(f"no mariadb chart at {CHART} -- wrong path?")

try:
    import yaml
except ImportError:
    measurement_failed("PyYAML unavailable, so the rendered probes were never inspected")

values = yaml.safe_load((CHART / "values.yaml").read_text())
pod_env = (((values.get("pod") or {}).get("env") or {}).get("mariadb_controller")) or {}
heartbeat_path = pod_env.get("MARIADB_CONTROLLER_HEARTBEAT_PATH")
if not heartbeat_path:
    measurement_failed(
        "mariadb values.yaml no longer sets pod.env.mariadb_controller."
        "MARIADB_CONTROLLER_HEARTBEAT_PATH, so this gate has no path to compare the probe, "
        "the script and the container's environment against"
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
        fail(f"helm template failed: {out.stderr.strip()[:400]}")

docs = [d for d in yaml.safe_load_all(out.stdout) if d]
if not docs:
    measurement_failed("chart rendered 0 objects -- a gate over nothing passes everything")

deploys = [
    d for d in docs
    if d.get("kind") == "Deployment" and d["metadata"]["name"] == "mariadb-controller"
]
if not deploys:
    # Not a FAIL: with no controller Deployment there is no probe to be wrong. It is also not a
    # pass, because the component this gate is about is then absent from the render and nothing
    # below was measured.
    measurement_failed(
        "no mariadb-controller Deployment in the rendered chart, so the controller's probe was "
        "never inspected -- has manifests.deployment_controller been turned off?"
    )

controllers = [
    c for c in deploys[0]["spec"]["template"]["spec"]["containers"] if c["name"] == "controller"
]
if not controllers:
    measurement_failed(
        "the mariadb-controller Deployment has no container named controller: "
        f"{[c['name'] for c in deploys[0]['spec']['template']['spec']['containers']]}"
    )
controller = controllers[0]

env = {e["name"]: e.get("value") for e in controller.get("env") or []}
if env.get("MARIADB_CONTROLLER_HEARTBEAT_PATH") != heartbeat_path:
    fail(
        f"the controller container's MARIADB_CONTROLLER_HEARTBEAT_PATH is "
        f"{env.get('MARIADB_CONTROLLER_HEARTBEAT_PATH')!r}, not {heartbeat_path!r}.\n"
        f"      The loop writes the file this names and the probe reads it. If they name\n"
        f"      different files the probe watches one nothing writes, and the component every\n"
        f"      OpenStack service waits on is permanently unready."
    )

readiness = controller.get("readinessProbe")
if not readiness:
    fail(
        "the controller container has no readinessProbe.\n"
        "      Its reconcile loop blocks only on the kubernetes API, so a wedged loop keeps\n"
        "      the process alive and the pod Ready while the master service selector every\n"
        "      OpenStack service resolves to stops being maintained. Nothing else in the pod\n"
        "      can report that. See issue #110."
    )

probe_command = (readiness.get("exec") or {}).get("command") or []
probe_source = next((a for a in probe_command if "MARIADB_CONTROLLER_HEARTBEAT_PATH" in a), None)
if not probe_source:
    fail(
        f"the controller's readinessProbe does not read MARIADB_CONTROLLER_HEARTBEAT_PATH.\n"
        f"      It must fail when the reconcile loop stops completing passes. A probe over\n"
        f"      anything else -- a TCP port the controller does not open, or\n"
        f"      `pgrep -f mariadb_controller.py` -- is satisfied by exactly the wedged\n"
        f"      controller it is supposed to catch: the process stays alive throughout.\n"
        f"      got: {json.dumps(readiness)[:300]}"
    )


def environ_default(source, var):
    """The fallback in `os.environ.get(var, default)`, parsed rather than matched.

    Parsed because the probe is formatted for the reader -- the call is wrapped across lines in
    the template -- and a substring check would be satisfied or defeated by reflowing it.
    """
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or len(node.args) != 2:
            continue
        if not isinstance(node.args[0], ast.Constant) or node.args[0].value != var:
            continue
        if isinstance(node.args[1], ast.Constant):
            return node.args[1].value
    return None


probe_default = environ_default(probe_source, "MARIADB_CONTROLLER_HEARTBEAT_PATH")
if probe_default != heartbeat_path:
    fail(
        f"the controller's readinessProbe falls back to {probe_default!r}, not "
        f"{heartbeat_path!r}.\n"
        f"      The fallback is what the probe reads if the environment variable is ever\n"
        f"      removed or overridden away, so it has to name the same file the script\n"
        f"      defaults to writing. Drift here fails closed on the component at the bottom\n"
        f"      of every dependency graph in this repo."
    )

for kind in ("livenessProbe", "startupProbe"):
    probe = controller.get(kind)
    if probe and heartbeat_path in json.dumps(probe):
        fail(
            f"the controller's {kind} reads {heartbeat_path}.\n"
            f"      A stale heartbeat almost always means the apiserver is slow or\n"
            f"      unreachable, and killing the one process whose job is to re-link the\n"
            f"      master service the instant it answers again repairs nothing -- on a\n"
            f"      cluster-wide blip it restarts this container everywhere at once. A\n"
            f"      startupProbe additionally suppresses readiness until it succeeds, which\n"
            f"      hides the signal this check is about. Report it in readiness."
        )

# The other half of the same failure mode: a probe that reads a heartbeat nothing refreshes
# passes forever. Only a write INSIDE the loop body makes the file mean "a pass completed", so
# that is what is asserted -- on the rendered script, because the template is not Python.
configmaps = [
    d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "mariadb-bin"
]
if not configmaps:
    measurement_failed("no mariadb-bin ConfigMap in the rendered chart")
scripts = configmaps[0].get("data") or {}
script = scripts.get("mariadb_controller.py")
if not script:
    measurement_failed(
        "the mariadb-bin ConfigMap ships no mariadb_controller.py, so the loop the probe "
        "reports on was never inspected"
    )

script_default = environ_default(script, "MARIADB_CONTROLLER_HEARTBEAT_PATH")
if script_default is None:
    script_default = next(
        (
            n.args[1].value
            for n in ast.walk(ast.parse(script))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "getenv"
            and len(n.args) == 2
            and isinstance(n.args[0], ast.Constant)
            and n.args[0].value == "MARIADB_CONTROLLER_HEARTBEAT_PATH"
            and isinstance(n.args[1], ast.Constant)
        ),
        None,
    )
if script_default != heartbeat_path:
    fail(
        f"mariadb_controller.py defaults MARIADB_CONTROLLER_HEARTBEAT_PATH to "
        f"{script_default!r}, not {heartbeat_path!r}.\n"
        f"      The script and the probe must name the same file with and without the\n"
        f"      environment variable set."
    )

tree = ast.parse(script)
writers = {
    node.name
    for node in ast.walk(tree)
    if isinstance(node, ast.FunctionDef)
    and any(
        isinstance(n, ast.Name) and n.id == "MARIADB_CONTROLLER_HEARTBEAT_PATH"
        for n in ast.walk(node)
    )
}
if not writers:
    fail(
        "no function in mariadb_controller.py writes MARIADB_CONTROLLER_HEARTBEAT_PATH, so the "
        "file the readiness probe reads is never produced"
    )

loops = [n for n in ast.walk(tree) if isinstance(n, (ast.While, ast.For))]
refreshed_in_loop = any(
    isinstance(call.func, ast.Name) and call.func.id in writers
    for loop in loops
    for call in ast.walk(loop)
    if isinstance(call, ast.Call)
)
if not refreshed_in_loop:
    fail(
        f"mariadb_controller.py never calls {' or '.join(sorted(writers))} from inside its\n"
        f"      reconcile loop.\n"
        f"      The heartbeat means 'a pass completed' only if every pass writes it. Written\n"
        f"      once at startup, or outside the loop, it is a readiness probe that passes\n"
        f"      forever -- which is the state this chart was already in, with no probe at all."
    )

# Nothing above protects the probe that was already right. It is the most carefully built one in
# this repo, it is in the same chart, and it is the one a careless edit here would take with it.
servers = [d for d in docs if d.get("kind") == "StatefulSet" and d["metadata"]["name"] == "mariadb-server"]
if not servers:
    measurement_failed(
        "no mariadb-server StatefulSet in the rendered chart, so the server's probes were "
        "never inspected"
    )
mariadbs = [
    c for c in servers[0]["spec"]["template"]["spec"]["containers"] if c["name"] == "mariadb"
]
if not mariadbs:
    measurement_failed(
        "the mariadb-server StatefulSet has no container named mariadb: "
        f"{[c['name'] for c in servers[0]['spec']['template']['spec']['containers']]}"
    )
server = mariadbs[0]

server_readiness = server.get("readinessProbe")
server_command = (server_readiness or {}).get("exec", {}).get("command") or []
if server_command[:3] != ["/tmp/health.sh", "-t", "readiness"]:
    fail(
        f"the mariadb server's readinessProbe no longer runs `/tmp/health.sh -t readiness`.\n"
        f"      That script is the only thing here that distinguishes a node which can serve\n"
        f"      queries from a process which merely accepts connections. Replacing it with a\n"
        f"      TCP connect or a `select 1` reports a node outside the cluster as Ready, and\n"
        f"      every OpenStack service builds on that.\n"
        f"      got: {json.dumps(server_readiness)[:300]}"
    )

startup = server.get("startupProbe")
if not startup:
    fail(
        "the mariadb server has no startupProbe.\n"
        "      It is what stops liveness from killing a first bootstrap that has not yet\n"
        "      produced a mariadbd pid. The kill is not recoverable: every restart then dies\n"
        "      on the half-written datadir (`Unknown storage engine 'Aria'`). See issue #97."
    )
budget = startup.get("initialDelaySeconds", 0) + (
    startup.get("periodSeconds", 10) * startup.get("failureThreshold", 3)
)
if budget < STARTUP_BUDGET_SECONDS:
    fail(
        f"the mariadb server's startupProbe allows {budget}s, below the measured "
        f"{STARTUP_BUDGET_SECONDS}s.\n"
        f"      Start-to-ready was measured at 81s on native amd64 with 2 of 60 startupProbe\n"
        f"      failures, and the budget exists for the slow case, not the measured one. The\n"
        f"      bootstrap this protects cannot survive being killed halfway (#97), so this\n"
        f"      budget may grow and may not shrink."
    )

health = scripts.get("health.sh")
if not health:
    measurement_failed("the mariadb-bin ConfigMap ships no health.sh")
readiness_body = health.split("check_readiness ()", 1)[-1].split("check_liveness ()", 1)[0]
missing = [v for v in WSREP_CHECKS if v not in readiness_body]
if missing:
    fail(
        f"health.sh's readiness check no longer inspects {', '.join(missing)}.\n"
        f"      Each one reports a distinct way a node is up and unable to serve: not ready\n"
        f"      for queries, disconnected from the cluster, in a non-primary partition, or\n"
        f"      still syncing. Dropping any of them makes such a node report Ready."
    )

print(f"OK: the controller's readinessProbe reads {heartbeat_path}, which "
      f"{' / '.join(sorted(writers))} refreshes on every reconcile pass")
print("OK: no restarting or suppressing probe on the controller reads the heartbeat")
print(f"OK: the server's readiness still runs health.sh, which checks all four wsrep variables, "
      f"and its startupProbe still allows {budget}s")
