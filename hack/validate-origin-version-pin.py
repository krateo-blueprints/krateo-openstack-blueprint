#!/usr/bin/env python3
"""An existing Composition must be emitted at the version it was BORN at, not at this chart's.

The umbrella derives the composition apiVersion from the chart version, so until #303 a chart
bump changed every emitted object's version. This gate was written believing helm deletes and
recreates on that change. It does not: helm matches on kind and group, and a version-only move
kept the UID when measured. The d3 split below was the prune in #166. See osh.pinVersion, which
says what the pin still does and leaves its value open.

When a CR is deleted and re-created, the replacement gets a NEW UID; core-provider's
CalculateReleaseName is `<CR name>-<hash of UID>`; so the component acquires a SECOND helm release
beside the one whose workloads are already running. That second release's Deployments carry its
own name in spec.selector (release_group, app.kubernetes.io/instance) over objects that already
exist under the first release's name, and Kubernetes forbids changing a selector -- so its every
revision fails, and keeps failing, forever.

Measured on a0-openstack-d3 after a 0.3.28 bump, 2026-11-03:

    composition CRs for mariadb : 1, uid 5eb8f7dd-a1e1-476c-8d88-0740ca127734,
                                  creationTimestamp AFTER the bump,
                                  apiVersion composition.krateo.io/v0-3-28
    helm releases for mariadb   : TWO -- mariadb-92wd4czz (rev 4,5,6, all FAILED, the one the
                                  surviving CR owns) and mariadb-9c847c79 (rev 2 DEPLOYED,
                                  chart mariadb-0.3.28, the one actually running the database)

So the umbrella pins: a Composition that already exists and carries core-provider's
krateo.io/composition-origin-version stamp is emitted at THAT version, and only a new one is
emitted at the current version. The GVK then stops moving, helm patches in place, the UID
survives and the release name never changes. This asserts that mechanism, in six parts.

 1. THE PIN IS WIRED IN, AT THE ONE PLACE THAT MATTERS. templates/compositions.yaml must emit
    the Composition apiVersion at $emitVer, the version osh.emitVersion resolved for the gate. A
    call site that reverts to osh.apiVersion renders perfectly and silently stops pinning.

 2. THE ANNOTATION KEY IS THE ENGINE'S, AND LIVES IN ONE PLACE. osh.originVersionAnnotation
    must be exactly krateo.io/composition-origin-version -- core-provider
    pkg/meta.AnnotationKeyOriginVersion, confirmed against its source and against a live CR on
    a0-openstack-d3 -- and no template may spell that key anywhere else. It is an agreement
    with another component: a rename on either side raises no error anywhere, the umbrella
    simply stops finding a stamp, falls back to the current version for every component, and
    silently stops pinning.

 2b. THE STAMP IS ACTUALLY READ -- and this part is STRUCTURAL, which is a limitation and not a
    style choice. It is the one link with no offline behaviour: `lookup` returns nothing under
    `helm template`, so an osh.originVersion gutted to return "" answers exactly what the
    correct helper answers with no cluster. Every probe in part 3 still passes, the
    byte-identity differential in part 5 still passes, and against a real cluster nothing is
    ever pinned. That hole was found by differential-testing THIS gate -- an earlier version of
    it did not notice the mutation -- and it is the only reason the part exists. What is
    asserted is that the value the helper returns is assigned from the looked-up object's
    annotations indexed by osh.originVersionAnnotation, and that the lookup is one named GET of
    the helper's own $name. The behavioural proof needs an apiserver and lives in the pull
    request: `helm template --dry-run=server` against a live cluster emitted a CR stamped
    v0-3-28 at v0-3-28, and at v0-3-30 once the stamp was removed.

 3. THE DECISION IS CORRECT, EXERCISED WITH SYNTHETIC INPUT. osh.pinVersion is a pure function
    of (current, origin, kind, CRD list), so a probe template calls it with fabricated CRD
    lists and asserts every branch:

      origin stamped, older, still served   -> the ORIGIN version        (the pin fires)
      origin stamped and equal to current   -> the current version       (nothing to pin)
      origin stamped but NO LONGER SERVED   -> the current version       (see below)
      no origin stamp at all                -> the current version       (new, or pre-stamp)

    The third is the one that is easy to get wrong in the dangerous direction. Emitting at a
    version the CRD no longer serves does not degrade quietly -- it 500s the render with "no
    matches for kind", on every reconcile, and a release that stops rendering gets uninstalled
    (#68). A reinstated wedge is recoverable by hand; that is not.

 4. NOTHING NEW IS READ FROM THE CLUSTER. Every `lookup` in the chart is classified by its
    name argument: a named GET of one object (cheap, blessed) or a LIST of everything of a kind
    (expensive). Every list-shaped lookup must sit in compositions.yaml, above the `range` over
    components -- there are exactly two, the CRD list and the node list -- and _helpers.tpl,
    where the per-component helpers live, must contain none at all. osh.crdExists carries the
    measurement: with the CRD fetch inside it, a 0.3.28 upgrade against a 257-CRD cluster spent
    13 MINUTES rendering, and composition-dynamic-controller renders under a timeout, so that
    wedges the umbrella rather than merely delaying it.

 5. THE OFFLINE RENDER IS UNCHANGED, PROVED DIFFERENTIALLY. `lookup` returns nothing without a
    cluster, so no stamp is ever found and every component must fall back to the current
    version. This builds the counterfactual chart -- the same tree with the call site put back
    to the unpinned osh.apiVersion -- renders both in four selections, and asserts the bytes
    are identical. Not "the renders look right": the pinned chart and the chart without the
    feature must produce the same bytes with no cluster, or the feature is changing CI, the
    release artifacts and every `helm template` anyone runs.

 6. THE COST IS DOCUMENTED WHERE AN OPERATOR WILL LOOK. Pinning is not free, and its cost is
    silent. Reading and writing through an older version means reading and writing through that
    version's schema, and a narrower schema PRUNES -- reads included, which is counter-
    intuitive and measured: core-provider's fieldloss package, verified against kind v1.36.4,
    "READS through a narrow endpoint are pruned too, not only writes." So any `values` property
    this chart adds after a pinned component's origin version is SILENTLY DROPPED for that
    component. Not rejected, not warned about: dropped. The only symptom is that the setting
    does nothing. And the pin holds only while the origin version stays unpruned, which rests
    on a coincidence -- the krateo.io/composition-version label is re-stamped on every write to
    request.requestKind.version, so writing through the origin endpoint keeps re-asserting that
    the version has instances -- that approving instance migration removes. Both facts must
    appear in the README and at the call site, or the one person who needs them cannot find
    them.

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
TEMPLATES = CHART / "templates"
UMBRELLA = CHART / "values.yaml"
COMPOSITIONS = TEMPLATES / "compositions.yaml"
HELPERS = TEMPLATES / "_helpers.tpl"
README = ROOT / "README.md"

# core-provider pkg/meta.AnnotationKeyOriginVersion. Confirmed twice: in its source
# (go/composition-dynamic-controller/pkg/meta/meta.go, where SetOriginVersion writes it
# write-once at first reconcile) and on a live CR --
#   kubectl -n openstack get mariadb mariadb -o jsonpath='{.metadata.annotations}'
#   {"krateo.io/composition-origin-version":"v0-3-28", ...}
# on a0-openstack-d3.
ORIGIN_ANNOTATION = "krateo.io/composition-origin-version"

# The only two full-list cluster reads this chart is allowed, both hoisted above the component
# loop in compositions.yaml. Anything else list-shaped is the 13-minute mistake.
ALLOWED_LIST_LOOKUPS = {
    ("apiextensions.k8s.io/v1", "CustomResourceDefinition"),
    ("v1", "Node"),
}

# The four selections the install paths actually use, exactly as the sibling gates exercise
# them. A check that only ever sees the default profile only ever enforces that profile.
SELECTIONS = (
    ("profile=full", ("--set", "profile=full")),
    ("profile=identity", ("--set", "profile=identity")),
    ("enabled={heat}", ("--set", "enabled={heat}")),
    ("enabled={nova,neutron}", ("--set", "enabled={nova,neutron}")),
)

# A plausible bumped chart version. The umbrella's values carry the CHART_VERSION placeholder,
# which the release stamps from the git tag, and "vCHART_VERSION" is not a version anything can
# reason about -- the pin is entirely about which of two versions is emitted.
CURRENT_CHART_VERSION = "0.3.30"
CURRENT = "v0-3-30"
OLDER = "v0-3-28"


def measurement_failed(msg):
    # Exit 2 and never 0. A gate that reports success without measuring anything is the same
    # bug as a probe that cannot fail (#120).
    print(f"FAILED MEASUREMENT: {msg}")
    sys.exit(2)


def helm_error(stderr):
    """The line of helm's stderr that says what actually went wrong.

    helm's LAST line is usually "Use --debug flag to render out invalid YAML", which names no
    cause at all; the "Error:" line above it carries the template and the parse or execution
    failure. A diagnostic that omits it costs the next person a debugging session.
    """
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith("Error:"):
            return ln[:400]
    return lines[-1][:400] if lines else "no stderr"


try:
    import yaml
except ImportError:
    measurement_failed("PyYAML unavailable, so no render of this chart was ever inspected")

if not shutil.which("helm"):
    measurement_failed("helm is not on PATH; every part below renders the chart")
if not CHART.is_dir():
    measurement_failed(f"no umbrella chart at {CHART} -- wrong path?")
if not TEMPLATES.is_dir():
    measurement_failed(f"no templates at {TEMPLATES}; there is nothing to check")
if not UMBRELLA.is_file():
    measurement_failed(f"no umbrella values at {UMBRELLA}")
if not COMPOSITIONS.is_file():
    measurement_failed(f"no {COMPOSITIONS}, which is where the pin has to be wired in")
if not HELPERS.is_file():
    measurement_failed(f"no {HELPERS}, which is where the pin lives")
if not README.is_file():
    measurement_failed(f"no {README}, which is where the pin's cost has to be documented")

try:
    umbrella = yaml.safe_load(UMBRELLA.read_text()) or {}
except yaml.YAMLError as e:
    measurement_failed(f"{UMBRELLA} does not parse -- {str(e)[:200]}")
if not isinstance(umbrella, dict):
    measurement_failed(f"{UMBRELLA} does not parse into a mapping")

comps = umbrella.get("components")
if not isinstance(comps, list) or not comps:
    measurement_failed("the umbrella declares no components, so nothing is ever emitted")
try:
    kinds = sorted({c["kind"] for c in comps})
except (TypeError, KeyError) as e:
    measurement_failed(f"a component in {UMBRELLA} has no kind -- {str(e)[:120]}")

comp_text = COMPOSITIONS.read_text()
helpers_text = HELPERS.read_text()
template_files = sorted(p for p in TEMPLATES.rglob("*") if p.is_file())
if not template_files:
    measurement_failed(f"no template files under {TEMPLATES}")

problems = []


def strip_comments(text):
    """The template text with {{/* ... */}} and {{- /* ... */ -}} comment blocks removed.

    Every part below reasons about what the chart DOES. These templates carry more prose than
    code -- deliberately -- and the prose names the very helpers and keys being searched for,
    so a check run over the raw text answers yes to questions whose answer is no.
    """
    return re.sub(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", "", text, flags=re.S)


comp_code = strip_comments(comp_text)
helpers_code = strip_comments(helpers_text)


# ---------------------------------------------------------------------------
# Part 1: the pin is wired in at the call site.
# ---------------------------------------------------------------------------
CALL_SITE = re.compile(
    r"^\s*apiVersion:\s*\{\{\s*include\s+\"(?P<helper>osh\.[A-Za-z]+)\"\s*"
    r"\(list\s+(?P<args>[^)]*(?:\([^)]*\)[^)]*)*)\)\s*\}\}\s*$",
    re.M,
)
call_sites = CALL_SITE.findall(comp_code)
if not call_sites:
    measurement_failed(
        "no `apiVersion: {{ include \"osh....\" (list ...) }}` line in compositions.yaml, so "
        "this gate cannot see which version Compositions are emitted at -- the one thing it "
        "exists to check. The call site was rewritten; rewrite this matcher with it.")
if len(call_sites) != 1:
    measurement_failed(
        f"{len(call_sites)} Composition apiVersion call sites in compositions.yaml; this gate "
        "assumes the one. A second emission path would be unchecked.")
helper, args = call_sites[0]
PINNED_HELPER = "osh.apiVersionAt"
if helper != PINNED_HELPER:
    problems.append(
        f"compositions.yaml emits the Composition apiVersion through `{helper}`, not "
        f"{PINNED_HELPER}. osh.apiVersion is unconditionally the CURRENT chart version, so it "
        "ignores the origin stamp and every decision osh.emitVersion made.")
else:
    want = ["$emitVer"]
    got = args.split()
    if got != want:
        problems.append(
            f"{PINNED_HELPER} is called with {got}, not {want}. The document must be emitted at "
            "the version the gate resolved. Resolving it a second time is a second read of the "
            "object and an answer that can differ from the gate's.")


# ---------------------------------------------------------------------------
# Part 2: the annotation key is the engine's, and spelled in exactly one place.
# ---------------------------------------------------------------------------
ANN_DEF = re.compile(
    r"\{\{-?\s*define\s+\"osh\.originVersionAnnotation\"\s*-?\}\}\s*(?P<key>[^\s{]+)\s*"
    r"\{\{-?\s*end\s*-?\}\}")
m = ANN_DEF.search(helpers_code)
if not m:
    problems.append(
        "no osh.originVersionAnnotation helper in _helpers.tpl. The origin annotation key is an "
        "agreement with core-provider and must be written once, where a rename can be checked "
        "against this gate, rather than inline at a lookup.")
elif m.group("key") != ORIGIN_ANNOTATION:
    problems.append(
        f"osh.originVersionAnnotation is {m.group('key')!r}, not {ORIGIN_ANNOTATION!r}. That is "
        "core-provider's pkg/meta.AnnotationKeyOriginVersion, confirmed in its source and on a "
        "live CR. A key that does not match means no stamp is ever found, every component falls "
        "back to the current version, and the pin silently does nothing at all.")

elsewhere = []
for p in template_files:
    text = strip_comments(p.read_text(errors="replace"))
    if p == HELPERS:
        text = ANN_DEF.sub("", text)
    if ORIGIN_ANNOTATION in text:
        elsewhere.append(p.relative_to(ROOT).as_posix())
if elsewhere:
    problems.append(
        f"{ORIGIN_ANNOTATION} is spelled literally in {', '.join(elsewhere)} as well as in "
        "osh.originVersionAnnotation. One spelling, one place: a second copy is a second thing "
        "to update when core-provider renames it, and the failure mode of missing one is "
        "silence.")


# ---------------------------------------------------------------------------
# Part 2b: osh.originVersion really READS the stamp. Structural, and that is a limitation.
#
# This is the one link in the chain with no offline behaviour to assert. `lookup` returns
# nothing under `helm template`, so a osh.originVersion gutted to `{{- $v = "" -}}` answers
# exactly what the correct helper answers with no cluster: the pin still looks right in every
# probe below, every decision case still passes, the offline render is still byte-identical --
# and against a real cluster nothing is ever pinned. That gap was
# found by differential-testing this gate (mutation "the annotation read is dropped" went
# unnoticed by an earlier version of it), which is the only reason it is covered at all.
#
# A gate cannot prove this one behaviourally without an apiserver, so it checks the structure
# and says so rather than implying more: the value the helper RETURNS must be assigned from an
# expression that indexes the looked-up object's annotations by osh.originVersionAnnotation.
# The behavioural proof is a `helm template --dry-run=server` run against a live apiserver,
# recorded in the pull request that introduced the pin -- a CR stamped v0-3-28 under a CRD
# serving v0-3-28 and v0-3-30 was emitted at v0-3-28, and at v0-3-30 once the stamp was removed.
# ---------------------------------------------------------------------------
def define_body(text, name):
    """The body of the `define` named <name>, up to the start of the next `define`.

    Deliberately NOT a regex to the matching {{- end -}}: these helpers nest `if` blocks, so a
    non-greedy match stops at the first inner `end` and silently hands back a truncated body --
    which is how an earlier draft of this part concluded that osh.originVersion returns nothing.
    Definitions in this file are top-level and sequential, so the next `define` is the boundary.
    """
    start = re.search(r"\{\{-?\s*define\s+\"" + re.escape(name) + r"\"\s*-?\}\}", text)
    if not start:
        return None
    rest = text[start.end():]
    nxt = re.search(r"\{\{-?\s*define\s+\"", rest)
    return rest[:nxt.start()] if nxt else rest


ob = define_body(helpers_code, "osh.originVersion")
if not ob:
    problems.append(
        "no osh.originVersion helper in _helpers.tpl. It is the only thing that reads the "
        "object's origin stamp; without it osh.pinVersion is a correct decision over an input "
        "nothing supplies.")
else:
    body = ob
    # The variable the helper emits as its result: the last bare {{- $x -}} in the body.
    emits = re.findall(r"\{\{-?\s*(\$[A-Za-z_]\w*)\s*-?\}\}", body)
    if not emits:
        problems.append(
            "osh.originVersion returns no variable, so whatever it reads it does not hand back.")
    else:
        ret = emits[-1]
        if not re.search(re.escape(ret) + r"\s*=[^\n]*osh\.originVersionAnnotation", body):
            problems.append(
                f"osh.originVersion returns {ret}, and no assignment to {ret} indexes anything "
                "by osh.originVersionAnnotation. Offline that is indistinguishable from the "
                "correct helper -- `lookup` finds nothing either way -- so every probe below "
                "still passes and the pin still never fires against a real cluster. This is "
                "the differential that an earlier version of this gate missed.")
    if "annotations" not in body:
        problems.append(
            "osh.originVersion never mentions annotations, so it is not reading core-provider's "
            "origin stamp. The stamp is an annotation, deliberately: nothing selects on it and "
            "it must not widen any informer's selector.")


# ---------------------------------------------------------------------------
# Part 3: exercise the decision with synthetic CRD lists.
# ---------------------------------------------------------------------------
PROBE = """{{- $crds := CRDS }}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: gate-probe
data:
  current: {{ include "osh.version" (list $) | quote }}
  annotation: {{ include "osh.originVersionAnnotation" $ | quote }}
  pin: {{ include "osh.pinVersion" (list (include "osh.version" (list $)) ORIGIN "KIND" $crds) | quote }}
  emit: {{ include "osh.apiVersionAt" (list (include "osh.emitVersion" (list $ "KIND" "NAME" $crds (include "osh.exists" (list $ "KIND" "NAME" $crds))))) | quote }}
