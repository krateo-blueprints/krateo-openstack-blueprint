#!/usr/bin/env python3
"""The orchestrator must refuse to install components whose pods no node can run.

Every component chart pins its pods with a nodeSelector and nothing in the charts creates the
labels those selectors want. That precondition WAS documented -- `kubectl label --overwrite
nodes --all openstack-control-plane=enabled` appears in both of the README's install sequences,
with the scheduler's own message quoted beside it -- and a fresh GKE install stalled on it
anyway, with five pods Pending, because the scheduler says

    0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector

which names neither the label nor the chart that wants it. The operator found it by diffing a
working cluster's node labels against the broken one's. A precondition that can only be noticed
by someone reading prose will eventually not be noticed.

Worse, Pending is the VISIBLE half. A DaemonSet whose nodeSelector matches no node has
desiredNumberScheduled=0 and creates no pod at all: no Pending pod, no event, nothing to find.
Measured on a 4-node GKE install of profile=full whose nodes carried only
openstack-control-plane: 263 pods, 227 of them selecting that label, and nova-compute, libvirt,
cyborg-agent, neutron-ovs-agent and openvswitch all at desiredNumberScheduled=0 -- an OpenStack
with no hypervisor and no data plane, reported by nothing.

So the umbrella checks it mechanically (templates/compositions.yaml -> osh.requireNodeLabels)
and this asserts the mechanism, in five parts:

 1. THE REQUIRED SET IS THE RENDERED TRUTH. The umbrella's .Values.nodeLabelPreconditions must
    equal, label for label and component for component, the nodeSelectors that the component
    charts actually RENDER. Derived here from `helm template` of all 35 charts, never from a
    hand-written table.

    Rendered pods and not the charts' values keys, which is the correctness of the whole thing:

      labels.<group>.node_selector_key across the 35 charts' values.yaml   6 distinct labels
      nodeSelector on a pod spec the charts actually render                4 distinct labels

    `sriov=enabled` and `openstack-node=enabled` are in the values and in no rendered pod --
    they belong to manifests disabled by default (neutron's sriov agent, ceilometer's ipmi
    DaemonSet). Requiring them would refuse installs over labels nothing asks for, which is a
    worse defect than the one the check replaces. A chart MENTIONING a label and a pod SELECTING
    on it are different facts and only the second can strand a workload. Part 5 asserts they
    really are unused, so enabling one cannot slip past.

 2. THE CHECK IS WIRED IN, ONCE, AND ABOVE THE COMPONENT LOOP. osh.requireNodeLabels must be
    called from compositions.yaml, the node list must be looked up exactly once in the whole
    chart, and that lookup must sit before the `range` over components. A per-component node
    fetch is the mistake osh.crdExists carries a 13-minute render measurement for.

 3. THE CHECK IS GUARDED, AND THE GUARD AGREES WITH THE GRANT. The node lookup must be
    conditional on osh.nodeReadGranted. This is not hygiene: the identity that renders this
    chart cannot list nodes (core-provider's generated role has no nodes rule), and a refused
    `lookup` is a TEMPLATE ERROR, not an empty result. Unguarded, the umbrella would fail to
    render on every reconcile of every install, and a release that stops rendering gets
    uninstalled (#68).

    The grant itself is a release asset an administrator applies
    (blueprints/openstack/node-reader-rbac.yaml) rather than something the chart creates for
    itself -- the chart is able to, since escalate and bind are both in that generated role, and
    widening the privileges of the identity that renders you is a decision a person should be
    able to refuse. That split means nothing at install time couples the two halves: the guard
    looks a name up, the asset declares one, and a drift between them raises no error on either
    side. The guard just never finds the grant and the check silently stops being enforced
    everywhere. So this asserts they agree -- name, a rule that really covers nodes/list, a
    system:serviceaccounts:<namespace> group subject -- that the asset is actually published by
    release-tag.yaml (an unpublished asset makes the documented `kubectl apply` a 404, which is
    #50), and that no template has quietly re-added the self-grant.

 4. THE DECISION IS CORRECT, EXERCISED WITH SYNTHETIC NODE LISTS. osh.nodeLabelsMissing and
    osh.requireNodeLabels are pure functions of the node list they are handed, so this calls
    them from a probe template with fabricated nodes and asserts all three states:

      no nodes                        -> renders, does not fail      (the CI / first-reconcile
                                                                      case: `helm template` has
                                                                      no cluster, so failing
                                                                      would break every PR)
      nodes carrying every label      -> renders, does not fail
      nodes missing one label         -> FAILS, naming that label, the components that want it,
                                         and the kubectl command

    and the last one once per required label, so narrowing the check to the dominant label
    cannot pass. What this does NOT cover is stated plainly: it proves the decision, the message
    and the `fail`, with a node list this script wrote. It does not prove that `lookup "v1"
    "Node"` returns that shape from a real API server (verified separately by hand, with `helm
    template --dry-run=server` against a live 4-node cluster: 4 items, labels readable), and it
    cannot prove what composition-dynamic-controller does with the error, which needs a cluster
    whose nodes lack the label.

 5. THE UNUSED LABELS ARE STILL UNUSED. The label groups that exist in the charts' values but
    that no rendered pod selects on are reported, and asserted to be exactly that. A re-vendor
    that enables one, or a chart that adds a node-pinned workload, changes part 1's answer and
    fails here rather than leaving the check measuring a stale set.

Never exits 0 without measuring. Four gates in this repo printed "SKIP: PyYAML unavailable" and
exited 0 for weeks (#120); "not measured" must not read as "fine".
"""
import atexit
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
CHART = ROOT / "blueprints/openstack/chart"
UMBRELLA = CHART / "values.yaml"
COMPOSITIONS = CHART / "templates/compositions.yaml"
HELPERS = CHART / "templates/_helpers.tpl"
RBAC = ROOT / "blueprints/openstack/node-reader-rbac.yaml"
RELEASE_WORKFLOW = ROOT / ".github/workflows/release-tag.yaml"
TOOLKIT = ROOT / "charts/helm-toolkit"

