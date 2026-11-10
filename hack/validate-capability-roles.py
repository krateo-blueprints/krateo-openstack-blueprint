#!/usr/bin/env python3
"""The capability ClusterRoles and guards: who renders them, and that they join something.

    hack/validate-capability-roles.py [repo-root]

Four properties, each one a defect this repo has shipped or would ship silently:

1. EVERY CAPABILITY CLUSTERROLE AGGREGATES TO A ROLE THAT EXISTS.
   A capability joins a predefined role through a `rbac.krateo.io/aggregate-to-<role>=true`
   label, which the predefined role's aggregationRule selects on. Nothing validates the
   `<role>` part. A label naming a role that does not exist is valid YAML and renders without
   complaint. The ClusterRole is created and listed in the catalog, and it is **joined to
   nothing**: the capability looks present and grants no access. This repo shipped exactly that:
   `aggregateTo: [viewer, admin]` against predefined roles named read-only, tenant-reader,
   auditor, operator, tenant-admin and org-admin. The overlap was zero, in four capabilities,
   for as long as they had existed.

2. THE PER-CLUSTER CHART RENDERS THEM, AND THE UMBRELLA DOES NOT.
   Up to 0.3.46 the umbrella rendered them, and the umbrella is installed by an `Openstack`
   Composition. So the roles and the compute-tier guard came into existence only with the first
   instance. The role that grants `create openstacks` could never authorize that create, and the
   guard that matches CREATE never saw it. Checked here:
     - blueprints/openstack/capabilities renders exactly one ClusterRole per capability, and
       one ValidatingAdmissionPolicy and one binding per `enforcement: abac` capability, with
       `failurePolicy: Fail`;
     - the umbrella, rendered with no cluster, renders none of them.
   Every rendered object carries `helm.sh/resource-policy: keep`, which the gap-free handoff
   from a 0.3.46 install depends on (templates/_capabilities.tpl).

3. THE UMBRELLA'S HANDOFF RENDERS ONLY WHAT ITS OWN RELEASE OWNS.
   On a cluster upgraded from 0.3.46, the umbrella re-renders an object while its own release
   still owns the live copy, so the upgrade cannot delete it before the per-cluster chart takes
   it over. Getting the ownership test wrong in either direction is silent:
     - too strict, and the upgrade deletes the guard;
     - too loose, and two Helm releases fight over one object on every reconcile.
   The decision is a pure function of the live objects passed in, so it is exercised here with
   synthetic ones: all owned here, all owned by the per-cluster chart, none, same release name
   in another namespace, and a partial takeover.

4. THE PER-CLUSTER CHART'S INPUTS ARE GENERATED FROM THE UMBRELLA AND HAVE NOT DRIFTED
   (hack/gen-capabilities-chart.py --check).

It renders the charts rather than reading values.yaml. A template that stops emitting a label,
a policy or the `keep` annotation produces the same silent outcome as bad values, and a
values-level check would pass it. A render that produces no capability object at all is a
failed measurement and fails, not a pass.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

# The predefined roles a capability may aggregate into.
#
# KNOWN LIMIT, and it is the same class of fault this script exists to catch: this is a
# COPY of the platform's role names, not derived from them. It catches a capability
# aggregating to a name that is not in this set. It cannot catch this set drifting from
# the platform -- if a predefined role is renamed or added platform-side, every check
# here stays green while the two disagree, and the symptom is again a capability that
# renders, lists, and joins nothing.
#
# Deriving the set from the published predefined-roles chart's aggregationRule selectors
# would close that, and is the right eventual shape. It is not done here because it makes
# a per-PR gate depend on fetching an external chart, which trades a silent-drift failure
# for an availability failure -- and a gate that cannot reach the network must then
# distinguish "the roles disagree" from "I could not look", or it becomes the thing it was
# meant to prevent. Worth doing deliberately rather than as a footnote to this fix.
#
# Until then: adding a name here without it existing platform-side recreates the original
# bug, with the gate's blessing.
PREDEFINED = {
    "read-only",
    "tenant-reader",
    "auditor",
    "operator",
    "tenant-admin",
    "org-admin",
}

AGG = re.compile(r"^rbac\.krateo\.io/aggregate-to-([a-z0-9][a-z0-9-]*)$")
KINDS = ("ClusterRole", "ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding")
OWNER_RELEASE = ("openstack", "openstack")          # the umbrella, as README.md installs it
CAPS_RELEASE = ("openstack-capabilities", "openstack-system")


def stage(src: Path, dst: Path, toolkit: Path | None = None) -> Path:
    """Copy a chart with the release-stamped placeholder replaced, as the release does."""
    shutil.copytree(src, dst)
    for f in ("Chart.yaml", "values.yaml", "values.schema.json"):
        p = dst / f
        if p.is_file():
            p.write_text(p.read_text().replace("CHART_VERSION", "0.0.0-validate"))
    if toolkit is not None and toolkit.is_dir():
        (dst / "charts").mkdir(exist_ok=True)
        shutil.copytree(toolkit, dst / "charts" / "helm-toolkit")
    return dst


def render(chart: Path, release: str, namespace: str, values: Path | None = None) -> list:
    cmd = ["helm", "template", release, str(chart), "--namespace", namespace]
    if values is not None:
        cmd += ["--values", str(values)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"helm template {chart.name} failed; cannot validate capability roles:\n"
                 f"{r.stderr[:2000]}")
    return [d for d in yaml.safe_load_all(r.stdout) if isinstance(d, dict)]


def capability_objects(docs: list) -> dict:
    """{(kind, name): doc} for every rendered object labeled krateo.io/capability=true."""
    out = {}
    for d in docs:
        md = d.get("metadata") or {}
        if (md.get("labels") or {}).get("krateo.io/capability") == "true" and d.get("kind") in KINDS:
            out[(d["kind"], md.get("name"))] = d
    return out


def expected(values: dict) -> set:
    sc = values["serviceContract"]
    svc = sc["service"]["name"]
    want = set()
    for cap in sc["capabilities"]:
        name = f"{svc}-{cap['name']}"
        want.add(("ClusterRole", name))
        if cap.get("enforcement") == "abac":
            want.add(("ValidatingAdmissionPolicy", name))
            want.add(("ValidatingAdmissionPolicyBinding", name))
    return want


def check_standalone(objs: dict, want: set, bad: list) -> None:
    got = set(objs)
    for k in sorted(want - got):
        bad.append(f"per-cluster chart does not render {k[0]}/{k[1]}")
    for k in sorted(got - want):
        bad.append(f"per-cluster chart renders {k[0]}/{k[1]}, which no capability declares")
    for (kind, name), d in sorted(objs.items()):
        ann = (d.get("metadata") or {}).get("annotations") or {}
        if ann.get("helm.sh/resource-policy") != "keep":
            bad.append(f"{kind}/{name} lacks helm.sh/resource-policy: keep; the handoff from a "
                       "0.3.46 install deletes it when the umbrella stops owning it")
        if kind == "ClusterRole":
            labels = (d.get("metadata") or {}).get("labels") or {}
            found = [m.group(1) for m in (AGG.match(k) for k in labels) if m and labels[m.group(0)] == "true"]
            print(f"  ClusterRole/{name}: {', '.join(found) if found else '(none)'}")
            if not found:
                bad.append(f"ClusterRole/{name} aggregates to nothing: no aggregate-to label at all")
            for r in found:
                if r not in PREDEFINED:
                    bad.append(f"ClusterRole/{name} aggregates to '{r}', which is not a predefined role")
        elif kind == "ValidatingAdmissionPolicy":
            spec = d.get("spec") or {}
            print(f"  ValidatingAdmissionPolicy/{name}: failurePolicy={spec.get('failurePolicy')}")
            if spec.get("failurePolicy") != "Fail":
                bad.append(f"ValidatingAdmissionPolicy/{name} failurePolicy is "
                           f"{spec.get('failurePolicy')!r}, not Fail: an evaluation error admits")
            if not (spec.get("validations") or []):
                bad.append(f"ValidatingAdmissionPolicy/{name} has no validations")
            # `'k' in object.metadata.annotations` on an object with no annotations is a CEL
            # evaluation error, not false. failurePolicy Fail still denies, but the user sees
            # "no such key: annotations" instead of the capability's message, which is what a
            # portal shows. Measured on kind against 0.3.46's guard. Labels have the same shape.
            for v in spec.get("validations") or []:
                expr = v.get("expression", "")
                for m in sorted(set(re.findall(r"\bin\s+object\.metadata\.(annotations|labels)\b", expr))):
                    if f"has(object.metadata.{m})" not in expr:
                        bad.append(f"ValidatingAdmissionPolicy/{name}: `in object.metadata.{m}` "
                                   f"without has(object.metadata.{m}) errors on an object with no "
                                   f"{m}, so the deny carries the CEL error, not the message")
        else:
            spec = d.get("spec") or {}
            print(f"  ValidatingAdmissionPolicyBinding/{name}: policy={spec.get('policyName')} "
                  f"actions={spec.get('validationActions')}")
            if spec.get("policyName") != name:
                bad.append(f"ValidatingAdmissionPolicyBinding/{name} binds {spec.get('policyName')!r}")
            if "Deny" not in (spec.get("validationActions") or []):
                bad.append(f"ValidatingAdmissionPolicyBinding/{name} does not Deny")


def check_handoff(umbrella: Path, standalone: dict, want: set, td: Path, bad: list) -> None:
    """Drive capabilities.render in handoff mode with synthetic live objects."""
    probe = td / "probe"
    (probe / "templates").mkdir(parents=True)
    (probe / "Chart.yaml").write_text("apiVersion: v2\nname: probe\nversion: 0.0.0\n")
    shutil.copy(umbrella / "templates" / "_capabilities.tpl", probe / "templates")
    shutil.copy(umbrella / "values.yaml", probe / "values.yaml")
    (probe / "templates" / "probe.yaml").write_text(
        '{{- include "capabilities.render" (list $ "handoff" (.Values.probeLive | default dict)) }}\n')

    def live(owners: dict) -> dict:
        return {f"{k}/{n}": {"metadata": {"name": n, "annotations": {
            "meta.helm.sh/release-name": rel, "meta.helm.sh/release-namespace": ns}}}
            for (k, n), (rel, ns) in owners.items()}

    taken = {k: CAPS_RELEASE if k[0] == "ClusterRole" else OWNER_RELEASE for k in want}
    cases = [
        ("every object owned by this release (0.3.46 install, before the handoff)",
         {k: OWNER_RELEASE for k in want}, want),
        ("every object owned by openstack-capabilities (after the handoff)",
         {k: CAPS_RELEASE for k in want}, set()),
        ("no live object (fresh cluster)", {}, set()),
        ("same release name in another namespace",
         {k: ("openstack", "openstack2") for k in want}, set()),
        ("partial takeover: ClusterRoles moved, guard still owned here",
         taken, {k for k, o in taken.items() if o == OWNER_RELEASE}),
    ]
    for i, (label, owners, should) in enumerate(cases):
        vf = td / f"probe-{i}.yaml"
        vf.write_text(yaml.safe_dump({"probeLive": live(owners)}))
        got = capability_objects(render(probe, *OWNER_RELEASE, values=vf))
        ok = set(got) == should
        print(f"  handoff, {label}: renders {len(got)} (want {len(should)}) "
              f"{'ok' if ok else 'WRONG'}")
        if not ok:
            bad.append(f"handoff, {label}: renders {sorted(got)}, want {sorted(should)}")
        for k in set(got) & should:
            if got[k] != standalone.get(k):
                bad.append(f"handoff, {label}: {k[0]}/{k[1]} differs from the per-cluster "
                           "chart's copy, so the takeover would change the live object")


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    umbrella = root / "blueprints" / "openstack" / "chart"
    caps = root / "blueprints" / "openstack" / "capabilities"
    for p in (umbrella, caps):
        if not (p / "Chart.yaml").is_file():
            sys.exit(f"no chart at {p}: the capability objects must be rendered by the "
                     "per-cluster chart (blueprints/openstack/capabilities), so there is nothing "
                     "to validate -- a failed measurement, not a pass")
    bad = []

    print("generated inputs (hack/gen-capabilities-chart.py --check):")
    r = subprocess.run([sys.executable, str(root / "hack" / "gen-capabilities-chart.py"),
                        str(root), "--check"], capture_output=True, text=True)
    print("  " + (r.stdout.strip() or r.stderr.strip()).replace("\n", "\n  "))
    if r.returncode != 0:
        bad.append("the per-cluster chart's generated inputs differ from the umbrella's")

    values = yaml.safe_load((umbrella / "values.yaml").read_text())
    want = expected(values)
    if not want:
        sys.exit("the umbrella declares no capabilities -- nothing to check is a failed "
                 "measurement, not a pass")

    with tempfile.TemporaryDirectory() as tdn:
        td = Path(tdn)
        toolkit = root / "charts" / "helm-toolkit"

        print("\nper-cluster chart (blueprints/openstack/capabilities):")
        standalone = capability_objects(render(stage(caps, td / "caps"), *CAPS_RELEASE))
        if not standalone:
            sys.exit("the per-cluster chart rendered no capability objects at all -- the check "
                     "could not look, which is a failed measurement, not a pass")
        check_standalone(standalone, want, bad)

        print("\numbrella, rendered with no cluster:")
        in_umbrella = capability_objects(render(stage(umbrella, td / "umbrella", toolkit),
                                                *OWNER_RELEASE))
        print(f"  capability objects rendered: {len(in_umbrella)} (want 0)")
        for kind, name in sorted(in_umbrella):
            bad.append(f"the umbrella renders {kind}/{name}; it must come from the per-cluster "
                       "chart, or it exists only once an Openstack instance does")

        print("\numbrella handoff (capabilities.render, mode handoff):")
        check_handoff(umbrella, standalone, want, td, bad)

    # The umbrella must reach the objects only through the handoff, never standalone.
    for t in sorted((umbrella / "templates").glob("*")):
        text = t.read_text()
        if t.name != "_capabilities.tpl" and '"standalone"' in text:
            bad.append(f"umbrella template {t.name} renders capabilities in standalone mode")
    call = (umbrella / "templates" / "capability-handoff.yaml")
    if not call.is_file() or "capabilities.liveObjects" not in call.read_text():
        bad.append("umbrella templates/capability-handoff.yaml does not feed the handoff the "
                   "live objects (capabilities.liveObjects)")

    if bad:
        print(f"\nINVALID CAPABILITY OBJECTS: {len(bad)}")
        for b in bad:
            print(f"  {b}")
        print("\nAllowed aggregation roles: " + ", ".join(sorted(PREDEFINED)))
        return 1
    print(f"\nall {len(want)} capability objects come from the per-cluster chart, aggregate to "
          "predefined roles, carry keep, and hand over from the umbrella only while it owns them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
