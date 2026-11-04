#!/usr/bin/env python3
"""Glance's readiness probe must depend on the image store; nothing that restarts may.

Why this gate exists. /healthcheck is oslo's healthcheck middleware: it proves the WSGI app is
listening and touches neither the store nor the database. GET / is no better -- glance answers
it with 300 and a version list, which is a success -- so every HTTP-shaped probe glance has is
satisfied by an API that cannot store a single byte.

The store is the prerequisite that can be absent while the API is perfectly up. glance-images is
a PersistentVolumeClaim by default, the pod sets no fsGroup, and the directory becomes writable
by uid 42424 only because the glance-perms init container chowns it as root. Where the volume
does not honour that chown -- NFS with root_squash is the common one -- every image upload
returns 500 while the pod reports 1/1 Ready, and so do the Deployment, the Composition and the
umbrella. Keystone's KeysNotFound (issue #99) was the same shape, measured on a live install:
the dependency gate reads a Ready that means nothing as "safe to build on".

That makes a revert of this probe exactly as invisible as the bug it detects. Nothing fails,
nothing restarts, no dashboard changes colour; image uploads simply stop working. A gate is the
only thing that would notice.

It also asserts the converse, for every probe that can restart the container: liveness and
startup must NOT check the store. A directory the process cannot write is not repaired by
restarting the process -- the ownership belongs to the volume -- so such a probe converts a
visible unready pod into a CrashLoop and repairs nothing. A startupProbe is named explicitly
because it is the trap: it suppresses both liveness and readiness until it succeeds, so a store
check placed there removes the very signal this gate is about.

Renders the chart and inspects the probes as the kubelet would receive them, rather than grepping
the template, because what matters is the object the kubelet acts on.
"""
import ast
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
CHART = ROOT / "blueprints/glance/chart"


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


if not CHART.is_dir():
    print(f"FAILED MEASUREMENT: no glance chart at {CHART} -- wrong path?")
    sys.exit(2)

