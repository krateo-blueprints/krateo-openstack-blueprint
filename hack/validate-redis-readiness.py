#!/usr/bin/env python3
"""Redis's readiness must prove it can serve; its liveness must only prove it is alive.

    hack/validate-redis-readiness.py [repo-root]

Why redis needs its own gate even though memcached does not. memcached's `tcpSocket 11211` was
classified MEANINGFUL in the probe census because no realistic state was found in which
memcached accepts TCP and cannot serve get/set. Redis has four, and they were measured in
docker.io/library/redis:7.2.17 under --platform linux/amd64, which is the image this chart
pins:

    state                             TCP   PING reply    redis-cli  SET reply     correct
                                                          exit code                verdict
    --------------------------------  ----  ------------  ---------  ------------  --------
    healthy                           ok    PONG          0          OK            ready
    loading a 311MB RDB from disk      ok    -LOADING      0          -LOADING      NOT ready
    BGSAVE failed, writes stopped      ok    -MISCONF      0          -MISCONF      NOT ready
    replica, master link down          ok    -MASTERDOWN   0          -READONLY     NOT ready
    maxmemory reached, noeviction      ok    PONG          0          -OOM          NOT ready
    event loop wedged (DEBUG SLEEP)    ok    no reply      124        no reply      NOT ready
    process gone                       no    n/a           1          n/a           NOT ready

Three conclusions, and this gate enforces each of them.

 1. A tcpSocket passes in EVERY broken row, including the wedged one -- the kernel completes
    the handshake from the listen backlog. Upstream's chart shipped exactly that probe.

 2. `redis-cli ping` is not enough either. redis-cli EXITS 0 ON AN ERROR REPLY, so a probe
    whose verdict is redis-cli's exit status reports Ready while every command is refused; and
    the maxmemory row shows PING answering PONG even when read as text, while redis rejects
    every write. (Redis denies PING on LOADING, MISCONF and MASTERDOWN deliberately, for load
    balancers. It does not deny it on OOM.)

 3. So readiness must make the round trip the consumer makes. zaqar's message store IS this
    redis, and posting a message is a WRITE, so readiness does SET and then GET and compares
    the REPLY TEXT.

The converse half matters as much, and is the half a well-meaning edit breaks. Liveness
restarts the container, and a restart repairs only two of those rows: wedged and gone. It
repairs neither a read-only /data, nor a reached maxmemory, nor a down master link, and on a
slow RDB load it makes things strictly worse by starting the load again. So liveness must be
the WEAKER probe: it asks only whether redis replied at all, which is exactly what redis-cli's
exit status means -- useless for readiness, correct here.

How it checks. It renders the chart and RUNS the rendered probe scripts against a stub
`redis-cli` that reproduces each row of the table, then asserts the verdicts. Behaviour, not
grep: an edit that deletes the reply-text comparison, or that turns liveness into a write
probe, changes an outcome here rather than merely changing some text. `timeout` is stubbed
alongside it so the whole check is hermetic -- it needs nothing on PATH but /bin/sh, and the
wedged row is modelled by the stub exiting 124, which is what the real timeout(1) does.

Refuses to pass on zero input. A missing chart, a failed render, no Deployment, no Service, no
probe, or a missing PyYAML all exit 2. Four gates in this repo printed "SKIP: PyYAML
unavailable" and exited 0 for weeks (#120): a gate that reports success without measuring
anything is the same bug as a probe that cannot fail.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
CHART = ROOT / "blueprints/redis/chart"
TOOLKIT = ROOT / "charts/helm-toolkit"
ZAQAR_VALUES = ROOT / "blueprints/zaqar/chart/values.yaml"

CONTAINER = "redis"
WORKLOAD = ("Deployment", "redis")

TIMEOUT_CALL = re.compile(r"\btimeout\s+([0-9]+)\b")


def measurement_failed(msg):
    print(f"FAILED MEASUREMENT: {msg}")
    sys.exit(2)


try:
    import yaml
except ImportError:
    measurement_failed("PyYAML unavailable, so the rendered probes were never inspected")

if not CHART.is_dir():
    measurement_failed(f"no redis chart at {CHART} -- wrong path?")
if not TOOLKIT.is_dir():
    measurement_failed(f"no helm-toolkit at {TOOLKIT}; the chart cannot render without it")
if not ZAQAR_VALUES.is_file():
    measurement_failed(
        f"no zaqar values at {ZAQAR_VALUES}; this gate cross-checks the Service name redis "
        f"renders against the host zaqar's message store resolves to, and cannot do that "
        f"against one side alone"
    )

values = yaml.safe_load((CHART / "values.yaml").read_text()) or {}
try:
    probe_opts = values["pod"]["probes"]["redis"]["redis"]
    network_port = values["network"]["port"]
except (KeyError, TypeError):
    measurement_failed(
        "redis values.yaml no longer declares pod.probes.redis.redis and network.port, so "
        "this gate has nothing to compare the rendered probes against"
    )

# ---------------------------------------------------------------------------------------------
# Render.
# ---------------------------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    work = Path(tmp) / "chart"
    shutil.copytree(CHART, work)
    cy = work / "Chart.yaml"
    cy.write_text(cy.read_text().replace("CHART_VERSION", "0.0.0-gate"))
    (work / "charts").mkdir(exist_ok=True)
    shutil.copytree(TOOLKIT, work / "charts/helm-toolkit", dirs_exist_ok=True)
    out = subprocess.run(
        # Without --kube-version a kubeVersion-constrained chart renders nothing, and a gate
        # over an empty document set passes everything.
        ["helm", "template", "gate", str(work), "--kube-version", "1.36.0"],
        capture_output=True, text=True,
    )
if out.returncode != 0:
    tail = out.stderr.strip()[:400] if out.stderr.strip() else "no stderr"
    measurement_failed(f"helm template of the redis chart failed: {tail}")

try:
    docs = [d for d in yaml.safe_load_all(out.stdout) if d]
except yaml.YAMLError as e:
    measurement_failed(f"the rendered redis chart does not parse as YAML: {str(e)[:200]}")
if not docs:
    measurement_failed("the redis chart rendered 0 objects -- a gate over nothing passes everything")

workload = None
for d in docs:
    if (d.get("kind"), d.get("metadata", {}).get("name")) == WORKLOAD:
        workload = d
if workload is None:
    measurement_failed(
        f"no {WORKLOAD[0]}/{WORKLOAD[1]} in the rendered chart. If the workload was renamed, "
        f"rename it here too; if manifests.deployment was turned off, no probe exists to check"
    )

container = None
for c in workload["spec"]["template"]["spec"]["containers"]:
    if c["name"] == CONTAINER:
        container = c
if container is None:
    measurement_failed(
        f"no {CONTAINER!r} container in {WORKLOAD[0]}/{WORKLOAD[1]}, so no probe was inspected"
    )

services = {
    d["metadata"]["name"]: d
    for d in docs
    if d.get("kind") == "Service"
}
if not services:
    measurement_failed(
        "the redis chart renders no Service at all. Nothing could reach this redis, and the "
        "Service-name cross-check below would have nothing to compare"
    )

failures = []


def fail(msg):
    failures.append(msg)


# ---------------------------------------------------------------------------------------------
# 1. Shape: both probes exist, both are exec, neither is a tcpSocket, and there is no
#    startupProbe anywhere in the chart.
# ---------------------------------------------------------------------------------------------
scripts = {}
for kind in ("readinessProbe", "livenessProbe"):
    probe = container.get(kind)
    if not probe:
        fail(
            f"the {CONTAINER} container has no {kind}. Readiness is what reports a redis that "
            f"is up and cannot serve; liveness is what restarts one that is wedged. Neither "
            f"substitutes for the other."
        )
        continue
    if "tcpSocket" in probe:
        fail(
            f"the {CONTAINER} container's {kind} is a tcpSocket -- the probe upstream shipped. "
            f"It passes in every broken state in this gate's table, including a wedged event "
            f"loop, because the kernel completes the handshake from the listen backlog."
        )
        continue
    if "httpGet" in probe:
        fail(f"the {CONTAINER} container's {kind} is an httpGet; redis speaks RESP, not HTTP.")
        continue
    command = ((probe.get("exec") or {}).get("command") or [])
    if len(command) < 3 or command[0] != "/bin/sh" or command[1] != "-c":
        fail(
            f"the {CONTAINER} container's {kind} is not a `/bin/sh -c <script>` exec probe, so "
            f"this gate cannot run it.\n      got: {command}"
        )
        continue
    scripts[kind] = (command[2], probe)

for d in docs:
    if d.get("kind") not in ("Deployment", "DaemonSet", "StatefulSet", "Pod"):
        continue
    pod = d["spec"] if d["kind"] == "Pod" else d["spec"]["template"]["spec"]
    for c in pod.get("containers") or []:
        if c.get("startupProbe"):
            fail(
                f"{d['kind']}/{d['metadata']['name']} container {c['name']} has a "
                f"startupProbe. A startupProbe suppresses BOTH liveness and readiness until it "
                f"succeeds, so it hides the readiness signal this gate is about."
            )

if len(scripts) != 2:
    if failures:
        print(f"FAIL: {len(failures)} problem(s) with redis's probes")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    measurement_failed("neither probe could be read, so nothing was measured")

# ---------------------------------------------------------------------------------------------
# 2. Budget: each probe's own timeouts must expire before the kubelet's, so an unreachable
#    server fails the probe with a message the kubelet records rather than being killed
#    mid-call. redis-cli 7.2 has no timeout option of its own, so the script must use
#    timeout(1) -- a script with none has no budget at all.
# ---------------------------------------------------------------------------------------------
for kind, (script, probe) in sorted(scripts.items()):
    budgets = [int(n) for n in TIMEOUT_CALL.findall(script)]
    if not budgets:
        fail(
            f"the {kind} script calls no `timeout`, so a redis that accepts the connection and "
            f"never answers leaves the check to be killed by the kubelet with no message. "
            f"redis-cli 7.2 has no connect or read timeout flag, so timeout(1) is the only "
            f"budget available."
        )
        continue
    spent = sum(budgets)
    allowed = probe.get("timeoutSeconds")
    if not isinstance(allowed, int):
        fail(f"the {kind} declares no integer timeoutSeconds, so its budget cannot be judged.")
        continue
    if spent >= allowed:
        fail(
            f"the {kind} can spend {spent}s ({' + '.join(str(b) for b in budgets)}) inside a "
            f"timeoutSeconds of {allowed}. The script must fail itself first, or the kubelet "
            f"kills it halfway and records no reason."
        )

# ---------------------------------------------------------------------------------------------
# 3. The port the probes reach must be the port the Service publishes, and the Service's name
#    must be the host zaqar's message store resolves to. Three places name this server --
#    the probe, the Service, and zaqar's endpoint -- and a probe that passes against a port
#    nobody consumes measures nothing.
# ---------------------------------------------------------------------------------------------
zaqar = yaml.safe_load(ZAQAR_VALUES.read_text()) or {}
endpoint = ((zaqar.get("endpoints") or {}).get("message_store") or {})
zaqar_host = ((endpoint.get("hosts") or {}).get("default"))
zaqar_port = (((endpoint.get("port") or {}).get("redis") or {}).get("default"))
if not zaqar_host or not zaqar_port:
    measurement_failed(
        "zaqar declares no endpoints.message_store.hosts.default / .port.redis.default, so "
        "the Service this chart renders cannot be checked against the host zaqar dials. Either "
        "zaqar stopped using this component or the endpoint was renamed; both need a look, and "
        "neither is a pass"
    )

if zaqar_host not in services:
    fail(
        f"zaqar's message store resolves to host {zaqar_host!r}, and this chart renders no "
        f"Service by that name (it renders {sorted(services)}). kubernetes-entrypoint cannot "
        f"tell \"not ready yet\" from \"will never exist\", so zaqar would block on it forever "
        f"with no error -- the shape of #115."
    )
else:
    ports = [p.get("port") for p in (services[zaqar_host]["spec"].get("ports") or [])]
    if zaqar_port not in ports:
        fail(
            f"Service {zaqar_host} publishes {ports}, but zaqar dials port {zaqar_port}."
        )
    if network_port not in ports:
        fail(
            f"Service {zaqar_host} publishes {ports}, but network.port -- which is what the "
            f"server is started with and what the probes reach -- is {network_port}."
        )

for kind, (script, _probe) in sorted(scripts.items()):
    # Quoting is a formatting choice, not a property worth failing on, so `-p 6379`,
    # `-p "6379"` and `-p '6379'` all count.
    if not re.search(rf"""-p\s+["']?{network_port}["']?(?!\d)""", script):
        fail(
            f"the {kind} script does not reach port {network_port} (network.port, the port "
            f"redis-server is started on).\n      got: {script.splitlines()[0][:160]}"
        )

# ---------------------------------------------------------------------------------------------
# 4. Behaviour. Run the RENDERED scripts against a stub redis-cli that reproduces each row of
#    the measured table, and assert the verdict for both probes.
#
#    This is the part an edit cannot pass by accident. A readiness probe that judges by
#    redis-cli's exit status passes `misconf`, `oom`, `loading` and `masterdown` here; one that
#    skips the GET read-back passes `badreadback`; a liveness probe that compares reply text
#    fails `misconf` and would CrashLoop a redis whose disk is read-only.
# ---------------------------------------------------------------------------------------------
REDIS_CLI_STUB = r"""#!/bin/sh
# A stub redis-cli reproducing one measured state of a real redis. Error replies go to stdout
# and exit 0, which is what the real redis-cli does -- that behaviour is the whole point.
mode="${GATE_REDIS_MODE}"
state="${GATE_REDIS_STATE}"
cmd=""
args=""
skip=0
for a in "$@"; do
  if [ "$skip" = "1" ]; then skip=0; continue; fi
  case "$a" in
    -h|-p|-n|-t) skip=1; continue ;;
    -*) continue ;;
  esac
  if [ -z "$cmd" ]; then cmd="$a"; else args="$args $a"; fi
done
case "$mode" in
  gone)
    echo "Could not connect to Redis at 127.0.0.1:6379: Connection refused" >&2
    exit 1
    ;;
  loading)
    echo "LOADING Redis is loading the dataset in memory"
    exit 0
    ;;
  misconf)
    echo "MISCONF Redis is configured to save RDB snapshots, but it's currently unable to persist to disk."
    exit 0
    ;;
  masterdown)
    case "$cmd" in
      PING|ping) echo "MASTERDOWN Link with MASTER is down and replica-serve-stale-data is set to 'no'." ;;
      *) echo "READONLY You can't write against a read only replica." ;;
    esac
    exit 0
    ;;
  oom)
    case "$cmd" in
      PING|ping) echo "PONG" ;;
      GET|get) cat "$state" 2>/dev/null ;;
      *) echo "OOM command not allowed when used memory > 'maxmemory'." ;;
    esac
    exit 0
    ;;
  badreadback)
    case "$cmd" in
      PING|ping) echo "PONG" ;;
      SET|set) echo "OK" ;;
      GET|get) echo "a-value-this-server-never-stored" ;;
    esac
    exit 0
    ;;
  healthy)
    case "$cmd" in
      PING|ping) echo "PONG" ;;
      SET|set)
        # argv after the key is "<value> EX <ttl>"; keep the value only.
        set -- $args
        shift
        printf '%s' "$1" > "$state"
        echo "OK"
        ;;
      GET|get) cat "$state" 2>/dev/null ;;
      DEL|del) echo "1" ;;
      *) echo "ERR unknown command '$cmd'" ;;
    esac
    exit 0
    ;;
esac
echo "ERR the stub was given no mode" >&2
exit 1
"""

TIMEOUT_STUB = r"""#!/bin/sh
# A stub timeout(1): hermetic, so this gate needs nothing on PATH but /bin/sh. In the `wedged`
# state it exits 124 without running the command, which is what the real timeout does when the
# budget expires.
if [ "${GATE_REDIS_MODE}" = "wedged" ]; then
  exit 124
fi
shift
exec "$@"
"""

# mode -> (readiness must pass, liveness must pass, why this row exists)
EXPECTED = {
    "healthy": (True, True, "redis accepts a write and serves it back"),
    "loading": (False, True,
                "loading a 311MB RDB: every command is refused with -LOADING, and a restart "
                "would start the load again"),
    "misconf": (False, True,
                "BGSAVE failed and writes are stopped: a read-only or full /data is not "
                "repaired by restarting the container"),
    "masterdown": (False, True,
                   "replica whose master link is down: writes get -READONLY, and the master "
                   "is in another pod"),
    "oom": (False, True,
            "maxmemory reached with noeviction: PING answers PONG while every write is "
            "refused -- the row that makes a PING probe insufficient even when its reply is "
            "read as text"),
    "badreadback": (False, True,
                    "SET is acknowledged but GET returns something else: the read-back is "
                    "what proves the value landed in the keyspace"),
    "gone": (False, False, "nothing is listening: a restart is the right response"),
    "wedged": (False, False,
               "the event loop is blocked and nothing replies inside the budget: a restart is "
               "the right response"),
}

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    bindir = tmp / "bin"
    bindir.mkdir()
    for name, body in (("redis-cli", REDIS_CLI_STUB), ("timeout", TIMEOUT_STUB)):
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    for kind, (script, _probe) in sorted(scripts.items()):
        p = tmp / f"{kind}.sh"
        p.write_text(script + "\n")
        p.chmod(0o755)

    checked = 0
    for mode, (want_ready, want_live, why) in EXPECTED.items():
        state = tmp / f"state-{mode}"
        env = dict(os.environ)
        # The stub dir comes FIRST, so a real redis-cli or timeout on the runner cannot take
        # part in the measurement. The two system directories follow because the probe scripts
        # legitimately use coreutils -- `date`, for the canary value that makes the GET
        # read-back meaningful -- and a PATH with nothing on it fails them for a reason that is
        # not the property under test. That is how the first run of this gate reported a
        # healthy redis as unready.
        env["PATH"] = os.pathsep.join([str(bindir), "/usr/bin", "/bin"])
        env["GATE_REDIS_MODE"] = mode
        env["GATE_REDIS_STATE"] = str(state)
        for kind, want in (("readinessProbe", want_ready), ("livenessProbe", want_live)):
            run = subprocess.run(
                ["/bin/sh", str(tmp / f"{kind}.sh")],
                capture_output=True, text=True, env=env, timeout=60,
            )
            passed = run.returncode == 0
            checked += 1
            if passed is want:
                continue
            verdict = "PASSED" if passed else "failed"
            expected = "pass" if want else "fail"
            fail(
                f"with redis in the {mode!r} state the {kind} {verdict}, and it must "
                f"{expected}.\n"
                f"      {why}.\n"
                f"      probe said: {(run.stdout + run.stderr).strip()[:300] or '(nothing)'}"
            )

if checked != 2 * len(EXPECTED):
    measurement_failed(
        f"only {checked} of {2 * len(EXPECTED)} probe/state combinations ran, so the table "
        f"above was not actually measured"
    )

if failures:
    print(f"FAIL: {len(failures)} problem(s) with redis's probes")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print(f"OK: redis readiness is false in all {sum(1 for v in EXPECTED.values() if not v[0])} "
      f"measured broken states and true when redis accepts a write and serves it back")
print(f"OK: redis liveness is false only in the {sum(1 for v in EXPECTED.values() if not v[1])} "
      f"states a restart repairs (wedged, gone) and true in the rest, so it cannot CrashLoop a "
      f"redis whose disk, memory or master is the problem")
print(f"OK: no startupProbe; both probes carry a timeout budget under their timeoutSeconds")
print(f"OK: Service {zaqar_host}:{zaqar_port} is what the probes reach and what zaqar's "
      f"message store resolves to")
