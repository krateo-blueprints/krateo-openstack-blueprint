#!/usr/bin/env python3
"""`profile: full-strict` must omit exactly the components whose requirements the install does not
provide, and `full` must still include them.

Four of the 36 components cannot start on a Kubernetes cluster as it comes: cinder wants a
volume backend (its only shipped one is Ceph, #196), ironic wants an interface named
`ironic-pxe`, octavia wants a Neutron port per host on its management network, swift wants a
ReadWriteMany StorageClass. Measured on a 4-node GKE install of profile=full: six pods at 14
restarts in Init plus four Pending, none of which could ever become Ready there.

Each lists what it needs under components[].requires, a capability (block-storage, pxe-interface,
lb-mgmt-net, rwx-storage-class). `profile: full-strict` selects a component only when the install
provides every capability it requires; `profile: full` is unchanged and still selects all 36,
because taking components away from an operator who HAS provisioned what they need would be a
behaviour change arriving through a patch release. One capability is provided by the umbrella
itself: block-storage, by storage.backend when it is not none (#196). This gate asserts:

  * every component that requires something has an entry in hack/provisioning-preconditions.yaml,
    and the entry's `capability` is exactly what the component requires, so a component cannot be
    quietly dropped from a profile without saying what it needs, nor require a capability (a
    typo) that nothing declares and nothing could ever provide;
  * every entry names a component that requires its capability, so a precondition cannot describe
    something no profile distinguishes -- which would read as a warning while nothing acts on it;
  * with storage.backend none, profile=full-strict does NOT select any of them, RENDERED rather
    than inferred from the template text -- otherwise `requires` is data nothing reads;
  * profile=full DOES select all of them, so the opt-in cannot quietly become the default;
  * THE POSITIVE CONTROL: for every other storage.backend the schema accepts, osh.providedCapabilities
    answers block-storage and full-strict selects exactly the none selection plus the components
    that require block-storage alone -- and still none of the others. Without it, a selector that
    ignored `requires` altogether and excluded the four by name would pass every check above.

The selections are read through the osh.selected helper by a probe template: an offline render of
the umbrella emits no component (no CRD is served), so the emitted manifest says nothing about what
was selected. The third and fourth checks are rendered because the first version of this gate
checked for the strings `requiresProvisioning` and the component names in the template, and was
satisfied by its own explanatory comment. Each entry must also carry a `symptom` and a `check`: the
symptom because every one of these fails as `Init:` forever (or, for cinder now, as a render error),
which a pod census can only count, and the check because a precondition an operator cannot test is
one they will rediscover instead.
"""
import json
import os
import re
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



def render_probe(values):
    """(selected, provided) as osh.selected and osh.providedCapabilities return them for `values`,
    rendered, not inferred from text. None when the umbrella does not render."""
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
            "#SELECTED {{ include \"osh.selected\" (list $) }}\n"
            "#PROVIDED {{ include \"osh.providedCapabilities\" (list $) }}\n")
        vf = os.path.join(tmp, "values.yaml")
        open(vf, "w").write(yaml.safe_dump(values))
        p = subprocess.run(["helm", "template", "gate", dst, "-f", vf], capture_output=True, text=True)
        if p.returncode:
            print(f"  (render with {values} failed: {p.stderr.strip()[:300]})")
            return None
        sel = prov = None
        for line in p.stdout.splitlines():
            if line.startswith("#SELECTED"):
                sel = set(line.split()[1:])
            if line.startswith("#PROVIDED"):
                prov = set(line.split()[1:])
        if sel is None or prov is None:
            return None
        return sel, prov
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def storage_values(backend):
    """storage values that render with `backend`: ceph-external names its source Secrets or is refused (#308), so the
    selection probe gives it placeholders; they select nothing."""
    v = {"backend": backend}
    if backend == "ceph-external":
        v["ceph"] = {"keys": {k: {"secret": "gate-" + k, "key": "key"} for k in ("cinder", "glance", "monHost")}}
    return v


def selection_for_profile(profile, backend="none"):
    r = render_probe({"profile": profile, "storage": storage_values(backend)})
    return None if r is None else r[0]


