#!/usr/bin/env python3
"""No probe that restarts horizon may enter Django.

This is the narrowed survivor of the gate added with issue #110's horizon change. That change
had two halves and only one of them survives the reduction of this fork to upstream
openstack-helm: the readiness half added a keystone-reachability check, which was an
improvement on a probe upstream had not got wrong, and it has been reverted to upstream's
httpGet on `/`. The liveness half fixed a defect and stays, so this gate was narrowed to the
liveness half rather than deleted -- a gate asserting a divergence that no longer exists is
worse than no gate.

The defect. Upstream points horizon's livenessProbe at `/`, and horizon's own
SimultaneousSessionsMiddleware calls caches['default'].get() and .set() on EVERY request, so
every Django path depends on memcached. Measured in the real image
(quay.io/airshipit/horizon:2026.1-ubuntu_noble, running this chart's rendered local_settings
and apache config):

    memcached stopped                       GET / -> 500, GET /auth/login/ -> 500
    memcached answering sets SERVER_ERROR   GET / -> 500, GET /auth/login/ -> 500
    either case          GET /static/dashboard/img/favicon.ico -> 200, 10174 bytes

So upstream's liveness restarts every horizon replica for the duration of a memcached outage.
The restart repairs nothing -- memcached is outside this pod -- and kubernetes-entrypoint then
holds each new pod in Init until memcached returns, having paid for a collectstatic of ~4000
files and a compress again. A probe that can only make the outage worse is broken behaviour,
not a missing feature, which is why this one divergence was ruled in while the readiness one
was not.

Liveness therefore requests the static handler, which Apache serves straight out of the
collected asset tree without entering Django, and this gate asserts that it still does.

A revert of it is as invisible as the bug: nothing fails until memcached does, and then the
restart storm looks like a memcached problem rather than a probe problem. A gate is the only
thing that would notice.

Also asserts the premise rather than assuming it. Upstream's `/` is only broken while the
default cache is a remote memcached -- with a local-memory cache a memcached outage would not
reach Django at all and the divergence would have no defect behind it. So if the cache moves,
this fails loudly instead of passing on a premise that no longer holds.

Renders the chart and inspects the probes as the kubelet would receive them, rather than
grepping the template, because what matters is the object the kubelet acts on. The static
prefix is read from the rendered apache site config rather than hardcoded, because what
matters is that the probe stays inside the paths Apache actually aliases.
"""
import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
CHART = ROOT / "blueprints/horizon/chart"


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


if not CHART.is_dir():
    print(f"FAILED MEASUREMENT: no horizon chart at {CHART} -- wrong path?")
    sys.exit(2)

# FAILED MEASUREMENT and not SKIP, deliberately. The gates here that skipped on a missing PyYAML
# had never once run in CI: setup-python ships no PyYAML, so each printed `SKIP: PyYAML
# unavailable`, exited 0, and the job went green -- a gate reporting success without measuring
# anything, which is the same bug as a probe that cannot fail. The workflow now installs it;
# this exits 2 so that if it stops doing so, the answer is "not measured" and not "fine".
try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so the rendered probes were never inspected")
    sys.exit(2)

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
    print("FAILED MEASUREMENT: chart rendered 0 objects -- a gate over nothing passes everything")
    sys.exit(2)

etc = [d for d in docs if d.get("kind") == "Secret" and d["metadata"]["name"] == "horizon-etc"]
if not etc:
    print("FAILED MEASUREMENT: no horizon-etc Secret rendered, so local_settings is unreadable")
    sys.exit(2)

data = etc[0]["data"]
local_settings = base64.b64decode(data["local_settings"]).decode()
apache_conf = base64.b64decode(data["horizon.conf"]).decode()

# The premise of the divergence: the default cache is a remote memcached, so a memcached outage
# reaches every Django path through SimultaneousSessionsMiddleware's get()/set(). Scoped to the
# CACHES block, because 'BACKEND' is a common key name and matching the first one anywhere in a
# 22KB settings file would be reading whichever setting happens to come first.
caches = re.search(r"^CACHES\s*=\s*\{(.*?)^\}", local_settings, re.M | re.S)
cache_backend = re.search(r"'BACKEND':\s*'([^']+)'", caches.group(1)) if caches else None
if not cache_backend:
    print(
        "FAILED MEASUREMENT: the rendered local_settings declares no CACHES BACKEND, so this "
        "gate cannot check whether upstream's liveness on / would still be the defect this "
        "divergence exists for"
    )
    sys.exit(2)
