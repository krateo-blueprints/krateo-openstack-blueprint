#!/usr/bin/env python3
"""A version bump must not close the emission gate on a component that already exists.

THE DEFECT THIS EXISTS FOR (#166). compositions.yaml emits a component's Composition only when

    and (eq $crd "true") (or (deps ready) (eq $exists "true"))

and `$crd` used to be `osh.crdExists <kind> (osh.version $)` -- a CRD serving the CHART's
current version. At the first render after a version bump no component CRD serves that version
yet, because the CompositionDefinitions rendered in the same pass are what create it. `$crd` is
the OUTER conjunct, so it closed the gate for all 34 components before the `$exists` escape was
ever evaluated, every component Composition vanished from the manifest, nothing carries
`helm.sh/resource-policy: keep`, and helm pruned 34 live CRs. Measured on a0-openstack-d4
during 0.3.32 -> 0.3.33: 6 CRs left for 37 releases, 27 of them running workloads with nothing
reconciling them, and three of the six re-created with new release names that could not patch
the live objects because spec.selector is immutable.

WHAT THIS CHECKS. The decision now goes through osh.emitVersion -> osh.pinVersion, and the
reachability of the origin annotation goes through osh.servedVersion. Both are PURE functions
of their arguments -- no `lookup` -- so this drives the real helpers from a probe chart with
synthetic CRD lists standing in for cluster states, and asserts the decision each one produces.
It does not re-implement the gate: the probe calls the same osh.* definitions the chart does,
from the chart's own _helpers.tpl.

WHAT A GREEN RESULT HERE DOES NOT COVER, which matters more than what it does:

  * The `lookup` path. osh.originVersion reads the annotation off a live object, and `helm
    template` disables lookup, so OFFLINE the origin always reads "" and osh.pinVersion falls
    back to the current version. This check therefore injects the origin directly where the
    real code would have read it. That the read itself now succeeds at the bump instant -- the
    thing osh.servedVersion was added for -- is asserted here only as "the guard admits a
    served version", not end to end. Only a cluster at the bump instant can show the rest.
  * Consequently, EVERY OTHER GATE IN hack/ RENDERS THE PRUNED CASE. With no cluster the CRD
    list is empty, so `helm template` of the umbrella emits zero component Compositions and has
    always done so. A check that asserts composition behaviour from a plain `helm template` is
    asserting the state this defect produces and will agree with itself. That is why this file
    builds a probe instead of rendering the umbrella.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    print("MEASUREMENT FAILED: PyYAML is not importable, so no probe could be parsed and every "
          "assertion below would be skipped rather than evaluated.")
    sys.exit(1)

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
HELPERS = ROOT / "blueprints" / "openstack" / "chart" / "templates" / "_helpers.tpl"
COMPOSITIONS = ROOT / "blueprints" / "openstack" / "chart" / "templates" / "compositions.yaml"

problems = []

CUR = "v0-3-33"
OLD = "v0-3-32"


def crd(kind, versions):
    return {"spec": {"group": "composition.krateo.io", "names": {"kind": kind},
                     "versions": [{"name": n, "served": s} for n, s in versions]}}


# Cluster states, each named for what it is rather than for what it should produce.
STATES = {
    # The bump instant: the new CompositionDefinitions have not reconciled, so only the old
    # version is served. THIS is the state that pruned d4.
    "bump_instant": [crd("Glance", [(OLD, True)])],
    # After the new CRD version appears, both are served.
    "both_served": [crd("Glance", [(OLD, True), (CUR, True)])],
    # Steady state long after a bump: only the current version remains served.
    "only_current": [crd("Glance", [(CUR, True)])],
    # The new version is registered but not served yet -- a real intermediate state.
    "new_registered_unserved": [crd("Glance", [(OLD, True), (CUR, False)])],
    # First install: the kind has no CRD at all. Nothing exists, so nothing can be pruned.
    "first_install": [],
}

PROBE = """
{{- $crds := .Values.state }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: probe
data:
{{- range $label, $origin := .Values.origins }}
  {{ $label }}_served: {{ include "osh.servedVersion" (list "Glance" (include "osh.version" (list $)) $crds) | quote }}
  {{ $label }}_pinned: {{ include "osh.pinVersion" (list (include "osh.version" (list $)) $origin "Glance" $crds) | quote }}
  {{ $label }}_gate: {{ include "osh.crdExists" (list "Glance" (include "osh.pinVersion" (list (include "osh.version" (list $)) $origin "Glance" $crds)) $crds) | quote }}
{{- end }}
"""


def run_probe(state):
    tmp = tempfile.mkdtemp()
    try:
        chart = os.path.join(tmp, "probe")
        os.makedirs(os.path.join(chart, "templates"))
        shutil.copy(HELPERS, os.path.join(chart, "templates", "_helpers.tpl"))
        with open(os.path.join(chart, "templates", "probe.yaml"), "w") as fh:
            fh.write(PROBE)
        with open(os.path.join(chart, "Chart.yaml"), "w") as fh:
            yaml.safe_dump({"apiVersion": "v2", "name": "probe", "version": "0.0.0"}, fh)
        with open(os.path.join(chart, "values.yaml"), "w") as fh:
            yaml.safe_dump({
                "chartVersion": "0.3.33",
                "state": state,
                # The origin the real code would have READ off the live object. Injected because
                # `helm template` disables lookup -- see the module docstring.
                "origins": {"existing": OLD, "born_at_current": CUR, "unstamped": ""},
            }, fh)
        out = subprocess.run(["helm", "template", "p", chart],
                             capture_output=True, text=True)
        if out.returncode != 0:
            tail = out.stderr.strip().splitlines()[-1][:300] if out.stderr.strip() else ""
            return None, tail
        for doc in yaml.safe_load_all(out.stdout):
            if doc and doc.get("kind") == "ConfigMap":
                return doc.get("data") or {}, None
        return None, "the probe rendered no ConfigMap"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# What each state must decide, for a component that ALREADY EXISTS and was born at OLD. This is
# the population the prune destroyed, and the only entry that was wrong before the fix is
# bump_instant -- which is the whole point.
EXPECTED_EXISTING = {
    "bump_instant": ("true", OLD),
    "both_served": ("true", OLD),
    "only_current": ("true", CUR),      # OLD no longer served, so the pin legitimately releases
    "new_registered_unserved": ("true", OLD),
    "first_install": ("", CUR),         # no CRD at all: nothing exists, nothing to prune
}

results = {}
for name, state in STATES.items():
    data, err = run_probe(state)
    if data is None:
        print(f"MEASUREMENT FAILED: probe '{name}' did not render -- {err}. No assertion below "
              "was evaluated, so a green result would mean nothing was tested.")
        sys.exit(1)
    results[name] = data

if not results:
    print("MEASUREMENT FAILED: no cluster state was probed at all.")
    sys.exit(1)

for name, (want_gate, want_pin) in EXPECTED_EXISTING.items():
    d = results[name]
    got_gate, got_pin = d.get("existing_gate"), d.get("existing_pinned")
    if got_gate != want_gate:
        problems.append(
            f"state '{name}': an EXISTING component born at {OLD} gets $crd={got_gate!r}, "
            f"expected {want_gate!r}. $crd is the outer conjunct of the emission gate, so "
            f"{'a false value here removes its Composition from the manifest and helm PRUNES the live CR' if want_gate == 'true' else 'a true value here emits against a CRD that does not exist'}.")
    if got_pin != want_pin:
        problems.append(
            f"state '{name}': the component would be emitted at {got_pin!r}, expected "
            f"{want_pin!r}. A component's GVK must not move on a render where nothing about "
            "the component changed.")

# The bump instant is the regression itself. Said separately so the failure names the defect.
if results["bump_instant"].get("existing_gate") != "true":
    problems.append(
        "REGRESSION OF #166: at the bump instant -- the new CRD version not served yet -- the "
        "gate is closed for a component that already exists. This is exactly the state that "
        "pruned 34 live CRs on a0-openstack-d4, leaving 27 running workloads with no CR "
        "reconciling them.")

# osh.servedVersion must find the OLD version when the current one is not served. That is the
# reachability the origin annotation depends on: if this is "", osh.originVersion cannot read
# the stamp, osh.pinVersion falls back to current, and the whole fix is inert.
sv = results["bump_instant"].get("existing_served")
if sv != OLD:
    problems.append(
        f"osh.servedVersion answers {sv!r} at the bump instant, expected {OLD!r}. The "
        "write-once origin annotation is read through this version; answering '' makes it "
        "unreadable, which is the second half of #166 -- osh.originVersion was itself guarded "
        "on the current version, so the origin pin was inert in the same instant for the same "
        "reason the gate was.")

# Preferring the current version is the documented tie-break, and an unstated tie-break is how
# osh.version became load-bearing here in the first place. Assert it rather than trust it.
if results["both_served"].get("existing_served") != CUR:
    problems.append(
        f"with both versions served osh.servedVersion answers "
        f"{results['both_served'].get('existing_served')!r}, expected {CUR!r}. The probe must "
        "prefer the current version so ordinary renders are unchanged; it exists only to make "
        "the origin annotation READABLE, never to choose a version -- the annotation decides, "
        "through osh.pinVersion.")

# A component with no origin stamp must not be pinned to one.
if results["both_served"].get("unstamped_pinned") != CUR:
    problems.append(
        f"an unstamped component pins to "
        f"{results['both_served'].get('unstamped_pinned')!r}, expected {CUR!r}: a component "
        "with no origin is new and belongs at the current version.")

# The gate and the emitted apiVersion must resolve the SAME version. They did not before #166 --
# the gate asked osh.version while the emitter asked osh.pinVersion -- so this asserts the chart
# routes both through the one function rather than deriving the fact twice.
comp = COMPOSITIONS.read_text()
helpers = HELPERS.read_text()

# STRUCTURAL ASSERTIONS. The probe above exercises the helpers; these assert that the chart
# actually ROUTES through them. Both halves are needed and neither is sufficient: the probe
# cannot see compositions.yaml's gate expression, and a structural check cannot tell whether
# the helpers decide correctly.
#
# Written as POSITIVE assertions -- "the line must be this" -- rather than as searches for the
# old defect's text. An absence-check passes for every way of writing the defect that its
# author did not anticipate, which is the same shape as a guard that excludes the case it
# guards.
EXPECTED_GATE = '{{- $emitVer := include "osh.emitVersion" (list $ $c.kind $c.name $crds) }}\n' \
                '{{- $crd := include "osh.crdExists" (list $c.kind $emitVer $crds) }}'
if EXPECTED_GATE not in comp:
    found = [l.strip() for l in comp.splitlines() if "$crd :=" in l] or ["(no $crd assignment at all)"]
    problems.append(
        "compositions.yaml does not compute $crd from osh.emitVersion. Found: "
        + " | ".join(found)
        + ". The gate and the emitted apiVersion must resolve ONE version through ONE function: "
          "deriving the same fact at two call sites is how they came to disagree, which is #166.")

# osh.originVersion must read the stamp through a SERVED version. This is the half the probe
# genuinely cannot see -- the read needs `lookup`, which is disabled offline -- so it is
# asserted structurally and the limitation is stated rather than papered over. Before #166 this
# function guarded itself on the current version, so at the bump instant it returned "" and the
# origin pin was inert in exactly the instant it was written for.
origin_def = helpers[helpers.index('{{- define "osh.originVersion" -}}'):]
origin_def = origin_def[:origin_def.index("{{- end -}}")]
if 'include "osh.servedVersion"' not in origin_def:
    problems.append(
        "osh.originVersion does not resolve through osh.servedVersion. It reads a write-once "
        "annotation off a live object, and annotations are readable through ANY served version; "
        "guarding it on the chart's current version makes it return \"\" at the bump instant, "
        "osh.pinVersion then falls back to current, and every fix above it is inert. This is "
        "asserted structurally because `helm template` disables lookup -- the probe injects the "
        "origin and so cannot observe this read at all.")

if problems:
    print(f"FAIL: {len(problems)} problem(s) across {len(results)} probed cluster states")
    for p in problems:
        print("  - " + p + "\n")
    sys.exit(1)

print(f"OK: {len(results)} cluster states probed through the chart's own helpers; an existing "
      f"component keeps its gate open and its GVK at every one, including the bump instant that "
      f"pruned d4. NOT covered: the `lookup` that reads the origin stamp needs a live cluster, "
      f"so the origin is injected here -- see this file's docstring.")
