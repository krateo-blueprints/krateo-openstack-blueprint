#!/usr/bin/env python3
"""No probe that restarts nova-compute may depend on RabbitMQ (issue #146).

    hack/validate-nova-compute-liveness.py [repo-root]

The defect. Upstream's nova-compute liveness AND startup probes run health-probe.py, which pings the service over RPC
(topic=compute) and so needs a working RabbitMQ. Observed on a real cluster while RabbitMQ's release was wedged: both
nova-compute pods Ready=False with 9 restarts each. The probe was right that the bus was unreachable; the restart was
wrong, because killing nova-compute cannot repair RabbitMQ. It restarts every hypervisor's agent at once, at the moment the
control plane is already unwell. The same argument as horizon's liveness (#110, hack/validate-horizon-liveness.py), with
RabbitMQ in memcached's place.

So liveness and startup check only that the nova-compute process exists. Readiness keeps the RPC check, so an outage still
shows as NotReady. The trade-off, stated: a nova-compute hung with RabbitMQ healthy is no longer restarted by liveness;
it shows NotReady instead.

  1. RENDERED. The nova chart is rendered at its defaults and every nova-compute container (one DaemonSet per node
     override) is inspected as the kubelet receives it:
       - a livenessProbe and a startupProbe exist (a dropped probe would pass a check of its contents);
       - neither runs health-probe.py, names the RPC queue, or reads nova's configuration;
       - the readinessProbe still runs health-probe.py with --service-queue-name compute (the premise: without it, an
         outage would show nowhere, and the divergence would need a new reason).
  2. RUN. The rendered liveness command is RUN against stand-in processes: it exits 0 while a process named
     nova-compute runs, and non-zero while only a decoy runs whose argument merely contains the name
     (nova-compute.conf). A probe that always passed would satisfy part 1 and repair nothing.

Fails when helm, PyYAML or psutil is missing, and when any part finds nothing to check.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
CHART = os.path.join(ROOT, "blueprints", "nova", "chart")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")


def measurement_failed(msg):
    print("FAILED MEASUREMENT: " + msg)
    sys.exit(2)


try:
    import yaml
except ImportError:
    measurement_failed("PyYAML unavailable, so the rendered probes were never inspected")
try:
    import psutil  # noqa: F401  (the probe imports it; the stand-in run below needs it here too)
except ImportError:
    measurement_failed("psutil unavailable, so the rendered liveness command could not be run")
if shutil.which("helm") is None:
    measurement_failed("helm not on PATH, so nothing was rendered")
if not os.path.isdir(CHART):
    measurement_failed("no nova chart at %s" % CHART)

failures = []


def fail(m):
    failures.append(m)


def render(tmp):
    w = os.path.join(tmp, "nova")
    shutil.copytree(CHART, w)
    cy = os.path.join(w, "Chart.yaml")
    txt = open(cy).read()
    open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
    os.makedirs(os.path.join(w, "charts"), exist_ok=True)
    shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
    r = subprocess.run(["helm", "template", "nova-gate", w, "--namespace", "openstack", "--kube-version", "1.36.0"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        measurement_failed("helm template failed: %s" % r.stderr.strip()[:400])
    return [d for d in yaml.safe_load_all(r.stdout) if d]


def command_of(probe):
    return [str(a) for a in ((probe or {}).get("exec") or {}).get("command") or []]


RPC_MARKS = ("health-probe.py", "--service-queue-name", "/etc/nova", "--config-file", "--config-dir", "transport")


def check_container(ds, c):
    where = "%s/%s" % (ds, c["name"])
    for kind in ("livenessProbe", "startupProbe"):
        probe = c.get(kind)
        if not probe:
            fail("1: %s has no %s. Upstream ships one; this gate keeps it off RabbitMQ, so a missing probe is not the fix"
                 % (where, kind))
            continue
        cmd = command_of(probe)
        hits = [m for m in RPC_MARKS if any(m in a for a in cmd)]
        if not cmd or hits:
            fail("1: %s's %s depends on more than the process (%s): %s\n"
                 "      It runs on every node; with RabbitMQ away it fails everywhere, and the kubelet restarts every\n"
                 "      nova-compute at once, which repairs nothing. Check the process only (#146)."
                 % (where, kind, ", ".join(hits) or "not an exec probe", " ".join(cmd)[:200]))
    ready = command_of(c.get("readinessProbe"))
    if not ("/tmp/health-probe.py" in ready and "--service-queue-name" in ready
            and ready[ready.index("--service-queue-name") + 1:][:1] == ["compute"]):
        fail("1: %s's readinessProbe no longer runs health-probe.py --service-queue-name compute: %s\n"
             "      Readiness is where a RabbitMQ outage must still show once liveness ignores it; without it the\n"
             "      outage shows nowhere. Re-judge the divergence instead of letting this pass." % (where, " ".join(ready)[:200]))
    return command_of(c.get("livenessProbe"))


def run_probe(cmd, tmp):
    """Exit status of the liveness command with a stand-in nova-compute running (expect 0) and with only a decoy."""
    if len(cmd) != 3 or cmd[1] != "-c":
        fail("2: the liveness command is not `python -c <code>`, so it cannot be run here: %s" % " ".join(cmd)[:200])
        return
    code = cmd[2]
    stand_in = os.path.join(tmp, "bin", "nova-compute")
    os.makedirs(os.path.dirname(stand_in), exist_ok=True)
    open(stand_in, "w").write("#!%s\nimport time\ntime.sleep(60)\n" % sys.executable)
    os.chmod(stand_in, 0o755)
    decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--config-file",
                              "/etc/nova/nova-compute.conf"])
    try:
        time.sleep(0.5)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        if r.returncode == 0:
            fail("2: the liveness command passes with no nova-compute running (only a decoy whose argument is "
                 "nova-compute.conf): it would never restart a dead agent")
        proc = subprocess.Popen([stand_in])
        try:
            time.sleep(0.5)
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
            if r.returncode != 0:
                fail("2: the liveness command fails while a process named nova-compute runs: %s"
                     % (r.stderr.strip()[-200:] or "exit %d" % r.returncode))
        finally:
            proc.kill()
            proc.wait()
    finally:
        decoy.kill()
        decoy.wait()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        docs = render(tmp)
        if not docs:
            measurement_failed("the nova chart rendered 0 objects")
        seen = []
        liveness = None
        for d in docs:
            if d.get("kind") != "DaemonSet" or not d["metadata"]["name"].startswith("nova-compute"):
                continue
            for c in d["spec"]["template"]["spec"].get("containers") or []:
                if c["name"] == "nova-compute":
                    seen.append(d["metadata"]["name"])
                    liveness = check_container(d["metadata"]["name"], c) or liveness
        if not seen:
            measurement_failed("no nova-compute container in any rendered DaemonSet")
        if liveness:
            run_probe(liveness, tmp)
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: nova-compute liveness and startup check the process only, in %d DaemonSet(s) (%s); readiness keeps the "
          "RPC check; the liveness command, run, passes with nova-compute and fails with only a decoy"
          % (len(seen), ", ".join(seen)))


main()