# Every kind that carries a pod template, and where the pod spec lives in it. A nodeSelector
# anywhere else cannot strand a workload.
POD_SPEC_AT = {
    "Deployment": lambda d: d["spec"]["template"]["spec"],
    "DaemonSet": lambda d: d["spec"]["template"]["spec"],
    "StatefulSet": lambda d: d["spec"]["template"]["spec"],
    "Job": lambda d: d["spec"]["template"]["spec"],
    "CronJob": lambda d: d["spec"]["jobTemplate"]["spec"]["template"]["spec"],
    "Pod": lambda d: d["spec"],
}


def measurement_failed(msg):
    # Exit 2 and never 0. A gate that reports success without measuring anything is the same
    # bug as a probe that cannot fail (#120).
    print(f"FAILED MEASUREMENT: {msg}")
    sys.exit(2)


try:
    import yaml
except ImportError:
    measurement_failed("PyYAML unavailable, so no chart's nodeSelectors were ever inspected")

if not shutil.which("helm"):
    measurement_failed("helm is not on PATH; the required label set is derived by rendering")
if not CHART.is_dir():
    measurement_failed(f"no umbrella chart at {CHART} -- wrong path?")
if not UMBRELLA.is_file():
    measurement_failed(f"no umbrella values at {UMBRELLA}")
if not COMPOSITIONS.is_file():
    measurement_failed(f"no {COMPOSITIONS}, which is where the check has to be wired in")
if not HELPERS.is_file():
    measurement_failed(f"no {HELPERS}, which is where the check lives")
if not TOOLKIT.is_dir():
    measurement_failed(f"no helm-toolkit at {TOOLKIT}; no component chart can render without it")

try:
    umbrella = yaml.safe_load(UMBRELLA.read_text()) or {}
except yaml.YAMLError as e:
    measurement_failed(f"{UMBRELLA} does not parse -- {str(e)[:200]}")
if not isinstance(umbrella, dict):
    measurement_failed(f"{UMBRELLA} does not parse into a mapping")

comps = umbrella.get("components")
if not isinstance(comps, list) or not comps:
    measurement_failed("the umbrella declares no components, so there is nothing to render")
try:
    names = sorted(c["name"] for c in comps)
except (TypeError, KeyError) as e:
    measurement_failed(f"a component in {UMBRELLA} has no name -- {str(e)[:120]}")

problems = []


