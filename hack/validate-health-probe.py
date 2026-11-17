#!/usr/bin/env python3
"""The worker readiness probe works, as rendered (#231, #151).

hack/validate-readiness-probes.py proves every long-running container HAS a readiness probe and
that the probe's script is mounted. It cannot prove the script tells a serving worker from a dead
one. A probe that always fails keeps every worker it guards out of Ready forever, and one that
always passes is the defect #231 reported; both would satisfy the presence gate.

So this renders every chart that ships worker-health-probe.py, requires every rendered copy to be
byte-identical (they come from one helm-toolkit define, and a chart-local edit would fork it),
writes that text out, and runs hack/tests/health_probe against it: oslo.messaging's real RPC
server and dispatcher on the in-process driver, the remote error as it crosses RabbitMQ, and the
/proc reading against real processes and sockets.

Fails when no chart renders the script, when the tests cannot import oslo.messaging, and when any
test is skipped or none ran: a probe test that did not run is not a pass.

Usage: hack/validate-health-probe.py [repo-root]
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so no chart was rendered")
    sys.exit(2)

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
BP = os.path.join(ROOT, "blueprints")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
TESTS = os.path.join(ROOT, "hack", "tests", "health_probe")
KEY = "worker-health-probe.py"

if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)


def render(chart, tmp):
    w = os.path.join(tmp, chart)
    shutil.copytree(os.path.join(BP, chart, "chart"), w)
    cy = os.path.join(w, "Chart.yaml")
    txt = open(cy).read()
    open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
    os.makedirs(os.path.join(w, "charts"), exist_ok=True)
    shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
    r = subprocess.run(["helm", "template", chart + "-gate", w, "--namespace", "openstack"],
                       capture_output=True, text=True)
    if r.returncode:
        return None, r.stderr.strip()[-300:]
    return [d for d in yaml.safe_load_all(r.stdout) if d], None


def main():
    charts = sorted(c for c in os.listdir(BP) if os.path.isdir(os.path.join(BP, c, "chart", "templates"))
                    and any(KEY in open(os.path.join(dp, f)).read()
                            for dp, _, fs in os.walk(os.path.join(BP, c, "chart", "templates")) for f in fs))
    if not charts:
        print("FAILED MEASUREMENT: no chart ships %s" % KEY)
        return 2
    copies, problems = {}, []
    with tempfile.TemporaryDirectory() as tmp:
        for c in charts:
            docs, err = render(c, tmp)
            if docs is None:
                problems.append("%s: does not render: %s" % (c, err))
                continue
            texts = [d["data"][KEY] for d in docs if d.get("kind") == "ConfigMap" and KEY in (d.get("data") or {})]
            if not texts:
                problems.append("%s: names %s in its templates but renders it in no ConfigMap" % (c, KEY))
            copies.update({c: t for t in texts})
        distinct = set(copies.values())
        if len(distinct) > 1:
            problems.append("%s differs between charts (%d versions): it must come from "
                            "helm-toolkit.scripts.health_probe unchanged" % (KEY, len(distinct)))
        if problems or not copies:
            for p in problems or ["no rendered copy of %s" % KEY]:
                print("FAIL: " + p)
            return 1
        script = os.path.join(tmp, KEY)
        open(script, "w").write(distinct.pop())
        os.environ["HEALTH_PROBE_SCRIPT"] = script
        sys.path.insert(0, TESTS)
        try:
            import oslo_messaging  # noqa: F401
        except ImportError:
            print("FAILED MEASUREMENT: oslo.messaging unavailable, so the probe was not exercised "
                  "(pip install oslo.messaging)")
            sys.exit(2)
        suite = unittest.defaultTestLoader.discover(TESTS, pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun == 0 or result.skipped:
        print("FAIL: %d test(s) ran, %d skipped: a probe test that did not run is not a pass"
              % (result.testsRun, len(result.skipped)))
        return 1
    if not result.wasSuccessful():
        print("FAIL: the worker readiness probe's tests failed")
        return 1
    print("OK: %s is identical in %d chart(s) (%s); %d test(s) passed against the rendered text"
          % (KEY, len(copies), ", ".join(sorted(copies)), result.testsRun))
    return 0


if __name__ == "__main__":
    sys.exit(main())
