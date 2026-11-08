#!/usr/bin/env python3
"""`profile: full-strict` must omit exactly the components that need provisioning, and `full`
must still include them.

Three of the 35 components cannot start on a Kubernetes cluster as it comes: ironic wants an
interface named `ironic-pxe`, octavia wants a Neutron port per host on its management network,
swift wants a ReadWriteMany StorageClass. Measured on a 4-node GKE install of profile=full: six
pods at 14 restarts in Init plus four Pending, none of which could ever become Ready there.

They are flagged `requiresProvisioning: true`. `profile: full-strict` skips the flagged ones;
`profile: full` is unchanged and still selects all 35, because taking components away from an
operator who HAS provisioned a PXE interface, an lb-mgmt-net or an RWX StorageClass would be a
behaviour change arriving through a patch release. This gate asserts all four directions:

  * every flagged component has an entry in hack/provisioning-preconditions.yaml, so a component
    cannot be quietly dropped from a profile without saying what it needs;
  * every entry names a flagged component, so a precondition cannot describe something no
    profile distinguishes -- which would read as a warning while nothing acts on it;
  * profile=full-strict does NOT select any flagged component, RENDERED rather than inferred
    from the template text -- otherwise the flag is data nothing reads;
  * profile=full DOES select all of them, so the opt-in cannot quietly become the default.

The one-directional version would have been the easy one to write and would have let the set rot
in whichever direction nobody was looking at. The third and fourth are rendered because the
first version of this gate checked for the strings `requiresProvisioning` and the component
names in the template, and was satisfied by its own explanatory comment. Each entry must also
carry a `symptom` and a `check`: the symptom because every one of these fails as `Init:` forever,
which a pod census can only count, and the check because a precondition an operator cannot test
is one they will rediscover instead.
"""
import os
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    sys.exit("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 \
    else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UMBRELLA = os.path.join(ROOT, "blueprints", "openstack", "chart", "values.yaml")
DECL = os.path.join(ROOT, "hack", "provisioning-preconditions.yaml")
HELPERS = os.path.join(ROOT, "blueprints", "openstack", "chart", "templates", "_helpers.tpl")



def selection_for_profile(profile):
    """What osh.selected actually returns for a profile -- rendered, not inferred from text."""
    tmp = tempfile.mkdtemp()
    try:
        dst = os.path.join(tmp, "chart")
        shutil.copytree(os.path.join(ROOT, "blueprints", "openstack", "chart"), dst)
        for name, pat, rep in (("Chart.yaml", "CHART_VERSION", "0.0.0-gate"),):
            f = os.path.join(dst, name)
            body = open(f).read()
            open(f, "w").write(body.replace(pat, rep))
        tk = os.path.join(ROOT, "charts", "helm-toolkit")
        if os.path.isdir(tk):
            os.makedirs(os.path.join(dst, "charts"), exist_ok=True)
            shutil.copytree(tk, os.path.join(dst, "charts", "helm-toolkit"))
        probe = os.path.join(dst, "templates", "zz-selection-probe.yaml")
        open(probe, "w").write(
            "#SELECTED {{ include \"osh.selected\" (list $) }}\n")
        p = subprocess.run(["helm", "template", "gate", dst, "--set", f"profile={profile}"],
                           capture_output=True, text=True)
        if p.returncode:
            return None
        for line in p.stdout.splitlines():
            if line.startswith("#SELECTED "):
                return set(line.split()[1:])
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    for path in (UMBRELLA, DECL, HELPERS):
        if not os.path.isfile(path):
            sys.exit(f"FAIL: missing {path} -- this gate refuses to run without it")

    comps = (yaml.safe_load(open(UMBRELLA)) or {}).get("components") or []
    if not comps:
        sys.exit("FAIL: the umbrella declares no components -- nothing was measured")
    flagged = {c["name"] for c in comps if c.get("requiresProvisioning")}

    decl = (yaml.safe_load(open(DECL)) or {}).get("preconditions") or []
    if not decl:
        sys.exit("FAIL: no preconditions declared -- either nothing is excluded from the "
                 "profiles, in which case remove the flag and this gate, or the declaration "
                 "has been emptied")
    declared = {}
    for e in decl:
        for k in ("component", "requires", "symptom", "check", "issue"):
            if not e.get(k):
                sys.exit(f"FAIL: a precondition entry is missing {k!r}: "
                         f"{e.get('component', '<unnamed>')}")
        declared[e["component"]] = e

    problems = []
    for name in sorted(flagged - set(declared)):
        problems.append(
            f"{name} is flagged requiresProvisioning and has no entry in "
            f"hack/provisioning-preconditions.yaml. It has been removed from every profile "
            f"without saying what an operator must provide to use it."
        )
    for name in sorted(set(declared) - flagged):
        problems.append(
            f"{name} has a declared provisioning precondition but is NOT flagged "
            f"requiresProvisioning, so the profiles still install it. The declaration reads as "
            f"a warning while the component still ships by default."
        )
    known = {c["name"] for c in comps}
    for name in sorted(set(declared) - known):
        problems.append(f"{name} has a precondition but is not a component of this umbrella")

    # The exclusion has to BE IN EFFECT, not merely mentioned. The first version of this check
    # grepped _helpers.tpl for "requiresProvisioning" -- which the explanatory comment above the
    # selector also contains, so deleting the guard and keeping the comment passed. Worse, a
    # rename to requiresProvisioningXX passed too, because a substring match cannot tell a
    # mention from a use. The only check that cannot be fooled is rendering the chart and asking
    # what it selected, so that is what this does.
    # Both halves of the contract, each rendered rather than inferred. `full` is unchanged and
    # must still select the three; `full-strict` must not. Asserting only the second would let
    # someone "simplify" by excluding them from `full` too -- which is the surprise-in-a-patch-
    # release this was deliberately designed to avoid.
    full = selection_for_profile("full")
    strict = selection_for_profile("full-strict")
    if full is None or strict is None:
        problems.append("could not render the umbrella for both profiles, so neither half of "
                        "the contract was verified -- a flag nothing reads selects everything")
    else:
        if not full or not strict:
            problems.append("a profile selected NOTHING, so this comparison proves nothing")
        missing_from_full = sorted(flagged - full)
        if missing_from_full:
            problems.append(
                "profile=full does NOT select " + ", ".join(missing_from_full) + ". `full` is "
                "unchanged on purpose: anyone who has provisioned what these need must keep "
                "getting them, and removing them from `full` is a behaviour change delivered "
                "through a patch release."
            )
        still_in_strict = sorted(flagged & strict)
        if still_in_strict:
            problems.append(
                "profile=full-strict still selects " + ", ".join(still_in_strict) + " despite "
                "the requiresProvisioning flag, so the flag is data that nothing reads"
            )
        if strict >= full:
            problems.append(
                f"full-strict selects {len(strict)} and full selects {len(full)}: full-strict "
                "is supposed to be the SMALLER set. If they are equal the distinction has "
                "collapsed and one of the two profiles is pointless."
            )

    if problems:
        print(f"FAIL: {len(problems)} problem(s) with the provisioning preconditions "
              f"({len(flagged)} flagged, {len(declared)} declared, {len(comps)} components)")
        for p in sorted(set(problems)):
            print("  " + p)
        return 1
    print(f"OK: profile=full selects all {len(full)}; profile=full-strict selects "
          f"{len(strict)}, excluding {', '.join(sorted(flagged))}, and each of those "
          f"declares what it needs, its symptom and a check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
