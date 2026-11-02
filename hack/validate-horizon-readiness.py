#!/usr/bin/env python3
"""Horizon's readiness probe must depend on keystone; nothing that restarts may touch Django.

Why this gate exists. GET / is all the HTTP-shaped signal horizon has, and with keystone gone it
is a success: measured in the real image (quay.io/airshipit/horizon:2026.1-ubuntu_noble, running
this chart's rendered local_settings and apache config), with keystone stopped / answered 200 and
rendered the whole login form, and a login POST answered 200 too, carrying `Unable to establish
connection to keystone endpoint.` So the pod reports 1/1 Ready, and so do the Deployment, the
Composition and the umbrella, while nobody can log in. Keystone's own KeysNotFound was the same
shape (issue #99), measured on a live install: the dependency gate reads a Ready that means
nothing as "safe to build on".

That makes a revert of this probe exactly as invisible as the bug it detects. Nothing fails,
nothing restarts, no dashboard changes colour; the dashboard simply stops letting anyone in. A
gate is the only thing that would notice.

It also asserts the converse, for every probe that can restart the container: liveness and
startup must not reference keystone, and must not enter Django at all. Those are two separate
facts, both measured. A keystone outage is not repaired by restarting horizon, so a liveness
probe on keystone is a restart storm that fixes nothing. And horizon's own
SimultaneousSessionsMiddleware calls caches['default'].get() and .set() on every request, so with
memcached away EVERY Django path answers 500 while /static is still served straight out of the
collected asset tree by Apache -- measured both ways. A liveness probe on / therefore restarts
every replica during a memcached outage, and the restart repairs nothing, because
kubernetes-entrypoint then holds the new pod in Init until memcached returns. A startupProbe is
named explicitly because it is the trap: it suppresses both liveness and readiness until it
succeeds, so a dependency check placed there removes the very signal this gate is about.

Finally it asserts the probe's premise rather than assuming it. Readiness keeps fetching / and
that is counted here as the session-cache check, which is only true while sessions live in the
cache: SESSION_ENGINE = django.contrib.sessions.backends.cache with a memcached CACHES backend.
If either moves, / stops exercising the session store and the probe needs rewriting, so this
fails loudly instead of passing on a premise that no longer holds.

Renders the chart and inspects the probes as the kubelet would receive them, rather than grepping
the template, because what matters is the object the kubelet acts on. The URLs are compared
against the rendered local_settings rather than recomputed from values, because what matters is
that the probe reaches the same keystone Django was told to use.
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

values = yaml.safe_load((CHART / "values.yaml").read_text())
web_port = (
    ((values.get("endpoints") or {}).get("dashboard") or {}).get("port", {}).get("web", {})
).get("default")
identity_host = (
    ((values.get("endpoints") or {}).get("identity") or {}).get("hosts", {}).get("internal")
)
if not web_port or not identity_host:
    print(
        "FAILED MEASUREMENT: horizon values.yaml no longer declares "
        "endpoints.dashboard.port.web.default and endpoints.identity.hosts.internal, "
        "so this gate has nothing to compare the probes against"
    )
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

# The URL Django itself is told to use. The probe must reach THIS keystone, not a plausible
# recomputation of it: a probe aimed somewhere else is green while every login fails.
m = re.search(r'^OPENSTACK_KEYSTONE_URL\s*=\s*"([^"]+)"', local_settings, re.M)
if not m:
    print(
        "FAILED MEASUREMENT: the rendered local_settings declares no OPENSTACK_KEYSTONE_URL, "
        "so this gate cannot tell which keystone the probe should reach"
    )
    sys.exit(2)
keystone_url = m.group(1)

# The premise of counting readiness' GET / as the session-cache check. True only while sessions
# live in the cache and that cache is memcached; see the module docstring.
session_engine = re.search(r"^SESSION_ENGINE\s*=\s*'([^']+)'", local_settings, re.M)
# Scoped to the CACHES block: 'BACKEND' is a common key name and matching the first one anywhere
# in a 22KB settings file would be reading whichever setting happens to come first.
caches = re.search(r"^CACHES\s*=\s*\{(.*?)^\}", local_settings, re.M | re.S)
cache_backend = re.search(r"'BACKEND':\s*'([^']+)'", caches.group(1)) if caches else None
if not session_engine or not cache_backend:
    print(
        "FAILED MEASUREMENT: the rendered local_settings declares no SESSION_ENGINE or no CACHES "
        "BACKEND, so this gate cannot check what readiness' GET / actually exercises"
    )
    sys.exit(2)
if not session_engine.group(1).endswith(".cache") or "memcached" not in cache_backend.group(1):
    fail(
        f"sessions no longer live in a memcached cache: SESSION_ENGINE is "
        f"{session_engine.group(1)} and the default CACHES BACKEND is {cache_backend.group(1)}.\n"
        f"      Readiness fetches / and that is counted as the session-store check, because\n"
        f"      horizon's SimultaneousSessionsMiddleware does a get() and a set() on the default\n"
        f"      cache for every request -- measured: with memcached stopped / answers 500. Move\n"
        f"      the session store and / stops touching it, so the probe must be rewritten\n"
        f"      instead of being quietly left to pass."
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

ro = c.get("readinessProbe")
if not ro:
    fail("horizon has no readinessProbe")
ro_text = json.dumps(ro)
if keystone_url not in ro_text:
    fail(
        f"horizon's readinessProbe does not reach {keystone_url}.\n"
        f"      It must fail when keystone is unreachable, or a dashboard nobody can log into\n"
        f"      reports Ready at every layer -- measured in the real image, where / answered 200\n"
        f"      with the whole login form and the login POST answered 200 with `Unable to\n"
        f"      establish connection to keystone endpoint.`\n"
        f"      got: {ro_text[:300]}"
    )
if f"localhost:{web_port}/" not in ro_text:
    fail(
        f"horizon's readinessProbe no longer fetches http://localhost:{web_port}/.\n"
        f"      The keystone check is in addition to serving a page, not instead of it: that\n"
        f"      one request is what proves Django starts, local_settings parses, the login page\n"
        f"      renders, and -- through SimultaneousSessionsMiddleware's get()/set() -- that the\n"
        f"      memcached session store answers, using horizon's own client library.\n"
        f"      got: {ro_text[:300]}"
    )

for kind in ("livenessProbe", "startupProbe"):
    probe = c.get(kind)
    if not probe:
        continue
    if keystone_url in json.dumps(probe):
        fail(
            f"horizon's {kind} reaches {keystone_url}.\n"
            f"      A keystone outage is not repaired by restarting horizon -- keystone is\n"
            f"      outside this pod -- so this turns a visible unready pod into a restart storm\n"
            f"      across every replica. A startupProbe also suppresses readiness until it\n"
            f"      succeeds, which hides the signal entirely. Check keystone in readiness."
        )
    # A tcpSocket probe is allowed: it depends on nothing outside the container either. An
    # httpGet must stay on the static prefix, and an exec is refused because its reach cannot be
    # read off the object the kubelet acts on.
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
            f"      then waits in Init for memcached to return."
        )

# The other half of the same dependency. Detection without a start-time gate would let a pod come
# up into a keystone that is not there and only then discover it; kubernetes-entrypoint holds the
# pod in Init instead. Both halves are needed: the init container runs once, and a keystone that
# dies an hour later is what the readiness probe above is for.
inits = pod.get("initContainers") or []
dep_services = ""
for ic in inits:
    for env in ic.get("env") or []:
        if env.get("name") == "DEPENDENCY_SERVICE":
            dep_services = env.get("value") or ""
if identity_host not in dep_services:
    fail(
        f"the entrypoint init container does not wait for {identity_host}: "
        f"DEPENDENCY_SERVICE={dep_services!r}.\n"
        f"      dependencies.static.dashboard.services must include the identity service, so a\n"
        f"      pod does not start into a keystone that is absent and serve a login form that\n"
        f"      cannot work."
    )

print(f"OK: horizon readiness requires {keystone_url} and still fetches localhost:{web_port}/")
print(f"OK: no restarting probe reaches keystone or enters Django; liveness stays under "
      f"{static_prefix}")
print(f"OK: sessions still live in {session_engine.group(1)} on {cache_backend.group(1)}, which "
      f"is what makes GET / a session-store check")
