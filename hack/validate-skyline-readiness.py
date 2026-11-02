#!/usr/bin/env python3
"""Skyline's nginx must prove it can serve, not merely that it is listening.

Why this gate exists. The skyline pod has two long-lived containers and nginx is the only
listener on the Service port: every request to Skyline, console and API alike, arrives there.
Its sibling skyline container has a readiness probe; nginx had none at all, so the kubelet
counted it ready the instant `nginx -g 'daemon off;'` was exec'd. A pod whose nginx 500s the
console and 502s the API therefore reported 1/1 Ready, and so did the Deployment, the
Composition and the umbrella that this repo's orchestrator gates dependents on -- the same shape
as keystone's KeysNotFound (issue #99) and glance's unwritable store (issue #110).

What it asserts, and why each half matters:

  The probe must traverse nginx's own listener. Addressing the upstream socket directly, as the
  skyline container's probe does, bypasses every part nginx owns -- the copied config, the
  server block, the location, the proxy_pass -- and so cannot see the 502 this is about.

  The probe must traverse the proxy. skyline-nginx-generator routes /api/v1/ to an `upstream
  skyline` whose single server is unix:/var/lib/skyline/skyline.sock. A request that stops at
  the static root proves nothing about that path.

  The probe must also request the console root. nginx has two jobs here and they break
  independently: the console is served from a `root` the generator resolves inside the image,
  falling back to a list of guessed dist-packages directories when the skyline_console import
  fails, and a guess that misses answers 500 to every console request -- try_files falls through
  to an /index.html that is absent too -- from an nginx whose proxy is answering 200.

  A tcpSocket is rejected by name. It is satisfied by an nginx that accepts the connection and
  then returns 502 to everything, which is exactly the class of probe the census of this repo's
  90 containers counted as unable to report failure.

  Nothing that restarts may depend on the upstream. nginx's readiness is deliberately
  co-dependent with the sibling API server -- pod readiness is already the conjunction of the
  two, so nothing new becomes unready, and the check buys the converse case where a healthy API
  server sits behind a wrong proxy. A livenessProbe on the same condition would instead restart
  nginx whenever the API server is briefly down, repairing nothing nginx owns, and a
  startupProbe suppresses liveness AND readiness until it passes, handing back the always-ready
  container this fixes.

A revert is as invisible as the bug: nothing fails, nothing restarts, the pod still reports
Ready, and Skyline simply serves 502s. A gate is the only thing that would notice.

Renders the chart and inspects the probes as the kubelet would receive them, rather than
grepping the template, because what matters is the object the kubelet acts on. The request paths
are read out of the probe by parsing it -- the exec script's AST, or an httpGet's path -- so the
assertions are about what the probe asks for and not about how it is written.

Refuses to pass on zero input. Four gates in this repo printed "SKIP: PyYAML unavailable" and
exited 0 for weeks (#120), which is a gate reporting success without measuring anything -- the
same defect as a probe that cannot fail. Every such case here exits 2.
"""
import ast
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
CHART = ROOT / "blueprints/skyline/chart"

# skyline_apiserver's constants.API_PREFIX, plus the one document under it that the app serves
# without authentication. Both containers' probes request it: the skyline container over the
# unix socket, nginx through the proxy, which is what makes the second a test of the proxy.
API_PATH = "/api/v1/openapi.json"
# The generated config's upstream. A probe naming this has gone around nginx, not through it.
UPSTREAM_SOCKET = "/var/lib/skyline/skyline.sock"


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def not_measured(msg):
    print(f"FAILED MEASUREMENT: {msg}")
    sys.exit(2)


if not CHART.is_dir():
    not_measured(f"no skyline chart at {CHART} -- wrong path?")

try:
    import yaml
except ImportError:
    not_measured("PyYAML unavailable, so the rendered probes were never inspected")

values = yaml.safe_load((CHART / "values.yaml").read_text())
# The port the Service publishes, the containerPort nginx declares and the port nginx's listener
# must be probed on are one value. Read from values rather than hardcoded so that moving it stays
# a one-line change, while losing the check does not.
listen_port = (
    ((values.get("endpoints") or {}).get("skyline") or {}).get("port", {}).get("api", {}).get("default")
)
if not listen_port:
    not_measured(
        "skyline values.yaml no longer declares endpoints.skyline.port.api.default, so this "
        "gate has nothing to compare the probes against"
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
    not_measured("chart rendered 0 objects -- a gate over nothing passes everything")

deploys = [
    d for d in docs
    if d.get("kind") == "Deployment" and d["metadata"]["name"] == "skyline"
]
if not deploys:
    fail("no skyline Deployment in the rendered chart")

pod = deploys[0]["spec"]["template"]["spec"]
labels = deploys[0]["spec"]["template"]["metadata"].get("labels") or {}
containers = {c["name"]: c for c in pod["containers"]}
for name in ("nginx", "skyline"):
    if name not in containers:
        fail(f"no {name} container in the skyline Deployment: {sorted(containers)}")

# The premise of the whole gate: nginx, and nothing else, answers the Service port. If a second
# container ever listens there, or the Service moves off nginx's port, the reasoning below has to
# be redone rather than silently kept.
published = sorted(
    {
        p["port"]
        for s in docs
        if s.get("kind") == "Service"
        and (s["spec"].get("selector") or {})
        and all(labels.get(k) == v for k, v in s["spec"]["selector"].items())
        for p in s["spec"]["ports"]
    }
)
if published != [listen_port]:
    not_measured(
        f"the Services selecting the skyline pod publish {published}, not [{listen_port}] as "
        f"endpoints.skyline.port.api.default says -- which of them nginx answers is no longer "
        f"something this gate can tell"
    )
nginx_ports = [p.get("containerPort") for p in containers["nginx"].get("ports") or []]
if nginx_ports != [listen_port]:
    fail(
        f"the nginx container declares containerPort {nginx_ports}, not [{listen_port}].\n"
        f"      The Service publishes {listen_port}, so traffic would arrive at a port nothing "
        f"in this pod is listening on."
    )


def probe_requests(probe, where):
    """The paths a probe asks for, and the port it asks them on, read out of the probe itself.

    An httpGet states both outright. An exec is parsed, not pattern-matched: every string
    constant in the script that looks like a URL path is a path it may request, and the port is
    whichever declared port appears in its text. Parsing rather than grepping keeps the
    assertions about what the probe requests instead of how it was spelled.
    """
    if "tcpSocket" in probe:
        fail(
            f"{where} is a tcpSocket.\n"
            f"      An open socket is what nginx offers while returning 502 to every request,\n"
            f"      so this cannot distinguish 'listening' from 'serving'. It is the shape the\n"
            f"      census of this repo's 90 containers counted as unable to report failure\n"
            f"      (issue #110)."
        )
    if "httpGet" in probe:
        return [str(probe["httpGet"].get("path", ""))], probe["httpGet"].get("port")
    if "exec" not in probe:
        fail(f"{where} is neither exec, httpGet nor tcpSocket: {json.dumps(probe)[:200]}")
    command = probe["exec"].get("command") or []
    script = command[-1] if command else ""
    try:
        tree = ast.parse(script)
    except SyntaxError:
        # Not python -- curl and friends put the URL in the argv, so take the argv as it stands.
        paths = [a for a in command if a.startswith("/") or "://" in a]
        return paths, listen_port if f":{listen_port}" in " ".join(command) else None
    paths = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value.startswith("/")
    ]
    port = listen_port if f":{listen_port}" in script else None
    return paths, port