def main():
    for path in (UMBRELLA, DECL, HELPERS):
        if not os.path.isfile(path):
            sys.exit(f"FAIL: missing {path} -- this gate refuses to run without it")

    comps = (yaml.safe_load(open(UMBRELLA)) or {}).get("components") or []
    if not comps:
        sys.exit("FAIL: the umbrella declares no components -- nothing was measured")
    requires = {c["name"]: set(c.get("requires") or []) for c in comps if c.get("requires")}
    flagged = set(requires)
    if any("requiresProvisioning" in c for c in comps):
        sys.exit("FAIL: a component still carries requiresProvisioning, which nothing reads since #196: "
                 "list what it needs under `requires` instead")

    decl = (yaml.safe_load(open(DECL)) or {}).get("preconditions") or []
    if not decl:
        sys.exit("FAIL: no preconditions declared -- either nothing is excluded from the "
                 "profiles, in which case remove the flag and this gate, or the declaration "
                 "has been emptied")
    declared = {}
    for e in decl:
        for k in ("component", "capability", "requires", "symptom", "check", "issue"):
            if not e.get(k):
                sys.exit(f"FAIL: a precondition entry is missing {k!r}: "
                         f"{e.get('component', '<unnamed>')}")
        declared[e["component"]] = e

    problems = []
    for name in sorted(flagged - set(declared)):
        problems.append(
            f"{name} requires {sorted(requires[name])} and has no entry in "
            f"hack/provisioning-preconditions.yaml. It has been removed from full-strict "
            f"without saying what an operator must provide to use it."
        )
    for name in sorted(set(declared) - flagged):
        problems.append(
            f"{name} has a declared provisioning precondition but requires nothing, so the "
            f"profiles still install it. The declaration reads as a warning while the component "
            f"still ships by default."
        )
    for name in sorted(flagged & set(declared)):
        if requires[name] != {declared[name]["capability"]}:
            problems.append(
                f"{name} requires {sorted(requires[name])} but its precondition declares the "
                f"capability {declared[name]['capability']!r}: a capability no entry declares is "
                f"one nothing can ever provide, so the component could never be selected"
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
    # must still select every flagged component; `full-strict` must not. Asserting only the second would let
    # someone "simplify" by excluding them from `full` too -- which is the surprise-in-a-patch-
    # release this was deliberately designed to avoid.
    full = selection_for_profile("full")
    strict = selection_for_profile("full-strict")
    none_probe = render_probe({"profile": "full-strict", "storage": {"backend": "none"}})
    if none_probe is not None and none_probe[1]:
        problems.append(f"with storage.backend none, osh.providedCapabilities answers {sorted(none_probe[1])}; "
                        "none provides nothing")
    # THE POSITIVE CONTROL. Every backend the schema accepts other than none provides block-storage, and with it
    # full-strict selects exactly what it selected with none plus the components that require block-storage alone.
    schema = json.load(open(os.path.join(ROOT, "blueprints", "openstack", "chart", "values.schema.json")))
    backends = ((((schema.get("properties") or {}).get("storage") or {}).get("properties") or {})
                .get("backend") or {}).get("enum") or []
    others = [b for b in backends if b != "none"]
    block = {n for n, r in requires.items() if r == {"block-storage"}}
    if "none" not in backends or not others:
        problems.append(f"the umbrella schema's storage.backend enum is {backends}: it must hold none and at least "
                        "one backend, or the positive control below has nothing to render")
    if not block:
        problems.append("no component requires block-storage alone, so storage.backend selects nothing and the "
                        "positive control proves nothing")
    controls = 0
    for b in others:
        r = render_probe({"profile": "full-strict", "storage": storage_values(b)})
        if r is None or strict is None:
            problems.append(f"could not render full-strict with storage.backend {b}, so the positive control "
                            "did not run")
            continue
        sel_b, prov_b = r
        if prov_b != {"block-storage"}:
            problems.append(f"with storage.backend {b}, osh.providedCapabilities answers {sorted(prov_b)}, "
                            "want exactly block-storage")
        if sel_b != strict | block:
            problems.append(
                f"with storage.backend {b}, full-strict selects {len(sel_b)}: missing "
                f"{sorted((strict | block) - sel_b)}, extra {sorted(sel_b - (strict | block))}. It must be the none "
                f"selection plus {sorted(block)}: a backend provides block-storage and nothing else")
        full_b = selection_for_profile("full", b)
        if full is not None and full_b != full:
            problems.append(f"profile=full selects differently with storage.backend {b}: full selects every "
                            "component whatever the install provides")
        ident_b = selection_for_profile("identity", b)
        if ident_b is None or ident_b & flagged:
            problems.append(f"profile=identity with storage.backend {b} selects {sorted((ident_b or set()) & flagged)}"
                            " or does not render: a backend must not widen the identity profile")
        controls += 1
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
                "what they require with storage.backend none, so `requires` is data that nothing reads"
            )
        if strict >= full:
            problems.append(
                f"full-strict selects {len(strict)} and full selects {len(full)}: full-strict "
                "is supposed to be the SMALLER set. If they are equal the distinction has "
                "collapsed and one of the two profiles is pointless."
            )
        # The README states the full-strict count in its example and names every left-out
        # component in its table. Both drifted once: "33 components" and "minus those three"
        # survived cinder joining the list until this check (#204). Rendered count and flagged
        # set are the truth; the prose has to match them.
        readme = os.path.join(ROOT, "README.md")
        text = open(readme).read() if os.path.exists(readme) else ""
        m = re.search(r"profile: full-strict\s+#\s*(\d+) components", text)
        if not m:
            problems.append("README.md has no `profile: full-strict   # N components` example line, "
                            "so the documented count is not checked")
        elif int(m.group(1)) != len(strict):
            problems.append(f"README.md says full-strict is {m.group(1)} components; it renders "
                            f"{len(strict)}")
        for c in sorted(flagged):
            if not re.search(r"^\| `" + re.escape(c) + r"` \|", text, re.M):
                problems.append(f"README.md's full-strict table has no row for `{c}`, which "
                                "full-strict leaves out")

    if problems:
        print(f"FAIL: {len(problems)} problem(s) with the provisioning preconditions "
              f"({len(flagged)} flagged, {len(declared)} declared, {len(comps)} components)")
        for p in sorted(set(problems)):
            print("  " + p)
        return 1
    print(f"OK: profile=full selects all {len(full)}; profile=full-strict selects {len(strict)} with "
          f"storage.backend none, excluding {', '.join(sorted(flagged))}, and {len(strict | block)} with each of "
          f"{controls} other backend(s), adding {', '.join(sorted(block))}; each declares what it needs, its "
          f"symptom and a check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