# FAILED MEASUREMENT and not SKIP, deliberately. The gates here that skip on a missing PyYAML
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
# The directory the chart configures as its file store, mounts glance-images at, and chowns in
# the glance-perms init container -- all three read this one value. Taken from values rather
# than hardcoded so that moving the store stays a one-line change, while losing the check does
# not.
store_dir = ((values.get("conf") or {}).get("glance") or {}).get("file", {}).get(
    "filesystem_store_datadir"
)
health_path = (
    ((values.get("endpoints") or {}).get("image") or {}).get("path", {}).get("healthcheck")
)
if not store_dir or not health_path:
    print(
        "FAILED MEASUREMENT: glance values.yaml no longer declares "
        "conf.glance.file.filesystem_store_datadir and endpoints.image.path.healthcheck, "
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

deploys = [
    d for d in docs
    if d.get("kind") == "Deployment" and d["metadata"]["name"] == "glance-api"
]
if not deploys:
    fail("no glance-api Deployment in the rendered chart")

pod = deploys[0]["spec"]["template"]["spec"]
api = [c for c in pod["containers"] if c["name"] == "glance-api"]
if not api:
    fail(f"no glance-api container: {[c['name'] for c in pod['containers']]}")
c = api[0]

ro = c.get("readinessProbe")
if not ro:
    fail("glance-api has no readinessProbe")
ro_text = json.dumps(ro)
if store_dir not in ro_text:
    fail(
        f"glance-api's readinessProbe does not reference {store_dir}.\n"
        f"      It must fail when the image store is not writable, or an image service that\n"
        f"      cannot accept a single upload reports Ready at every layer -- the shape of\n"
        f"      issue #99, where /healthcheck answered 200 throughout.\n"
        f"      got: {ro_text[:300]}"
    )
if health_path not in ro_text:
    fail(
        f"glance-api's readinessProbe no longer reaches {health_path}.\n"
        f"      The store check is in addition to the healthcheck, not instead of it: a probe\n"
        f"      that only stats a directory stops noticing a WSGI app that has stopped\n"
        f"      answering.\n"
        f"      got: {ro_text[:300]}"
    )

# Writable and HAVING SPACE are different properties and only the first was asserted. Measured
# on a 4-node GKE install: the store is a 2 GiB PVC which held two images and then refused the
# next upload -- and glance reports a full store as "413 Request Entity Too Large", which reads
# as a limit on the request. Three rounds went to uwsgi's limit-post, oslo_middleware's
# RequestBodySizeLimiter (not in glance's paste pipeline at all) and Keystone unified limits
# before anyone read glance's own line: "Image storage media is full". Issue #163.
#
# PARSED WITH ast, NOT MATCHED AS TEXT. The first version of this check looked for "statvfs",
# "f_bavail" and "floor" in the probe's JSON -- and a single COMMENT naming all three satisfied
# it while the probe measured nothing. That is the fourth time in this repo a check has been
# satisfied by prose about itself, and the third time today. ast does not see comments, so a
# mention cannot stand in for a call.
probe_src = (ro.get("exec") or {}).get("command") or []
probe_src = probe_src[-1] if probe_src else ""
try:
    tree = ast.parse(probe_src)
except SyntaxError as e:
    fail(f"glance-api's readinessProbe script does not parse as Python: {e}")

calls_statvfs = any(
    isinstance(n, ast.Call) and (getattr(n.func, "attr", None) == "statvfs"
                                 or getattr(n.func, "id", None) == "statvfs")
    for n in ast.walk(tree))
reads_bavail = any(isinstance(n, ast.Attribute) and n.attr == "f_bavail" for n in ast.walk(tree))
# a comparison that can FAIL: `free < floor` guarding an exit
guards_exit = False
for n in ast.walk(tree):
    if not isinstance(n, ast.If) or not isinstance(n.test, ast.Compare):
        continue
    if not any(isinstance(o, (ast.Lt, ast.LtE)) for o in n.test.ops):
        continue
    if any(isinstance(x, ast.Call) and (getattr(x.func, "attr", None) == "exit"
                                        or getattr(x.func, "id", None) == "exit")
           for x in ast.walk(n)):
        guards_exit = True

for ok, needle, why in (
    (calls_statvfs, "a call to os.statvfs", "free space has to be measured, not assumed"),
    (reads_bavail, "a read of f_bavail",
     "the AVAILABLE blocks are the ones a non-root writer can use; f_bfree counts reserved "
     "blocks it cannot"),
    (guards_exit, "a `< floor` comparison that exits",
     "a measurement with no threshold cannot fail, so it reports nothing"),
):
    if not ok:
        fail(
            f"glance-api's readinessProbe has no {needle}: {why}.\n"
            f"      A full image store answers every upload with 413 Request Entity Too Large\n"
            f"      while the pod reports Ready -- measured on a 2 GiB store holding two\n"
            f"      images (#163). Writable is not the same property as having room.\n"
            f"      Checked by parsing the script, so a comment naming these cannot satisfy it."
        )

for kind in ("livenessProbe", "startupProbe"):
    probe = c.get(kind)
    if probe and ("statvfs" in json.dumps(probe) or store_dir in json.dumps(probe)):
        fail(
            f"glance-api's {kind} references {store_dir}.\n"
            f"      An unwritable store is not repaired by a restart -- the ownership belongs\n"
            f"      to the volume, not the container -- so this turns a visible unready pod\n"
            f"      into a CrashLoop. A startupProbe also suppresses readiness until it\n"
            f"      succeeds, which hides the signal entirely. Check the store in readiness."
        )

# The other half of the same failure mode. Detection without recovery leaves an operator a
# permanently unready pod. Ownership is re-established on EVERY pod start by an init container,
# which is what makes a store that lost its ownership self-heal; keystone learned this the hard
# way when the equivalent work sat in a Job that, once Completed, never ran again (issue #41).
inits = pod.get("initContainers") or []
perms = [
    ic for ic in inits
    if (ic.get("command") or [])[:1] == ["chown"] and store_dir in (ic.get("command") or [])
]
if not perms:
    fail(
        f"no init container chowns {store_dir} on pod start.\n"
        f"      Without it a store whose ownership is wrong stays wrong, and the readiness\n"
        f"      probe above reports it forever with nothing repairing it. It must be an init\n"
        f"      container and not a Job: a Completed Job does not re-run (issue #41).\n"
        f"      init containers found: {[ic.get('name') for ic in inits]}"
    )

print(f"OK: glance-api readiness requires {store_dir} to be writable AND to have room "
      f"(statvfs against a floor), and still reaches {health_path}")
print(f"OK: no restarting probe checks the store; {perms[0].get('name')} re-chowns it on every "
      f"pod start")
