#!/usr/bin/env python3
"""Keystone's readiness probe must depend on the fernet keys; its liveness probe must not.

Why this gate exists. /healthcheck is oslo's healthcheck middleware: it proves the WSGI app is
listening and touches neither fernet nor the database. With an empty keystone-fernet-keys Secret
the pod reports 1/1 Ready, and so do the Deployment, the Composition, and the umbrella's own
top-level Ready -- while every POST /v3/auth/tokens returns 500 with KeysNotFound. Measured on a
live install, not hypothesised: issue #99.

That makes a revert of the readiness probe exactly as invisible as the bug was. Nothing fails,
nothing restarts, no dashboard changes colour; authentication simply stops working. A gate is
the only thing that would notice.

It also asserts the converse: liveness must NOT check the keys. Missing keys are not repaired by
a restart -- deleting the pod with an empty Secret was measured and the keys did not return,
because keystone-fernet-setup is a Completed Job (issue #41). A liveness probe on this condition
converts a visible unready pod into a CrashLoop and repairs nothing.

Renders the chart and inspects the probes as the kubelet would receive them, rather than grepping
the template, because what matters is the object the kubelet acts on.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
CHART = ROOT / "blueprints/keystone/chart"
KEY_DIR = "/etc/keystone/fernet-keys"


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


if not CHART.is_dir():
    print(f"FAILED MEASUREMENT: no keystone chart at {CHART} -- wrong path?")
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

try:
    import yaml
except ImportError:
    print("SKIP: PyYAML unavailable")
    sys.exit(0)

docs = [d for d in yaml.safe_load_all(out.stdout) if d]
if not docs:
    print("FAILED MEASUREMENT: chart rendered 0 objects -- a gate over nothing passes everything")
    sys.exit(2)

deploys = [
    d for d in docs
    if d.get("kind") == "Deployment" and d["metadata"]["name"] == "keystone-api"
]
if not deploys:
    fail("no keystone-api Deployment in the rendered chart")

pod = deploys[0]["spec"]["template"]["spec"]
c = pod["containers"][0]

ro = c.get("readinessProbe")
if not ro:
    fail("keystone-api has no readinessProbe")
ro_text = json.dumps(ro)
if KEY_DIR not in ro_text:
    fail(
        f"keystone-api's readinessProbe does not reference {KEY_DIR}.\n"
        f"      It must fail when the fernet keys are missing, or an identity plane that\n"
        f"      cannot issue a single token reports Ready at every layer (issue #99).\n"
        f"      got: {ro_text[:300]}"
    )

lv = c.get("livenessProbe")
if lv and KEY_DIR in json.dumps(lv):
    fail(
        f"keystone-api's livenessProbe references {KEY_DIR}.\n"
        f"      Missing keys are not repaired by a restart (issue #41), so this turns a\n"
        f"      visible unready pod into a CrashLoop. Check them in readiness only."
    )

# The other half of the same failure mode. #99 made a missing-keys state visible; #41 is the
# recovery. A Completed Job never re-runs, so without an init container that re-runs
# fernet_setup on every pod start, an emptied Secret stays empty -- measured at 180s with the
# Job reporting 1 succeeded. Detection without recovery leaves an operator a permanently
# unready pod; recovery without detection is the silent 500s. Both or neither.
inits = pod.get("initContainers") or []
ensure = [ic for ic in inits if "fernet_setup" in " ".join(ic.get("command") or [])]
if not ensure:
    fail(
        "keystone-api has no init container running fernet_setup.\n"
        "      Without it an emptied keystone-fernet-keys Secret is never repopulated, because\n"
        "      keystone-fernet-setup is a Job and a Completed Job does not re-run (issue #41).\n"
        f"      init containers found: {[ic.get('name') for ic in inits]}"
    )

# And it must stay narrowly permitted. Granting the API pod blanket Secret access is the
# tempting fix for any RBAC error here, and it would hand a long-lived internet-facing pod
# every Secret in the namespace to cover two it owns. The Job's own Role does exactly that;
# it is not a precedent to copy.
sa = pod.get("serviceAccountName")
wide = []
for doc in docs:
    if doc.get("kind") != "Role":
        continue
    bound = False
    for rb in docs:
        if rb.get("kind") != "RoleBinding":
            continue
        if rb.get("roleRef", {}).get("name") != doc["metadata"]["name"]:
            continue
        if any(s.get("kind") == "ServiceAccount" and s.get("name") == sa
               for s in rb.get("subjects") or []):
            bound = True
    if not bound:
        continue
    for rule in doc.get("rules") or []:
        if "secrets" not in (rule.get("resources") or []):
            continue
        verbs = set(rule.get("verbs") or [])
        if verbs & {"create", "delete", "deletecollection", "patch", "update", "*"} and not rule.get("resourceNames"):
            wide.append((doc["metadata"]["name"], sorted(verbs)))
if wide:
    fail(
        f"a Role bound to ServiceAccount {sa!r} grants unscoped write access to secrets:\n"
        + "".join(f"      {n}: verbs={v}\n" for n, v in wide)
        + "      Scope it with resourceNames to the Secrets this pod owns. `create` cannot be\n"
        + "      restricted by resourceName, so it must not be granted here at all."
    )

print(f"OK: keystone-api readiness requires {KEY_DIR}; liveness does not")
print(f"OK: fernet_setup init container present ({ensure[0].get('name')}); "
      f"secret write access is resourceNames-scoped")