# ---------------------------------------------------------------------------
# Part 1: what the charts actually require, measured by rendering them.
# ---------------------------------------------------------------------------
def render(chart_dir, extra=()):
    """helm template a chart in a freshly created empty directory."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "chart"
        shutil.copytree(chart_dir, work)
        cy = work / "Chart.yaml"
        cy.write_text(cy.read_text().replace("CHART_VERSION", "0.0.0-gate"))
        (work / "charts").mkdir(exist_ok=True)
        shutil.copytree(TOOLKIT, work / "charts/helm-toolkit", dirs_exist_ok=True)
        out = subprocess.run(
            # Without --kube-version a kubeVersion-constrained chart renders nothing, and a
            # gate over an empty document set passes everything.
            ["helm", "template", "gate", str(work), "--kube-version", "1.36.0", *extra],
            capture_output=True, text=True,
        )
    return out


def render_docs(name):
    chart = ROOT / "blueprints" / name / "chart"
    if not (chart / "Chart.yaml").is_file():
        return None, f"no chart at blueprints/{name}/chart"
    out = render(chart)
    if out.returncode != 0:
        tail = out.stderr.strip().splitlines()[-1][:200] if out.stderr.strip() else "no stderr"
        return None, f"helm template failed -- {tail}"
    try:
        docs = [d for d in yaml.safe_load_all(out.stdout) if d]
    except yaml.YAMLError as e:
        return None, f"rendered output does not parse as YAML -- {str(e)[:200]}"
    if not docs:
        return None, "rendered 0 objects"
    return docs, None


measured = {}  # (key, value) -> set of component names whose rendered pods select on it
pod_specs = 0
for name in names:
    if name == "openstack":  # the umbrella renders no pods of its own
        continue
    docs, err = render_docs(name)
    if err:
        # A component that cannot be rendered cannot be measured. That is a gap in the
        # measurement, not a pass.
        measurement_failed(f"{name}: {err}")
    for doc in docs:
        at = POD_SPEC_AT.get(doc.get("kind"))
        if not at:
            continue
        try:
            spec = at(doc)
        except (KeyError, TypeError):
            continue
        pod_specs += 1
        for key, value in ((spec or {}).get("nodeSelector") or {}).items():
            measured.setdefault((key, str(value)), set()).add(name)

if not pod_specs:
    measurement_failed("not one rendered pod spec was found across 35 charts; every label "
                       "would look unused and this gate would pass anything")
if not measured:
    measurement_failed("no rendered pod carries a nodeSelector at all; either the charts "
                       "stopped pinning their pods or this gate is reading the wrong field")

declared = umbrella.get("nodeLabelPreconditions")
if not isinstance(declared, list) or not declared:
    problems.append(
        "blueprints/openstack/chart/values.yaml declares no nodeLabelPreconditions, so "
        "osh.requireNodeLabels has nothing to require and the check passes every cluster. "
        f"The charts render pods selecting on: "
        f"{', '.join(f'{k}={v}' for k, v in sorted(measured))}")
    declared = []

declared_map = {}
for entry in declared:
    if not isinstance(entry, dict) or "key" not in entry or "value" not in entry:
        problems.append(f"a nodeLabelPreconditions entry is not a key/value mapping: {entry!r}")
        continue
    cs = entry.get("components")
    if not isinstance(cs, list) or not cs:
        problems.append(f"nodeLabelPreconditions entry {entry['key']}={entry['value']} lists no "
                        "components, so no selection can ever require it")
        continue
    declared_map[(str(entry["key"]), str(entry["value"]))] = set(cs)

for label in sorted(set(measured) | set(declared_map)):
    shown = f"{label[0]}={label[1]}"
    want, have = measured.get(label), declared_map.get(label)
    if want and not have:
        problems.append(
            f"{shown} is selected on by the rendered pods of {', '.join(sorted(want))} and is "
            "NOT declared in nodeLabelPreconditions, so an install can be allowed to proceed "
            "with no node able to run them")
    elif have and not want:
        problems.append(
            f"{shown} is declared in nodeLabelPreconditions and NO rendered pod selects on it. "
            "The check would refuse a cluster over a label nothing asks for, which blocks "
            "installs that are fine")
    elif want != have:
        extra = sorted(have - want)
        missing = sorted(want - have)
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)} (their pods select on it and a "
                          "selection containing only them would not require it)")
        if extra:
            detail.append(f"wrongly lists {', '.join(extra)} (no pod of theirs selects on it, "
                          "so selecting them alone would demand a label they do not need)")
        problems.append(f"{shown}: the declared component list is not the rendered one -- "
                        + "; ".join(detail))


# ---------------------------------------------------------------------------
# Part 2 and 3: the check is wired in once, above the component loop, and guarded.
# ---------------------------------------------------------------------------
compositions = COMPOSITIONS.read_text()
helpers = HELPERS.read_text()

# Comments are stripped before anything is counted. These templates document their cluster
# reads at length and quote the very expressions this gate looks for -- osh.nodeReadGranted's
# comment contains the `lookup "v1" "Node"` error message verbatim -- so counting raw text
# finds two node lookups where the chart performs one, and a gate that miscounts in the safe
# direction today miscounts in the other direction tomorrow.
TEMPLATE_COMMENT = re.compile(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", re.DOTALL)


def code_only(text):
    # Blanked, not deleted: line numbers have to survive so that "is the lookup inside the
    # component range" can still be answered by position.
    return TEMPLATE_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)


all_templates = {p: code_only(p.read_text()) for p in sorted((CHART / "templates").glob("*"))
                 if p.is_file()}

compositions_code = code_only(compositions)
helpers_code = code_only(helpers)

if "osh.requireNodeLabels" not in compositions_code:
    problems.append(
        "templates/compositions.yaml never calls osh.requireNodeLabels, so no node label is "
        "checked before the component Compositions are emitted. The helper existing is not the "
        "mechanism; being called is")

node_lookup = re.compile(r'lookup\s+"v1"\s+"Node"')
lookup_sites = [p.name for p, t in all_templates.items() if node_lookup.search(t)]
total_lookups = sum(len(node_lookup.findall(t)) for t in all_templates.values())
if total_lookups == 0:
    problems.append("no template looks the cluster's nodes up, so nothing is ever measured "
                    "against a real cluster")
elif total_lookups > 1:
    problems.append(
        f"the node list is looked up {total_lookups} times across "
        f"{', '.join(lookup_sites)}. It must be fetched ONCE per render and "
        "threaded as an argument: osh.crdExists carries the measurement of what repeating a "
        "cluster-wide fetch costs here (13 minutes on a 257-CRD cluster, inside a controller "
        "timeout)")
else:
    lines = compositions_code.splitlines()
    lookup_at = next((i for i, l in enumerate(lines) if node_lookup.search(l)), None)
    range_at = next((i for i, l in enumerate(lines) if re.search(r"range\s+\$c\s*:=", l)), None)
    if lookup_at is None:
        problems.append("the single node lookup is not in templates/compositions.yaml, which is "
                        "where this chart hoists its cluster reads")
    elif range_at is None:
        problems.append("templates/compositions.yaml no longer ranges over components; this "
                        "gate cannot tell whether the node lookup is inside the loop")
    elif lookup_at > range_at:
        problems.append(
            f"the node lookup (line {lookup_at + 1}) is INSIDE the component range (line "
            f"{range_at + 1}), so it runs once per component -- 34 full node-list reads per "
            "pass. Hoist it above the range, as $crds is")

    guard_window = ("\n".join(lines[max(0, lookup_at - 6):lookup_at + 1])
                    if lookup_at is not None else "")
    if "osh.nodeReadGranted" not in guard_window:
        problems.append(
            "the node lookup is not guarded by osh.nodeReadGranted. The identity that renders "
            "this chart cannot list nodes unless an administrator has applied the node-read "
            "grant, and a refused `lookup` is a template error rather than an empty result -- "
            "unguarded, the umbrella fails to render on every reconcile of every install and "
            "the release gets uninstalled (#68). `helm template` cannot catch this, because "
            "with no cluster the lookup is never attempted")

for helper in ("osh.nodeReadGranted", "osh.nodeLabelsWanted", "osh.nodeLabelsMissing",
               "osh.requireNodeLabels", "osh.nodeReaderName"):
    if f'define "{helper}"' not in helpers_code:
        problems.append(f"{helper} is not defined in templates/_helpers.tpl")

# The grant is a release asset an administrator applies, not something the chart creates, so
# nothing at install time couples the two. The guard looks a name up and the asset declares one;
# a drift between them produces no error anywhere, on either side -- the guard simply never finds
# the grant and the check silently stops being enforced on every cluster. Same class as the
# upstream-divergence registry: both halves present is not the property that matters, the two
# halves agreeing is.
guard_name = re.search(r'define\s+"osh\.nodeReaderName"\s*-?\}\}\s*([A-Za-z0-9][A-Za-z0-9.-]*)',
                       helpers_code)
if not guard_name:
    problems.append("osh.nodeReaderName does not resolve to a literal name, so this gate cannot "
                    "tell whether the guard and the release asset still agree")
elif not RBAC.is_file():
    problems.append(
        f"{RBAC.relative_to(ROOT)} is gone. It is the release asset that grants the orchestrator "
        "the node read its guard waits for, so without it osh.nodeReadGranted is false on every "
        "cluster and the precondition is never measured anywhere -- the mechanism still fully "
        "present and enforcing nothing")
else:
    try:
        rbac_docs = [d for d in yaml.safe_load_all(RBAC.read_text()) if d]
    except yaml.YAMLError as e:
        measurement_failed(f"{RBAC.relative_to(ROOT)} does not parse -- {str(e)[:200]}")
    want = guard_name.group(1)

    # The name must carry NO version, on either side. A version-pinned grant is a known, shipped
    # failure: a peer's chart used a version-pinned ClusterRoleBinding and it "used to wedge
    # every migration's FRESH SA with `nodes is forbidden` (Synced=False) until an operator
    # granted it by hand". Here the consequence is the mirror image and quieter -- a release that
    # ships a new object name finds no grant under it, so every upgrade silently reverts to the
    # skip path until somebody applies the new asset, and nothing says the check stopped running.
    # Applied once must mean applied once, for every release after it.
    version_like = re.compile(r"v?\d+[.-]\d+[.-]\d+|v\d+-\d+|\bv\d+\b")
    if version_like.search(want):
        problems.append(
            f"osh.nodeReaderName resolves to {want!r}, which contains a version. The grant is "
            "applied once by an administrator and must keep being found by every later release; "
            "a versioned name means each upgrade looks for a grant nobody has applied yet and "
            "silently stops enforcing the check")
    for d in rbac_docs:
        n = d.get("metadata", {}).get("name", "")
        if version_like.search(n):
            problems.append(
                f"{RBAC.relative_to(ROOT)} declares {d.get('kind')} {n!r}, whose name contains a "
                "version. This asset is applied once, not per release")
        for s in (d.get("subjects") or []):
            if version_like.search(str(s.get("name", ""))):
                problems.append(
                    f"{RBAC.relative_to(ROOT)}: the binding's subject {s.get('name')!r} contains "
                    "a version. A version-stamped subject goes stale on the next release and a "
                    "stale subject is not a skip -- the guard sees the grant, the lookup is "
                    "refused, and the render aborts. Use the "
                    "system:serviceaccounts:<namespace> group")

    roles = [d for d in rbac_docs if d.get("kind") == "ClusterRole"]
    bindings = [d for d in rbac_docs if d.get("kind") == "ClusterRoleBinding"]
    named_roles = [d for d in roles if d.get("metadata", {}).get("name") == want]
    if not named_roles:
        problems.append(
            f"{RBAC.relative_to(ROOT)} declares no ClusterRole named {want!r}, which is the name "
            f"osh.nodeReaderName looks up. It has "
            f"{[d.get('metadata', {}).get('name') for d in roles]}. A rename on either side is "
            "silent: the guard finds nothing and the check stops being enforced")
    elif not any(
        ("" in (r.get("apiGroups") or []) or "*" in (r.get("apiGroups") or []))
        and ("nodes" in (r.get("resources") or []) or "*" in (r.get("resources") or []))
        and ("list" in (r.get("verbs") or []) or "*" in (r.get("verbs") or []))
        for r in (named_roles[0].get("rules") or [])
    ):
        problems.append(
            f"{RBAC.relative_to(ROOT)}: ClusterRole {want} carries no rule covering nodes/list, "
            "so applying it grants nothing the guard needs. The guard checks the rule and not "
            "just the name, so the result is a permanent skip rather than a 403 -- which is the "
            "safe failure and the invisible one")
    named_bindings = [d for d in bindings if d.get("metadata", {}).get("name") == want]
    if not named_bindings:
        problems.append(f"{RBAC.relative_to(ROOT)} declares no ClusterRoleBinding named {want!r}; "
                        "an unbound ClusterRole grants nobody anything")
    else:
        b = named_bindings[0]
        if (b.get("roleRef") or {}).get("name") != want:
            problems.append(f"{RBAC.relative_to(ROOT)}: the binding's roleRef is "
                            f"{(b.get('roleRef') or {}).get('name')!r}, not {want!r}")
        groups = [s for s in (b.get("subjects") or [])
                  if s.get("kind") == "Group"
                  and str(s.get("name", "")).startswith("system:serviceaccounts:")]
        if not groups:
            problems.append(
                f"{RBAC.relative_to(ROOT)}: the binding's subject is not a "
                "system:serviceaccounts:<namespace> group. The guard requires that shape, and "
                "the alternative -- naming the controller's service account -- cannot be written "
                "down correctly: it is <plural>-<chart version>, the plurals are irregular "
                "(Magnum -> magna), and a version-stamped subject goes stale every release and "
                "yields the 403 that aborts the render")

    # An asset that is not published is not installable. The docs point at
    # /releases/download/<version>/node-reader-rbac.yaml, and a file that only exists in the
    # tree makes that a 404 -- which is precisely #50, where the CompositionDefinitions the docs
    # told a reader to apply were not published either.
    if not RELEASE_WORKFLOW.is_file():
        problems.append("no .github/workflows/release-tag.yaml, so this gate cannot tell whether "
                        "the node-read grant is published as a release asset")
    elif RBAC.name not in RELEASE_WORKFLOW.read_text():
        problems.append(
            f"release-tag.yaml never publishes {RBAC.name}, so the asset exists in the tree and "
            "not in any release. The documented `kubectl apply -f "
            ".../releases/download/<version>/node-reader-rbac.yaml` would 404 and the check "
            "would be unenforceable by anyone following the README -- the same defect as #50")



# ---------------------------------------------------------------------------
# Part 4: exercise the decision with synthetic node lists.
# ---------------------------------------------------------------------------
PROBE = """{{- $sel := splitList " " (include "osh.selected" (list $)) }}
{{- $nodes := NODES }}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: gate-probe
data:
  wanted: {{ include "osh.nodeLabelsWanted" (list $ $sel) | quote }}
  missing: {{ include "osh.nodeLabelsMissing" (list $ $sel $nodes) | quote }}
{{- include "osh.requireNodeLabels" (list $ $sel $nodes) -}}
"""


def node_list_literal(label_sets):
    """A Helm `list` of fabricated node objects, each carrying the given labels."""
    if not label_sets:
        return "list"
    nodes = []
    for i, labels in enumerate(label_sets):
        pairs = " ".join(f'{json.dumps(k)} {json.dumps(v)}' for k, v in sorted(labels.items()))
        nodes.append(f'(dict "metadata" (dict "name" "gate-node-{i}" '
                     f'"labels" (dict {pairs})))')
    return "list " + " ".join(nodes)


# The probe chart is prepared ONCE and only its probe template is rewritten per call. Two
# dozen decisions are exercised below, and copying the chart tree for each of them -- and in an
# earlier version helm-toolkit too, which this chart does not even depend on -- took the gate
# from seconds to minutes. A gate slow enough to be skipped locally is a gate that stops being
# run before a pull request.
_probe_tmp = tempfile.mkdtemp(prefix="osh-node-label-gate-")
PROBE_CHART = Path(_probe_tmp) / "chart"
shutil.copytree(CHART, PROBE_CHART)
_cy = PROBE_CHART / "Chart.yaml"
_cy.write_text(_cy.read_text().replace("CHART_VERSION", "0.0.0-gate"))
atexit.register(shutil.rmtree, _probe_tmp, True)


def probe(label_sets, extra=()):
    """Render the real umbrella chart plus a probe template, with a synthetic node list.

    The chart is the real one, so the helpers under test are the shipped ones and the real
    values supply the required set; only the node list is fabricated. compositions.yaml also
    calls osh.requireNodeLabels in the same render, with the empty list `lookup` gives it, so
    any failure can only come from the probe.

    No helm-toolkit: the umbrella declares no dependencies and renders none of the openstack
    charts' templates, which is also why `helm lint` rejects the chart when the toolkit is
    copied in beside it.
    """
    (PROBE_CHART / "templates/zz-gate-probe.yaml").write_text(
        PROBE.replace("NODES", node_list_literal(label_sets)))
    try:
        return subprocess.run(
            ["helm", "template", "gate", str(PROBE_CHART), "--kube-version", "1.36.0", *extra],
            capture_output=True, text=True,
        )
    finally:
        (PROBE_CHART / "templates/zz-gate-probe.yaml").unlink(missing_ok=True)


def probe_data(out):
    for doc in yaml.safe_load_all(out.stdout):
        if doc and doc.get("metadata", {}).get("name") == "gate-probe":
            return doc["data"]
    return None


# The four selections the install paths actually use. Each is exercised in full, because the
# required set is selection-dependent and a check that only ever sees the default profile only
# ever enforces that profile's labels: profile=identity wants one label and profile=full wants
# all four, so testing the default alone would leave three unenforced and look green.
SELECTIONS = (
    ("profile=identity", ("--set", "profile=identity")),
    ("profile=full", ("--set", "profile=full")),
    ("enabled={nova,neutron}", ("--set", "enabled={nova,neutron}")),
    ("enabled={keystone}", ("--set", "enabled={keystone}")),
)

# The label set each selection requires, read back from the chart itself rather than recomputed
# here, so the probes exercise what the chart believes rather than what this script would have
# believed.
baseline = probe([])
if baseline.returncode != 0:
    tail = baseline.stderr.strip().splitlines()[-1][:300] if baseline.stderr.strip() else ""
    measurement_failed(
        "the umbrella does not render with an EMPTY node list. That is the `helm template` "
        f"case, which runs in CI on every pull request, and it must never fail -- {tail}")
data = probe_data(baseline)
if data is None:
    measurement_failed("the probe rendered no ConfigMap, so no decision was exercised")

# The chart must NOT grant itself this permission. It is able to -- escalate and bind are both
# in the controller's generated role, and a server-side dry-run create as a real controller
# service account passes admission -- and the whole point of the asset is that widening the
# privileges of the identity that renders you is a decision a person makes and can refuse. A
# template that quietly re-added it would restore the convenience and remove the choice.
#
# Read from the RENDERED output and not from the template text: the templates legitimately
# mention ClusterRoles (the guard looks two up) and nodes (the variable is called $nodes), so a
# text scan reports the guard itself as a self-grant. What matters is the object the cluster
# would act on.
for doc in yaml.safe_load_all(baseline.stdout):
    if not doc or doc.get("kind") not in ("ClusterRole", "Role"):
        continue
    for rule in (doc.get("rules") or []):
        if (("" in (rule.get("apiGroups") or []) or "*" in (rule.get("apiGroups") or []))
                and ("nodes" in (rule.get("resources") or [])
                     or "*" in (rule.get("resources") or []))):
            problems.append(
                f"the chart renders {doc['kind']} {doc.get('metadata', {}).get('name')!r} with a "
                "rule covering nodes. The node-read grant is deliberately a release asset an "
                f"administrator applies ({RBAC.relative_to(ROOT)}), not something this chart "
                "creates for itself: doing it from a template means using escalate/bind to "
                "widen the permissions of the identity that renders the chart, and the grant is "
                "cluster-scoped node read for a whole namespace's service accounts")

wanted_default = [t for t in data["wanted"].split(" ") if t]

# With no nodes, osh.nodeLabelsMissing honestly reports that no node carries any of them -- it
# is a pure predicate over the list it was handed and knows nothing about why the list is
# empty. The skip lives one level up, in osh.requireNodeLabels, and the two assertions here are
# what hold that division in place: the predicate must NOT quietly answer "nothing missing"
# (which would make the check inert on a real cluster that genuinely carries no labels), and
# the render must still succeed (asserted above, which is the `helm template` case). Collapsing
# the skip into the predicate would pass this gate's render check and silently disable the
# mechanism everywhere.
if data["missing"] != data["wanted"]:
    problems.append(
        f"with an empty node list osh.nodeLabelsMissing reports {data['missing']!r} where the "
        f"selection wants {data['wanted']!r}. It must report every wanted label as uncarried -- "
        "an empty node list means no node carries anything. If it answers 'nothing missing' "
        "instead, the skip has been moved into the predicate and a real cluster that carries "
        "none of these labels will pass the check as well")
if not wanted_default:
    problems.append("the default profile requires no node label at all, so the check is inert "
                    "on the path the README documents")

checks = 0
enforced = set()

for label, extra in SELECTIONS:
    # No cluster at all: must render. This is `helm template`, which runs in CI on every pull
    # request and on each of these four selections, and the first reconcile of every install,
    # where the node read is not yet granted.
    out = probe([], extra=extra)
    checks += 1
    if out.returncode != 0:
        tail = out.stderr.strip().splitlines()[-1][:300] if out.stderr.strip() else ""
        measurement_failed(f"{label} does not render with an EMPTY node list -- {tail}")
    d = probe_data(out)
    if d is None:
        measurement_failed(f"{label}: the probe rendered no ConfigMap, no decision exercised")
    wanted = [t for t in d["wanted"].split(" ") if t]
    if not wanted:
        problems.append(f"{label} requires no node label at all, so the check is inert on it")
        continue

    # Every label carried: must pass. One node carrying all of them, and the realistic shape
    # of a labelled cluster -- several nodes each carrying all of them.
    carried = dict(t.split("=", 1) for t in wanted)
    for shape, sets in (("one node", [carried]), ("three nodes", [carried] * 3)):
        out = probe(sets, extra=extra)
        checks += 1
        if out.returncode != 0:
            tail = out.stderr.strip().splitlines()[-1][:300] if out.stderr.strip() else ""
            problems.append(f"{label}: {shape} carrying every required label was REFUSED "
                            f"-- {tail}")
        else:
            got = probe_data(out)
            if got and got["missing"]:
                problems.append(f"{label}: {shape} carrying every required label still "
                                f"reports missing={got['missing']!r}")

    # Each label withheld in turn: must fail, naming that label, the components that want it
    # and the command. Once per label per selection, so a check narrowed to the dominant label
    # cannot pass -- openstack-control-plane would still be enforced and the other three would
    # not.
    for withheld in wanted:
        rest = {k: v for k, v in carried.items() if f"{k}={v}" != withheld}
        # A node carrying something irrelevant when nothing else is left, so that the node list
        # is never empty: an empty list is the skip case and would prove nothing here.
        out = probe([rest or {"kubernetes.io/os": "linux"}], extra=extra)
        checks += 1
        if out.returncode == 0:
            problems.append(
                f"{label}: a node list carrying every required label EXCEPT {withheld} "
                "rendered successfully. That label is declared and not enforced, so the "
                "components that need it would be installed onto a cluster that cannot run "
                "them -- which is exactly the silent failure this check exists to prevent")
            continue
        enforced.add(withheld)
        msg = out.stderr
        for needle, why in (
            (withheld, "the message must name the label and the value that are missing"),
            ("kubectl label", "the message must carry the command that fixes it"),
        ):
            if needle not in msg:
                problems.append(f"{label}: the failure for a missing {withheld} does not "
                                f"contain {needle!r} -- {why}. Got: {msg.strip()[:300]}")
        want_components = sorted(declared_map.get(tuple(withheld.split("=", 1)), set()))
        if want_components and not any(c in msg for c in want_components):
            problems.append(
                f"{label}: the failure for a missing {withheld} names none of the components "
                f"that want it ({', '.join(want_components)}), so the operator cannot tell "
                "what is affected or what to deselect")

# Every label the charts require must have been proven to fire, not just declared. Without
# this, a label declared for a component that no selection above reaches would be carried as
# decoration.
unenforced = {f"{k}={v}" for k, v in measured} - enforced
if unenforced:
    problems.append(
        f"these required labels were never shown to fail when absent: "
        f"{', '.join(sorted(unenforced))}. Either no selection exercised here reaches the "
        "components that want them, or the check does not enforce them; a declared label that "
        "cannot be shown to fire is decoration")


# ---------------------------------------------------------------------------
# Part 5: the label groups that exist in values and that no pod selects on.
# ---------------------------------------------------------------------------
def values_label_groups(path, node, trail):
    if not isinstance(node, dict):
        return
    if "node_selector_key" in node or "node_selector_value" in node:
        yield (str(node.get("node_selector_key")), str(node.get("node_selector_value")),
               ".".join(trail))
        return
    for k, v in node.items():
        yield from values_label_groups(path, v, trail + [str(k)])


in_values = {}
for name in names:
    vp = ROOT / "blueprints" / name / "chart/values.yaml"
    if not vp.is_file():
        continue
    try:
        v = yaml.safe_load(vp.read_text()) or {}
    except yaml.YAMLError as e:
        measurement_failed(f"blueprints/{name}/chart/values.yaml does not parse -- "
                           f"{str(e)[:160]}")
    for key, value, group in values_label_groups(vp, (v or {}).get("labels") or {}, []):
        in_values.setdefault((key, value), []).append(f"{name}.{group}")

unused = {k: v for k, v in in_values.items() if k not in measured}

if problems:
    print("node-label precondition: FAIL\n")
    for p in problems:
        print(f"  - {p}\n")
    sys.exit(1)

print(f"node-label precondition: OK -- {len(measured)} label(s) required, derived from "
      f"{pod_specs} rendered pod specs across {len(names) - 1} charts, "
      f"{checks} decisions exercised with synthetic node lists")
for label in sorted(measured, key=lambda l: (-len(measured[l]), l)):
    print(f"    {label[0]}={label[1]:10s} {len(measured[label])} component(s): "
          f"{', '.join(sorted(measured[label]))}")
if unused:
    print("\n  present in the charts' values, selected on by no rendered pod, so deliberately "
          "NOT required (requiring one would refuse installs over a label nothing asks for):")
    for label, groups in sorted(unused.items()):
        print(f"    {label[0]}={label[1]:10s} {', '.join(groups)}")
print("\n  What this does and does not cover: the decision, the message and the `fail` are "
      "exercised above with node lists this script wrote, which proves the logic and the text "
      "and not that `lookup \"v1\" \"Node\"` returns that shape from an API server. The latter, "
      "and the whole guarded firing path, were verified by hand against a live cluster with "
      "`helm template --dry-run=server` (no grant -> renders unmeasured; grant plus unlabelled "
      "nodes -> refused; labelled nodes -> measured; a grant for another namespace -> skips, "
      "not 403). What nothing offline can show is composition-dynamic-controller putting that "
      "message on the Composition's status.")