ro = containers["nginx"].get("readinessProbe")
if not ro:
    fail(
        "the nginx container has no readinessProbe.\n"
        "      It is the only listener on the Service port, so without one an nginx that\n"
        "      500s the console and 502s the API reports Ready at every layer (issue #110)."
    )
paths, port = probe_requests(ro, "the nginx container's readinessProbe")
if port != listen_port:
    fail(
        f"the nginx container's readinessProbe does not address port {listen_port}.\n"
        f"      That is nginx's own listener and the port the Service publishes. A probe that\n"
        f"      goes anywhere else is not testing the path external traffic takes.\n"
        f"      got: {json.dumps(ro)[:300]}"
    )
if UPSTREAM_SOCKET in paths:
    fail(
        f"the nginx container's readinessProbe addresses {UPSTREAM_SOCKET} directly.\n"
        f"      That is the upstream, so the probe has gone around nginx rather than through\n"
        f"      it and proves nothing about the copied config, the location or the proxy_pass\n"
        f"      -- the parts that return 502 while the socket behind them is perfectly fine."
    )
if API_PATH not in paths:
    fail(
        f"the nginx container's readinessProbe does not request {API_PATH}.\n"
        f"      Only a request routed to the proxy fails when the upstream is broken; without\n"
        f"      one the probe is satisfied by an nginx that 502s every API call.\n"
        f"      requests: {paths}"
    )
if "/" not in paths:
    fail(
        "the nginx container's readinessProbe does not request the console root /.\n"
        "      nginx serves the console from a `root` resolved inside the image, by a fallback\n"
        "      chain of guessed dist-packages directories when the skyline_console import\n"
        "      fails. A guess that misses answers 500 to every console request while the proxy\n"
        "      keeps answering 200, so the proxy check alone does not cover it.\n"
        f"      requests: {paths}"
    )

for kind in ("livenessProbe", "startupProbe"):
    probe = containers["nginx"].get(kind)
    if not probe:
        continue
    if kind == "startupProbe":
        fail(
            "the nginx container has a startupProbe.\n"
            "      It suppresses both liveness and readiness until it succeeds, so whatever it\n"
            "      checks, the readiness above stops being reported while it runs -- which is\n"
            "      the always-ready container this fixed."
        )
    if API_PATH in probe_requests(probe, f"the nginx container's {kind}")[0]:
        fail(
            f"the nginx container's {kind} requests {API_PATH}, which is proxied to the\n"
            f"      sibling API server.\n"
            f"      A transient restart of that container would then restart nginx too, which\n"
            f"      repairs nothing nginx owns. Depend on the upstream in readiness only."
        )

# The other container of the pair. Its probe was reasoned about separately and is the only thing
# that can see a crash-looping API server directly; gating one container's readiness while the
# other's quietly disappears would leave the same hole one container along.
sibling = containers["skyline"].get("readinessProbe")
if not sibling:
    fail(
        "the skyline container has no readinessProbe.\n"
        "      gunicorn binds a unix socket and no port, so without one a crash-looping API\n"
        "      server drives readyReplicas to 1 in every window between crashes (issue #110)."
    )
sibling_paths, _ = probe_requests(sibling, "the skyline container's readinessProbe")
if UPSTREAM_SOCKET not in sibling_paths:
    fail(
        f"the skyline container's readinessProbe no longer addresses {UPSTREAM_SOCKET}.\n"
        f"      gunicorn binds that socket and no TCP port, so it is the only way to ask the\n"
        f"      API server itself whether it is serving.\n"
        f"      requests: {sibling_paths}"
    )

print(f"OK: nginx readiness reaches {API_PATH} and / through its own listener on {listen_port}, "
      f"so a broken proxy or console cannot report Ready")
print(f"OK: no restarting probe on nginx depends on the sibling API server; the skyline "
      f"container still probes {UPSTREAM_SOCKET}")