if "memcached" not in cache_backend.group(1):
    fail(
        f"the default cache is no longer memcached: {cache_backend.group(1)}.\n"
        f"      This divergence exists because horizon's SimultaneousSessionsMiddleware does a\n"
        f"      get() and a set() on the default cache for EVERY request, so with a REMOTE cache\n"
        f"      away every Django path answers 500 -- measured -- and upstream's liveness on /\n"
        f"      restarts every replica for the duration of the outage. On a local-memory cache\n"
        f"      there is no outage to restart for, so the defect would be gone and keeping the\n"
        f"      divergence would need a new reason. Re-judge it instead of leaving this to pass."
    )

# The static prefix Apache serves without entering Django, read from the rendered site config so
# that renaming it cannot leave the liveness probe pointing at a 404.
alias = re.search(r"^\s*Alias\s+(\S+)\s+(\S+)", apache_conf, re.M)
if not alias:
    print(
        "FAILED MEASUREMENT: the rendered apache site config declares no Alias, so this gate "
        "cannot tell which paths Apache serves without entering Django"
    )
    sys.exit(2)
static_prefix = alias.group(1).rstrip("/") + "/"

deploys = [
    d for d in docs
    if d.get("kind") == "Deployment" and d["metadata"]["name"] == "horizon"
]
if not deploys:
    fail("no horizon Deployment in the rendered chart")

pod = deploys[0]["spec"]["template"]["spec"]
srv = [c for c in pod["containers"] if c["name"] == "horizon"]
if not srv:
    fail(f"no horizon container: {[c['name'] for c in pod['containers']]}")
c = srv[0]

# Asserted explicitly, because the loop below has nothing to inspect without it and would pass
# on a container that simply dropped the probe. Upstream ships a livenessProbe here; what this
# fork changes is where it points, so the probe existing is part of what is being asserted.
if not c.get("livenessProbe"):
    fail(
        "horizon has no livenessProbe.\n"
        "      Upstream ships one, and this gate exists to keep it off the Django paths. With no\n"
        "      probe at all there is nothing to keep off them and nothing restarts an apache2\n"
        "      that has stopped serving, which is the failure a restart does repair."
    )

for kind in ("livenessProbe", "startupProbe"):
    probe = c.get(kind)
    if not probe:
        continue
    # A tcpSocket probe is allowed: it depends on nothing outside the container either. An
    # httpGet must stay on the static prefix, and an exec is refused because its reach cannot be
    # read off the object the kubelet acts on. A startupProbe is checked alongside liveness
    # because it is the trap: it suppresses liveness AND readiness until it succeeds, so a
    # Django-dependent check placed there is a pod that never becomes ready during an outage.
    if "tcpSocket" in probe:
        continue
    path = (probe.get("httpGet") or {}).get("path")
    if not path or not path.startswith(static_prefix):
        fail(
            f"horizon's {kind} is neither a tcpSocket nor an httpGet under {static_prefix}: "
            f"{json.dumps(probe)[:200]}\n"
            f"      Anything else enters Django, and every Django path answers 500 while\n"
            f"      memcached is away -- horizon's SimultaneousSessionsMiddleware touches the\n"
            f"      default cache on every request. Measured: with memcached stopped, / and\n"
            f"      /auth/login/ both 500 while {static_prefix}dashboard/img/favicon.ico still\n"
            f"      answered 200. A probe that restarts the container must depend on nothing\n"
            f"      outside it, or a memcached outage becomes a restart of every replica that\n"
            f"      then waits in Init for memcached to return.\n"
            f"      This is the one place horizon's chart diverges from upstream, which points\n"
            f"      liveness at /. See issue #110."
        )

print(f"OK: no restarting probe enters Django; liveness stays under {static_prefix}")
print(f"OK: the default cache is still {cache_backend.group(1)}, which is what makes upstream's "
      f"liveness on / the defect this divergence exists for")