"""


def crd_list_literal(kind, versions):
    """A Helm `list` of one fabricated composition CRD serving the given versions.

    Shaped exactly as `lookup "apiextensions.k8s.io/v1" "CustomResourceDefinition" "" ""`
    delivers them, because osh.crdExists reads .spec.group, .spec.names.kind and
    .spec.versions[].name/.served off that shape.
    """
    if not versions:
        return "list"
    vs = " ".join(
        f'(dict "name" {json.dumps(n)} "served" {"true" if served else "false"})'
        for n, served in versions)
    return ('list (dict "spec" (dict "group" "composition.krateo.io" '
            f'"names" (dict "kind" {json.dumps(kind)}) "versions" (list {vs})))')


# The probe chart is prepared ONCE and only its probe template is rewritten per call: copying
# the tree per decision took an earlier draft of the sibling node-label gate from seconds to
# minutes, and a gate slow enough to skip locally stops being run before a pull request.
_tmp = tempfile.mkdtemp(prefix="osh-origin-pin-gate-")
atexit.register(shutil.rmtree, _tmp, True)
PROBE_CHART = Path(_tmp) / "chart"
shutil.copytree(CHART, PROBE_CHART)
_cy = PROBE_CHART / "Chart.yaml"
_cy.write_text(_cy.read_text().replace("CHART_VERSION", CURRENT_CHART_VERSION))


def probe(origin, crds_literal, extra=()):
    """Render the REAL umbrella chart plus a probe template, with a synthetic CRD list.

    The chart is the shipped one, so the helpers under test are the shipped helpers and the
    real values supply the components; only the CRD list and the origin stamp are fabricated.
    compositions.yaml renders in the same pass with the empty list `lookup` hands it offline,
    so a failure can only come from the probe.
    """
    body = (PROBE
            .replace("CRDS", crds_literal)
            .replace("ORIGIN", json.dumps(origin))
            .replace("KIND", kinds[0])
            .replace("NAME", "gate-probe-object"))
    (PROBE_CHART / "templates/zz-gate-probe.yaml").write_text(body)
    try:
        return subprocess.run(
            ["helm", "template", "gate", str(PROBE_CHART), "--kube-version", "1.36.0",
             "--set", f"chartVersion={CURRENT_CHART_VERSION}", *extra],
            capture_output=True, text=True,
        )
    finally:
        (PROBE_CHART / "templates/zz-gate-probe.yaml").unlink(missing_ok=True)


def probe_data(origin, crds_literal, what):
    out = probe(origin, crds_literal)
    if out.returncode != 0:
        measurement_failed(f"the probe for {what} did not render -- {helm_error(out.stderr)}")
    try:
        docs = [d for d in yaml.safe_load_all(out.stdout) if d]
    except yaml.YAMLError as e:
        measurement_failed(f"the probe output for {what} does not parse -- {str(e)[:200]}")
    for d in docs:
        if d.get("metadata", {}).get("name") == "gate-probe":
            return d.get("data") or {}
    measurement_failed(f"the probe for {what} rendered no ConfigMap, so no decision was made")


BOTH_SERVED = crd_list_literal(kinds[0], ((OLDER, True), (CURRENT, True)))
ONLY_CURRENT = crd_list_literal(kinds[0], ((CURRENT, True),))
OLDER_UNSERVED = crd_list_literal(kinds[0], ((OLDER, False), (CURRENT, True)))

baseline = probe_data("", BOTH_SERVED, "the baseline")
if baseline.get("current") != CURRENT:
    measurement_failed(
        f"osh.version answers {baseline.get('current')!r} with chartVersion="
        f"{CURRENT_CHART_VERSION}, not {CURRENT!r}; every expectation below is stated in that "
        "form, so the probe is not measuring what it reads as measuring.")
if baseline.get("annotation") != ORIGIN_ANNOTATION:
    measurement_failed(
        "the rendered osh.originVersionAnnotation disagrees with this gate's constant, so the "
        "probe and the chart are not talking about the same key")

# (what, origin stamp, CRD list, expected version). Stated as a table so a branch that stops
# being exercised is visible as a missing row rather than as a check that quietly passes.
CASES = (
    ("an existing object stamped at an older, still-served version",
     OLDER, BOTH_SERVED, OLDER),
    ("an existing object stamped at the current version",
     CURRENT, BOTH_SERVED, CURRENT),
    ("an existing object whose stamped version the CRD no longer serves",
     OLDER, ONLY_CURRENT, CURRENT),
    ("an existing object whose stamped version is present but served: false",
     OLDER, OLDER_UNSERVED, CURRENT),
    ("an object with no origin stamp (new, or created before core-provider stamped)",
     "", BOTH_SERVED, CURRENT),
)
for what, origin, crds, want in CASES:
    data = probe_data(origin, crds, what)
    got = data.get("pin")
    if got != want:
        problems.append(
            f"osh.pinVersion emits {got!r} for {what}; it must emit {want!r}."
            + (" Emitting a version the CRD does not serve 500s the render with \"no matches "
               "for kind\" on every reconcile, and a release that stops rendering gets "
               "uninstalled (#68)." if want == CURRENT and origin and crds is not BOTH_SERVED
               else ""))
    # osh.emitVersion composes the lookup with the decision. Offline the lookup finds
    # nothing, so whatever the synthetic CRD list says, it must answer the current version --
    # the same property part 5 proves over the whole render.
    emit = data.get("emit")
    if emit != f"composition.krateo.io/{CURRENT}":
        problems.append(
            f"with no cluster, the emitted apiVersion is {emit!r} for {what}; it must be "
            f"'composition.krateo.io/{CURRENT}'. `lookup` returns nothing under `helm "
            "template`, so there is no stamp to honour and the only correct answer is the "
            "current version.")

if baseline.get("pin") != CURRENT:
    problems.append(
        "the unstamped case is the FALLBACK, and it is not the current version. Everything "
        "offline and every first install goes through it.")


# ---------------------------------------------------------------------------
# Part 4: no new cluster-wide lookup.
# ---------------------------------------------------------------------------
def lookup_args(text):
    """Every `lookup` call in text, as its four argument tokens.

    Hand-tokenized rather than regexed because the arguments are Helm expressions: quoted
    strings, $variables, and parenthesized (include ...) calls that contain spaces and quotes
    of their own.
    """
    calls = []
    for mo in re.finditer(r"\blookup\b", text):
        i = mo.end()
        args = []
        while len(args) < 4:
            while i < len(text) and text[i] in " \t":
                i += 1
            if i >= len(text) or text[i] in "\r\n":
                break
            if text[i] == '"':
                j = i + 1
                while j < len(text) and text[j] != '"':
                    j += 2 if text[j] == "\\" else 1
                args.append(text[i:j + 1])
                i = j + 1
            elif text[i] == "(":
                depth, j = 0, i
                while j < len(text):
                    if text[j] == "(":
                        depth += 1
                    elif text[j] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                args.append(text[i:j + 1])
                i = j + 1
            else:
                j = i
                while j < len(text) and text[j] not in " \t\r\n)}":
                    j += 1
                args.append(text[i:j])
                i = j
        calls.append(args)
    return calls


def unquote(tok):
    return tok[1:-1] if len(tok) >= 2 and tok[0] == '"' else tok


all_lookups = {}
for p in template_files:
    all_lookups[p] = lookup_args(strip_comments(p.read_text(errors="replace")))

for p, calls in all_lookups.items():
    rel = p.relative_to(ROOT).as_posix()
    for args in calls:
        if len(args) != 4:
            problems.append(
                f"{rel}: a `lookup` with {len(args)} argument tokens ({' '.join(args)}); this "
                "gate must be able to read every lookup's name argument to tell a named GET "
                "from a full-list fetch, and it cannot read this one.")

list_lookups = []
for p, calls in all_lookups.items():
    for args in calls:
        if len(args) == 4 and unquote(args[3]) == "":
            list_lookups.append((p, unquote(args[0]), unquote(args[1])))

if any(p == HELPERS for p, _, _ in list_lookups):
    problems.append(
        "_helpers.tpl contains a full-list `lookup`. Every helper in there is called once per "
        "component or once per dependency edge -- 154 times for profile=full -- so a list "
        "fetch inside one pulls the cluster's entire inventory that many times. With the CRD "
        "fetch in osh.crdExists, a 0.3.28 upgrade on a 257-CRD cluster spent 13 MINUTES "
        "rendering, and composition-dynamic-controller renders under a timeout: that wedges "
        "the umbrella rather than delaying it. The list belongs in compositions.yaml, hoisted "
        "above the component loop and threaded in as an argument.")

for p, group, kind in list_lookups:
    if p != COMPOSITIONS:
        problems.append(
            f"{p.relative_to(ROOT).as_posix()}: full-list `lookup` of {group}/{kind}. Every "
            "cluster-wide read must live in compositions.yaml above the component range, where "
            "its cost is paid once per render and is visible at the call site.")
    if (group, kind) not in ALLOWED_LIST_LOOKUPS:
        problems.append(
            f"new full-list `lookup` of {group}/{kind}. The pin was specifically not allowed to "
            "add one: it reads the one object it needs with a named GET. If a new cluster-wide "
            "read is genuinely required, hoist it above the component range, thread it through "
            "as an argument, and add it here with the measurement that justifies it.")

range_at = comp_code.find("range $c := .Values.components")
if range_at < 0:
    measurement_failed(
        "compositions.yaml has no `range $c := .Values.components`, so this gate cannot tell "
        "which lookups are inside the component loop -- the question part 4 exists to answer.")
for mo in re.finditer(r"\blookup\b", comp_code):
    args = lookup_args(comp_code[mo.start():])[0]
    if len(args) == 4 and unquote(args[3]) == "" and mo.start() > range_at:
        problems.append(
            f"compositions.yaml performs a full-list `lookup` of {unquote(args[0])}/"
            f"{unquote(args[1])} INSIDE the component range. That is one full fetch per "
            "selected component -- 34 for profile=full -- which is the 13-minute render "
            "osh.crdExists records. Hoist it above the range.")

if ob:
    origin_calls = lookup_args(ob)
    if len(origin_calls) != 1:
        problems.append(
            f"osh.originVersion performs {len(origin_calls)} `lookup` calls; it must perform "
            "exactly one, a named GET of the one Composition whose stamp it needs. It is called "
            "once per selected component, so anything more is multiplied by 34.")
    elif len(origin_calls[0]) == 4 and unquote(origin_calls[0][3]) == "":
        problems.append(
            "osh.originVersion's `lookup` has an empty name argument, so it LISTS every "
            "Composition of that kind instead of getting the one it needs -- once per selected "
            "component. Pass the component's name.")
    elif len(origin_calls[0]) == 4 and origin_calls[0][3] != "$name":
        problems.append(
            f"osh.originVersion looks up {origin_calls[0][3]} rather than its own $name "
            "parameter, so the stamp it reads is not necessarily the stamp of the object being "
            "emitted. Pinning the wrong object's origin is worse than not pinning.")

named_in_helpers = sum(
    1 for args in all_lookups[HELPERS] if len(args) == 4 and unquote(args[3]) != "")
if named_in_helpers == 0:
    measurement_failed(
        "_helpers.tpl performs no named `lookup` at all, so nothing reads the cluster per "
        "object -- osh.exists, osh.ready and osh.originVersion cannot be doing their jobs and "
        "this gate is measuring a chart that no longer has the mechanism it checks.")


# ---------------------------------------------------------------------------
# Part 5: the offline render is byte-identical to the same chart without the pin.
# ---------------------------------------------------------------------------
def render_all(chart_dir, label):
    """Render the four selections of a chart into a freshly created empty directory."""
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "chart"
        shutil.copytree(chart_dir, work)
        cy = work / "Chart.yaml"
        cy.write_text(cy.read_text().replace("CHART_VERSION", CURRENT_CHART_VERSION))
        for name, extra in SELECTIONS:
            r = subprocess.run(
                ["helm", "template", "gate", str(work), "--kube-version", "1.36.0", *extra],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                measurement_failed(
                    f"the {label} chart does not render {name} -- {helm_error(r.stderr)}")
            out[name] = r.stdout
    return out


UNPINNED_CALL = 'apiVersion: {{ include "osh.apiVersion" (list $) }}'
if helper != PINNED_HELPER:
    # Part 1 has already reported the call site as unpinned. There is no pin to compare a
    # counterfactual against, and substituting the unpinned call for itself would produce a
    # check that cannot fail -- which is a worse outcome than not running it and saying so.
    problems.append(
        "part 5 (the offline byte-identity differential) was NOT run: the call site is already "
        "the unpinned helper, so there is no counterfactual to build. Fix the call site and "
        "re-run; the byte-identity property is unmeasured until then.")
else:
    with tempfile.TemporaryDirectory() as _cf:
        counterfactual = Path(_cf) / "chart"
        shutil.copytree(CHART, counterfactual)
        cf_comp = counterfactual / "templates/compositions.yaml"
        before = cf_comp.read_text()
        pinned_call = CALL_SITE.search(strip_comments(before))
        after = before.replace(pinned_call.group(0).strip(), UNPINNED_CALL)
        if after == before:
            measurement_failed(
                "could not build the unpinned counterfactual: the call site this gate found in "
                "compositions.yaml is not substitutable, so the byte-identity claim in part 5 "
                "would be made against the pinned chart compared with itself -- a check that "
                "cannot fail. Fix the substitution, do not relax it.")
        cf_comp.write_text(after)
        pinned = render_all(CHART, "pinned")
        unpinned = render_all(counterfactual, "unpinned counterfactual")

    for name, _ in SELECTIONS:
        if pinned[name] != unpinned[name]:
            problems.append(
                f"with NO CLUSTER, the pinned chart and the same chart without the pin render "
                f"{name} differently. `lookup` finds nothing offline, so there is no stamp to "
                "honour and the two must agree byte for byte. A difference means the pin "
                "changed CI's renders, the release's stamped example manifests and every "
                "`helm template` an operator runs, none of which has a cluster to read.")


# ---------------------------------------------------------------------------
# Part 6: the cost is documented where an operator will look for it.
# ---------------------------------------------------------------------------
# What must be findable, and why each one. An operator hits exactly two surprises: a value they
# set that does nothing, and a migration setting that quietly undoes the pin. Both are
# unguessable from the symptom, so both have to be written down in the place they will search.
README_MUST_MENTION = (
    (ORIGIN_ANNOTATION,
     "the annotation that decides which version a component is emitted at -- the one thing to "
     "look at on the object when a value appears to do nothing"),
    ("prune",
     "that an older version's narrower schema DROPS values properties added after it, silently"),
    ("krateo.io/upgrade-to-version",
     "the migration approval that flips the composition-version label and makes the pinned "
     "version prunable, which defeats the pin"),
)
readme = README.read_text()
for needle, why in README_MUST_MENTION:
    if needle not in readme:
        problems.append(
            f"README.md never mentions {needle!r}: {why}. The pin's costs are SILENT -- a "
            "dropped values property produces no error and no event, only a setting that does "
            "nothing -- so documentation is the only way anyone finds them.")

CALL_SITE_MUST_MENTION = (
    ("prune", "that a pinned component's older schema drops newer values properties"),
    ("upgrade-to-version", "the migration approval that defeats the pin"),
)
for needle, why in CALL_SITE_MUST_MENTION:
    if needle not in comp_text:
        problems.append(
            f"templates/compositions.yaml never mentions {needle!r}: {why}. The next person to "
            "change this template reads the template, and the cost of the pin is not derivable "
            "from the code that implements it.")


# ---------------------------------------------------------------------------
print(f"origin-version pin: {len(kinds)} component kinds, "
      f"{sum(len(v) for v in all_lookups.values())} lookups "
      f"({len(list_lookups)} full-list, {named_in_helpers} named in _helpers.tpl), "
      f"{len(CASES)} decisions exercised, {len(SELECTIONS)} selections rendered twice")
if problems:
    print()
    for p in problems:
        print("  - " + p)
    print()
    print(f"{len(problems)} problem(s). See the module docstring for what each part asserts "
          "and the measurement behind it.")
    sys.exit(1)
print("the pin fires on a stamped object, falls back to the current version everywhere else, "
      "adds no cluster-wide read, renders byte-identically offline, and is documented")
