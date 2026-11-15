#!/usr/bin/env python3
"""A live component's NEW values must be written only at the new apiVersion, once it is served.

THE DEFECT. One umbrella render moves a component's CompositionDefinition to the new chart
version AND writes the component's new spec values. In that render the component's CRD does not
serve the new version yet (the definition rendered in the same pass is what adds it), so
osh.resolveVersion answers the served OLD version -- correctly, an unserved one would close the
gate and prune (#166) -- and the umbrella writes the NEW values at the OLD apiVersion. core-
provider's krateo-composition-version MutatingAdmissionPolicy stamps krateo.io/composition-version
from the apiVersion of each write, so the object stays labelled old, the OLD version's controller
(still running, selecting on that label) owns it, and it runs the OLD chart with the NEW values.
Measured on a GKE upgrade 0.3.64 to 0.3.65: 3 of 33 components did exactly that, and the old
chart's pre-upgrade Job rotated passwords early. core-provider confirmed the reading of the code.

THE FIX (osh.holdValues). Two phases. For an EXISTING component whose migration is approved, the
new values are emitted only when this render emits at the current version AND the component's
CompositionDefinition reports status.apiVersion == composition.krateo.io/<current> AND a condition
of type Ready with status True. Until then the live spec is re-emitted unchanged at the served
version. A component that does not exist, and one whose migration is NOT approved (pinned, #233),
are emitted exactly as before.

What this asserts, in five parts:

 1. THE DECISION, WITH SYNTHETIC INPUT. osh.holdValues is a pure function of (current, emitted
    version, approved, exists, CD status.apiVersion, CD Ready status); a probe calls it for every
    branch, including the r065 state itself.

 2. THE SIGNAL IS READ AS AGREED. osh.definitionApiVersion and osh.definitionReady are pure
    readers of the CompositionDefinition object; a probe hands them synthetic definitions shaped
    as core-provider reports them. Ready is judged on type + status ONLY: Ready=True arrives with
    reason Available or ScaledToZero, and ControllerActive is absent for a remote definition and
    False for an idle scale-to-zero one, so a gate on either holds those components forever.

 3. THE CHART DOES IT, END TO END. The real compositions.yaml is rendered with only the
    cluster-reading helpers replaced by constants (osh.exists, osh.definition, osh.originVersion,
    osh.liveSpec, osh.depsReady, osh.depsExist) and the CRD list replaced by a synthetic one --
    the stubbing hack/validate-umbrella-multidoc.py uses -- and the emitted Composition's
    apiVersion and spec are asserted per cluster state. Parts 1-2 cannot see compositions.yaml;
    this sees whether it consumes the decision at all.

 4. THE READS ARE THE NAMED GETS, STRUCTURALLY -- a limitation, not a style choice. `lookup`
    returns nothing offline, so an osh.definition gutted to return "{}" answers exactly what the
    real one answers without a cluster, and part 3 stubs it anyway. So the definition read is
    asserted as exact text: one named GET of the component's own CompositionDefinition, the only
    CompositionDefinition lookup in the chart, parsed once in compositions.yaml and handed to the
    readers that feed osh.holdValues; the readers and the decision perform no read of their own;
    osh.liveSpec is one named GET of the component at the emitted version, called only inside
    the hold. The behavioural proof of the reads needs an apiserver.

 5. THE OFFLINE RENDER IS UNCHANGED, PROVED DIFFERENTIALLY. Offline nothing exists, so nothing
    is held. Built against a counterfactual chart whose $hold is the constant "", in the four
    selections the origin-pin gate uses, both as `helm template` renders it (no component
    documents: the CRD list is empty) and with the emission gates stubbed open (so the component
    documents themselves are compared, not empty against empty).

Never exits 0 without measuring (#120): every way of not having measured exits 2.
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
COMPOSITIONS = TEMPLATES / "compositions.yaml"
HELPERS = TEMPLATES / "_helpers.tpl"

# A plausible bumped chart version stamped into the staged Chart.yaml (osh.version reads
# .Chart.Version, #179), and the version before it. The same pair the origin-pin gate uses.
CURRENT_CHART_VERSION = "0.3.30"
CURRENT = "v0-3-30"
OLDER = "v0-3-28"
GROUP = "composition.krateo.io"


def av(v):
    return f"{GROUP}/{v}"


SELECTIONS = (
    ("profile=full", ("--set", "profile=full")),
    ("profile=identity", ("--set", "profile=identity")),
    ("enabled={heat}", ("--set", "enabled={heat}")),
    ("enabled={nova,neutron}", ("--set", "enabled={nova,neutron}")),
)


def measurement_failed(msg):
    print(f"FAILED MEASUREMENT: {msg}")
    sys.exit(2)


def helm_error(stderr):
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
for f in (CHART, TEMPLATES):
    if not f.is_dir():
        measurement_failed(f"no {f} -- wrong path?")
for f in (COMPOSITIONS, HELPERS):
    if not f.is_file():
        measurement_failed(f"no {f}, which is where the two-phase emission lives")

umbrella = yaml.safe_load((CHART / "values.yaml").read_text()) or {}
comps = umbrella.get("components") or []
kinds = sorted({c["kind"] for c in comps if isinstance(c, dict) and c.get("kind")})
if not kinds:
    measurement_failed("the umbrella declares no component kinds, so nothing is ever emitted")
by_name = {c["name"]: c["kind"] for c in comps if isinstance(c, dict) and c.get("name")}
# The component the end-to-end part follows. keystone: in profile=identity, has a componentValues
# schema entry, and is given credential references (#228), so its computed spec is never empty.
PROBE_COMPONENT = "keystone"
if PROBE_COMPONENT not in by_name:
    measurement_failed(f"the umbrella has no {PROBE_COMPONENT} component to follow end to end")
PROBE_KIND = by_name[PROBE_COMPONENT]

comp_text = COMPOSITIONS.read_text()
helpers_text = HELPERS.read_text()
template_files = sorted(p for p in TEMPLATES.rglob("*") if p.is_file())

problems = []


def strip_comments(text):
    """Template text without {{/* */}} comment blocks: the prose names the very helpers searched for."""
    return re.sub(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", "", text, flags=re.S)


def define_body(text, name):
    """The body of define <name>, up to the next define (bodies nest `if`, so not to the first end)."""
    start = re.search(r"\{\{-?\s*define\s+\"" + re.escape(name) + r"\"\s*-?\}\}", text)
    if not start:
        return None
    rest = text[start.end():]
    nxt = re.search(r"\{\{-?\s*define\s+\"", rest)
    return rest[:nxt.start()] if nxt else rest


def lookup_args(text):
    """Every `lookup` call in text, as its argument tokens (same tokenizer as the origin-pin gate)."""
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


comp_code = strip_comments(comp_text)
helpers_code = strip_comments(helpers_text)

_tmp = tempfile.mkdtemp(prefix="osh-two-phase-gate-")
atexit.register(shutil.rmtree, _tmp, True)


def stage(src, name):
    """A copy of <src> under the gate's temp dir with the chart version stamped."""
    dst = Path(_tmp) / name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    cy = dst / "Chart.yaml"
    cy.write_text(cy.read_text().replace("CHART_VERSION", CURRENT_CHART_VERSION))
    return dst


def render(chart_dir, *extra):
    return subprocess.run(
        ["helm", "template", "gate", str(chart_dir), "--kube-version", "1.36.0", *extra],
        capture_output=True, text=True)


def gotmpl_raw(s):
    """A Go-template raw string literal holding <s>; JSON never contains a backtick."""
    if "`" in s:
        measurement_failed("a synthetic value contains a backtick and cannot be a raw literal")
    return "`" + s + "`"


def condition(ctype, status, reason):
    return {"type": ctype, "status": status, "reason": reason,
            "lastTransitionTime": "2026-11-15T00:00:00Z"}


def definition(api_version=None, conditions=None, policy=None, upgrade_to=None):
    """A CompositionDefinition shaped as core-provider's status reports it (types.go)."""
    cd = {"apiVersion": "core.krateo.io/v1alpha1", "kind": "CompositionDefinition",
          "metadata": {"name": PROBE_COMPONENT, "namespace": "default"},
          "spec": {"chart": {"version": CURRENT_CHART_VERSION}}}
    if upgrade_to is not None:
        cd["metadata"]["annotations"] = {"krateo.io/upgrade-to-version": upgrade_to}
    if policy is not None:
        cd["spec"]["upgradePolicy"] = policy
    if api_version is not None or conditions is not None:
        cd["status"] = {"kind": PROBE_KIND}
        if api_version is not None:
            cd["status"]["apiVersion"] = api_version
        if conditions is not None:
            cd["status"]["conditions"] = conditions
    return cd


# The definitions core-provider actually reports, named for what they are.
CD = {
    # Serving the new version, controller running.
    "available": definition(av(CURRENT), [condition("Ready", "True", "Available"),
                                          condition("ControllerActive", "True", "Available")]),
    # Serving the new version, scaled to zero: Ready=True AND ControllerActive=False, both
    # ScaledToZero (core-provider types.go, ScaleToZero).
    "scaled_to_zero": definition(av(CURRENT), [condition("Ready", "True", "ScaledToZero"),
                                               condition("ControllerActive", "False", "ScaledToZero")]),
    # Serving the new version on a remote target: no ControllerActive condition at all.
    "remote": definition(av(CURRENT), [condition("Ready", "True", "Available")]),
    # Not Ready, with the reason that elsewhere means ready: only the status may decide.
    "not_ready_reason_available": definition(av(CURRENT), [condition("Ready", "False", "Available")]),
    "ready_unknown": definition(av(CURRENT), [condition("Ready", "Unknown", "Creating")]),
    # ControllerActive listed first and True, Ready False: the reader must not take the first.
    "controller_first_not_ready": definition(av(CURRENT), [condition("ControllerActive", "True", "Available"),
                                                           condition("Ready", "False", "Creating")]),
    # Only ControllerActive: there is no Ready to read.
    "only_controller_active": definition(av(CURRENT), [condition("ControllerActive", "True", "Available")]),
    # Ready, but still on the OLD version: the definition has not moved yet. The r065 state.
    "ready_on_old": definition(av(OLDER), [condition("Ready", "True", "Available")]),
    # A definition with no status yet, and no definition at all.
    "no_status": definition(),
    "absent": {},
}
WANT_API = {k: (cd.get("status") or {}).get("apiVersion", "") for k, cd in CD.items()}
WANT_READY = {"available": "True", "scaled_to_zero": "True", "remote": "True",
              "not_ready_reason_available": "False", "ready_unknown": "Unknown",
              "controller_first_not_ready": "False", "only_controller_active": "",
              "ready_on_old": "True", "no_status": "", "absent": ""}
# Whether an existing, approved component emitted at CURRENT is RELEASED (new values) under each.
WANT_RELEASED = {"available", "scaled_to_zero", "remote"}

# ---------------------------------------------------------------------------
# Parts 1 and 2: the pure decision and the pure readers, in one probe render.
# ---------------------------------------------------------------------------
# (id, emitVer, approved, exists, cdApiVersion, cdReady, want, what). Every branch of
# osh.holdValues: not existing; not approved; released; and each conjunct of the signal failing.
HOLD_CASES = (
    ("fresh_bump", OLDER, "true", "", av(OLDER), "True", "",
     "a component that does not exist yet, at the bump instant"),
    ("fresh_not_ready", CURRENT, "true", "", "", "", "",
     "a component that does not exist yet, its definition not Ready"),
    ("pinned_bump", OLDER, "", "true", av(OLDER), "True", "",
     "an existing component whose migration is NOT approved, at the bump instant"),
    ("pinned_not_ready", CURRENT, "", "true", av(CURRENT), "False", "",
     "an existing component whose migration is NOT approved, its definition not Ready"),
    ("r065_bump_instant", OLDER, "true", "true", av(OLDER), "True", "true",
     "an existing approved component at the bump instant (emitted at the OLD version, its "
     "definition still on the old one) -- the 0.3.65 defect"),
    ("emitted_old_cd_new", OLDER, "true", "true", av(CURRENT), "True", "true",
     "an existing approved component whose definition reports the new version while this render "
     "still emits at the old one (the CRD list this render read does not serve it)"),
    ("crd_served_cd_not_moved", CURRENT, "true", "true", av(OLDER), "True", "true",
     "an existing approved component emitted at the new version while its definition still "
     "reports the old one"),
    ("cd_moved_not_ready", CURRENT, "true", "true", av(CURRENT), "False", "true",
     "an existing approved component whose definition is on the new version but Ready=False"),
    ("cd_ready_unknown", CURRENT, "true", "true", av(CURRENT), "Unknown", "true",
     "an existing approved component whose definition is Ready=Unknown"),
    ("cd_no_ready", CURRENT, "true", "true", av(CURRENT), "", "true",
     "an existing approved component whose definition reports no Ready condition"),
    ("no_cd", CURRENT, "true", "true", "", "", "true",
     "an existing approved component with no definition found"),
    ("bare_version_status", CURRENT, "true", "true", CURRENT, "True", "true",
     "a status.apiVersion that is a bare version, not the agreed composition.krateo.io/<v>"),
    ("released", CURRENT, "true", "true", av(CURRENT), "True", "",
     "an existing approved component emitted at the new version, definition on it and Ready=True"),
)

# (id, synthetic CD, want approved) for osh.migrationApprovedFor, now a pure reader of the CD.
APPROVAL_CASES = (
    ("automatic", definition(policy="Automatic"), "true"),
    ("unset", definition(), "true"),
    ("absent", {}, "true"),
    ("paused", definition(policy="Paused"), ""),
    ("manual_no_annotation", definition(policy="Manual"), ""),
    ("manual_to_current", definition(policy="Manual", upgrade_to=CURRENT), "true"),
    ("manual_to_older", definition(policy="Manual", upgrade_to=OLDER), ""),
)

PURE_CHART = stage(CHART, "pure")
lines = ["{{- $cur := include \"osh.version\" (list $) }}", "---", "apiVersion: v1",
         "kind: ConfigMap", "metadata:", "  name: gate-probe", "data:",
         "  current: {{ $cur | quote }}"]
for cid, emit, appr, exists, cdapi, ready, _, _ in HOLD_CASES:
    lines.append(f"  hold_{cid}: {{{{ include \"osh.holdValues\" (list $cur {json.dumps(emit)} "
                 f"{json.dumps(appr)} {json.dumps(exists)} {json.dumps(cdapi)} {json.dumps(ready)}) | quote }}}}")
for k, cd in CD.items():
    lit = gotmpl_raw(json.dumps(cd))
    lines.append(f"  api_{k}: {{{{ include \"osh.definitionApiVersion\" (list ({lit} | fromJson)) | quote }}}}")
    lines.append(f"  ready_{k}: {{{{ include \"osh.definitionReady\" (list ({lit} | fromJson)) | quote }}}}")
    lines.append(f"  chain_{k}: {{{{ include \"osh.holdValues\" (list $cur $cur \"true\" \"true\" "
                 f"(include \"osh.definitionApiVersion\" (list ({lit} | fromJson))) "
                 f"(include \"osh.definitionReady\" (list ({lit} | fromJson)))) | quote }}}}")
for aid, cd, _ in APPROVAL_CASES:
    lit = gotmpl_raw(json.dumps(cd))
    lines.append(f"  approved_{aid}: {{{{ include \"osh.migrationApprovedFor\" (list $ ({lit} | fromJson)) | quote }}}}")
(PURE_CHART / "templates/zz-gate-probe.yaml").write_text("\n".join(lines) + "\n")
r = render(PURE_CHART, "--show-only", "templates/zz-gate-probe.yaml")
if r.returncode != 0:
    measurement_failed(f"the decision probe did not render -- {helm_error(r.stderr)}")
try:
    pure = next(d for d in yaml.safe_load_all(r.stdout) if d and d.get("kind") == "ConfigMap")["data"]
except (StopIteration, KeyError, TypeError, yaml.YAMLError) as e:
    measurement_failed(f"the decision probe rendered no readable ConfigMap -- {str(e)[:200]}")
if pure.get("current") != CURRENT:
    measurement_failed(f"osh.version answers {pure.get('current')!r}, not {CURRENT!r}; every "
                       "expectation below is stated at that version")

for cid, emit, appr, exists, cdapi, ready, want, what in HOLD_CASES:
    got = pure.get(f"hold_{cid}")
    if got is None:
        measurement_failed(f"the probe produced no answer for case {cid}")
    if got == want:
        continue
    if cid == "r065_bump_instant":
        problems.append(
            "REGRESSION OF THE 0.3.65 DEFECT: osh.holdValues does not hold " + what + ". The "
            "umbrella writes the NEW values at the OLD apiVersion, the object keeps the old "
            "krateo.io/composition-version label, and the old controller runs the OLD chart with "
            "the NEW values (3 of 33 components on GKE, 0.3.64 -> 0.3.65; the old pre-upgrade Job "
            "rotated passwords early).")
    elif cid.startswith("pinned"):
        problems.append(
            f"osh.holdValues answers {got!r} for {what}; it must answer {want!r}. The hold is "
            "applied to a component whose migration is NOT approved: the engine holds it at its "
            "origin on purpose (#233), and holding its values too freezes them for as long as the "
            "operator keeps it paused. A pinned component keeps its pre-two-phase behaviour.")
    elif cid.startswith("fresh"):
        problems.append(
            f"osh.holdValues answers {got!r} for {what}; it must answer {want!r}. Nothing runs a "
            "component that does not exist, so there is no old chart to protect, and there is no "
            "live spec to re-emit: holding it creates it without its values.")
    elif want == "true":
        problems.append(
            f"osh.holdValues releases the new values for {what}. The new values may be written "
            "only AT the new apiVersion and only once the definition reports "
            f"status.apiVersion={av(CURRENT)} with Ready status True; otherwise the old version's "
            "controller can still own the object and runs its old chart with them (the 0.3.65 "
            "defect).")
    else:
        problems.append(
            f"osh.holdValues holds {what}. Every conjunct of the agreed signal holds, so the new "
            "values must be written; holding here defers them forever.")

for k in CD:
    got_api, got_ready, got_chain = pure.get(f"api_{k}"), pure.get(f"ready_{k}"), pure.get(f"chain_{k}")
    if got_api != WANT_API[k]:
        problems.append(
            f"osh.definitionApiVersion reads {got_api!r} from the {k} definition, not "
            f"{WANT_API[k]!r} (its status.apiVersion).")
    if got_ready != WANT_READY[k]:
        problems.append(
            f"osh.definitionReady reads {got_ready!r} from the {k} definition, not "
            f"{WANT_READY[k]!r}: the status of its condition of TYPE Ready. "
            + ("Its Ready=True carries reason ScaledToZero and ControllerActive=False, so a reader "
               "that judges the reason or requires ControllerActive holds every scaled-to-zero "
               "component forever." if k == "scaled_to_zero" else
               "It has no ControllerActive condition at all (a remote definition), so a reader "
               "that requires one holds it forever." if k == "remote" else
               "Its Ready is False with reason Available: a reader that judges the reason releases "
               "a definition that is not Ready." if k == "not_ready_reason_available" else ""))
    want_chain = "" if k in WANT_RELEASED else "true"
    if got_chain != want_chain:
        problems.append(
            f"through the real readers, the {k} definition "
            + ("HOLDS the values of an approved component emitted at the current version, although "
               "it reports the new version Ready=True. Gate on Ready's TYPE and STATUS only -- never "
               "its reason (Available, ScaledToZero) and never ControllerActive (absent for a "
               "remote definition, False for an idle scale-to-zero one)."
               if want_chain == "" else
               "RELEASES the new values although it does not report the new version with Ready "
               "status True."))

for aid, _, want in APPROVAL_CASES:
    got = pure.get(f"approved_{aid}")
    if got != want:
        problems.append(
            f"osh.migrationApprovedFor answers {got!r} for the {aid} definition; core-provider's "
            f"MigrationApproved answers {want!r}. A wrong approval either holds a pinned component "
            "or writes an approved one at its origin (#233).")

# ---------------------------------------------------------------------------
# Part 3: end to end through the real compositions.yaml, cluster reads stubbed.
# ---------------------------------------------------------------------------
CRDS_LINE = '{{- $crds := (lookup "apiextensions.k8s.io/v1" "CustomResourceDefinition" "" "").items }}'
if comp_text.count(CRDS_LINE) != 1:
    measurement_failed(
        "compositions.yaml no longer fetches the CRD list with the one line this gate replaces by a "
        "synthetic list, so part 3 cannot set the cluster state it asserts against. Update "
        "CRDS_LINE with the template.")


def crd_list(versions):
    return [{"spec": {"group": GROUP, "names": {"kind": k},
                      "versions": [{"name": n, "served": True} for n in versions]}} for k in kinds]


BUMP = crd_list([OLDER])            # the bump instant: only the old version served
BOTH = crd_list([OLDER, CURRENT])   # the new version added, the old still served

LIVE = {"gateLiveMarker": "held-live-spec"}
NEW_MARKER = "new-values-from-this-chart"


def stub_define(src, name, body):
    """Replace define <name>'s body with <body>, as hack/validate-umbrella-multidoc.py does."""
    start = src.find('{{- define "%s" -}}' % name)
    if start < 0:
        measurement_failed(f"{name} is not defined in _helpers.tpl; part 3 cannot stub the cluster "
                           "read it performs, and is out of date with the chart")
    nxt = src.find('\n{{- define ', start + 1)
    nxt = len(src) if nxt < 0 else nxt
    close = src[start:nxt].rfind("{{- end -}}")
    if close < 0:
        measurement_failed(f"cannot find the closing marker of {name}")
    return src[:start] + '{{- define "%s" -}}%s{{- end -}}' % (name, body) \
        + src[start + close + len("{{- end -}}"):]


E2E_CHART = stage(CHART, "e2e")
E2E_VALUES = Path(_tmp) / "e2e-values.yaml"
E2E_VALUES.write_text(yaml.safe_dump({"profile": "identity",
                                      "componentValues": {PROBE_COMPONENT: {"gateMarker": NEW_MARKER}}}))
_e2e_comp = (E2E_CHART / "templates/compositions.yaml").read_text()
_e2e_helpers = (E2E_CHART / "templates/_helpers.tpl").read_text()


def e2e(crds, exists, cd, origin, live):
    """The probe component's emitted Composition (or None) for one stubbed cluster state."""
    comp = _e2e_comp.replace(CRDS_LINE, "{{- $crds := %s | fromJsonArray }}" % gotmpl_raw(json.dumps(crds)))
    h = _e2e_helpers
    for name, body in (("osh.exists", exists),
                       ("osh.definition", gotmpl_raw(json.dumps(cd)).join(("{{- ", " -}}"))),
                       ("osh.originVersion", origin),
                       ("osh.liveSpec", "" if live is None else gotmpl_raw(json.dumps(live)).join(("{{- ", " -}}"))),
                       ("osh.depsReady", "true"),
                       ("osh.depsExist", "true")):
        h = stub_define(h, name, body)
    (E2E_CHART / "templates/compositions.yaml").write_text(comp)
    (E2E_CHART / "templates/_helpers.tpl").write_text(h)
    r = render(E2E_CHART, "-f", str(E2E_VALUES), "--show-only", "templates/compositions.yaml")
    if r.returncode != 0:
        measurement_failed(f"the end-to-end probe did not render -- {helm_error(r.stderr)}")
    try:
        docs = [d for d in yaml.safe_load_all(r.stdout) if d]
    except yaml.YAMLError as e:
        measurement_failed(f"the end-to-end probe output does not parse -- {str(e)[:200]}")
    if not any(str(d.get("apiVersion", "")).startswith(GROUP + "/") for d in docs) and crds is BOTH:
        measurement_failed("with both versions served and every gate stubbed open, compositions.yaml "
                           "emitted no component at all: part 3 is not measuring the emission")
    for d in docs:
        if d.get("kind") == PROBE_KIND and (d.get("metadata") or {}).get("name") == PROBE_COMPONENT:
            return d
    return None


# (id, crds, exists, cd, origin, live, want apiVersion or None for "not emitted", want spec, what)
E2E_CASES = (
    ("r065_bump_instant", BUMP, "true", CD["ready_on_old"], "", LIVE, OLDER, "live",
     "an existing approved component at the bump instant"),
    ("crd_served_cd_not_moved", BOTH, "true", CD["ready_on_old"], "", LIVE, CURRENT, "live",
     "an existing approved component, both versions served, its definition still on the old one"),
    ("released_available", BOTH, "true", CD["available"], "", LIVE, CURRENT, "new",
     "an existing approved component whose definition serves the new version (Available)"),
    ("released_scaled_to_zero", BOTH, "true", CD["scaled_to_zero"], "", LIVE, CURRENT, "new",
     "an existing approved component whose definition serves the new version scaled to zero "
     "(Ready=True/ScaledToZero, ControllerActive=False)"),
    ("released_remote", BOTH, "true", CD["remote"], "", LIVE, CURRENT, "new",
     "an existing approved component whose definition serves the new version on a remote target "
     "(no ControllerActive condition)"),
    ("not_ready_reason_available", BOTH, "true", CD["not_ready_reason_available"], "", LIVE, CURRENT, "live",
     "an existing approved component whose definition is Ready=False with reason Available"),
    ("manual_approved_bump", BUMP, "true",
     definition(av(OLDER), [condition("Ready", "True", "Available")], policy="Manual", upgrade_to=CURRENT),
     "", LIVE, OLDER, "live",
     "an existing component approved by Manual + upgrade-to-version, at the bump instant"),
    ("pinned_paused_bump", BUMP, "true",
     definition(av(OLDER), [condition("Ready", "True", "Available")], policy="Paused"),
     OLDER, LIVE, OLDER, "new",
     "an existing PAUSED component stamped at its origin, at the bump instant"),
    ("pinned_paused_both", BOTH, "true",
     definition(av(CURRENT), [condition("Ready", "False", "Creating")], policy="Paused"),
     OLDER, LIVE, OLDER, "new",
     "an existing PAUSED component stamped at its origin, both versions served"),
    ("pinned_manual_unapproved", BOTH, "true",
     definition(av(CURRENT), [condition("Ready", "False", "Creating")], policy="Manual"),
     OLDER, LIVE, OLDER, "new",
     "an existing Manual component without upgrade-to-version, stamped at its origin"),
    ("fresh_both", BOTH, "", CD["not_ready_reason_available"], "", LIVE, CURRENT, "new",
     "a component that does not exist yet, both versions served, its definition not Ready"),
    ("fresh_bump", BUMP, "", CD["ready_on_old"], "", LIVE, None, None,
     "a component that does not exist yet at the bump instant"),
    ("held_empty_live_spec", BUMP, "true", CD["ready_on_old"], "", {}, OLDER, "empty",
     "an existing approved component at the bump instant whose live spec is empty"),
    ("held_object_gone", BUMP, "true", CD["ready_on_old"], "", None, OLDER, "new",
     "an existing approved component deleted before its spec was read (documented fallback)"),
)

new_specs = []
for cid, crds, exists, cd, origin, live, want_ver, want_spec, what in E2E_CASES:
    doc = e2e(crds, exists, cd, origin, live)
    if want_ver is None:
        if doc is not None:
            problems.append(f"end to end, {what} is emitted (at {doc.get('apiVersion')}); it must "
                            "wait for the current CRD, as before the two phases.")
        continue
    if doc is None:
        problems.append(f"end to end, {what} is NOT emitted; it must be emitted at {av(want_ver)}. "
                        "A live component missing from the manifest is pruned by helm (#166).")
        continue
    got_ver = doc.get("apiVersion")
    spec = doc.get("spec") or {}
    if want_spec == "live":
        ok = spec == LIVE
    elif want_spec == "empty":
        ok = spec == {}
    else:
        ok = spec.get("gateMarker") == NEW_MARKER and "gateLiveMarker" not in spec
        if ok:
            new_specs.append(spec)
    if got_ver == av(want_ver) and ok:
        continue
    if cid == "r065_bump_instant" or (cid == "manual_approved_bump" and spec.get("gateMarker") == NEW_MARKER):
        problems.append(
            f"REGRESSION OF THE 0.3.65 DEFECT, end to end: compositions.yaml emits {what} at "
            f"{got_ver} with spec {json.dumps(spec)[:160]}; it must re-emit the LIVE spec "
            f"{json.dumps(LIVE)} at {av(want_ver)}. New values at the old apiVersion leave the "
            "object on the old krateo.io/composition-version label, where the old controller runs "
            "the OLD chart with the NEW values.")
    elif cid.startswith("pinned"):
        problems.append(
            f"end to end, {what} is emitted at {got_ver} with spec {json.dumps(spec)[:160]}; it must "
            f"be {av(want_ver)} with the values this chart computes, exactly as before the two "
            "phases. The hold must not apply to a component whose migration is not approved.")
    elif cid.startswith("released"):
        problems.append(
            f"end to end, {what} is emitted at {got_ver} with spec {json.dumps(spec)[:160]}; it must "
            f"be {av(want_ver)} with the NEW values. Ready is judged on type and status only: not "
            "its reason, and not ControllerActive.")
    else:
        problems.append(
            f"end to end, {what} is emitted at {got_ver} with spec {json.dumps(spec)[:160]}; it must "
            f"be {av(want_ver)} with the {want_spec} spec.")

# The probe could tell the two specs apart, and every "new" emission is the same computed spec.
if new_specs and any(s != new_specs[0] for s in new_specs):
    problems.append("end to end, the components emitted with their computed values do not all carry "
                    "the same spec, so something other than the hold changed them.")
if not new_specs:
    measurement_failed("no end-to-end case emitted the computed values, so the probe never showed "
                       "it could tell the new spec from the held one")

# ---------------------------------------------------------------------------
# Part 4: the reads are the named GETs (structural).
# ---------------------------------------------------------------------------
WIRING = (
    ('{{- $cd := include "osh.definition" (list $ $c.name) | fromJson }}',
     "the component's CompositionDefinition must be read once, by osh.definition's named GET, and "
     "parsed in compositions.yaml"),
    ('{{- $approved := include "osh.migrationApprovedFor" (list $ $cd) }}',
     "the approval must be derived once, from that same read"),
    ('{{- $emitVer := include "osh.emitVersion" (list $ $c.kind $c.name $crds $exists $approved) }}',
     "osh.emitVersion must receive that approval rather than derive its own"),
    ('{{- $hold := include "osh.holdValues" (list (include "osh.version" (list $)) $emitVer $approved '
     '$exists (include "osh.definitionApiVersion" (list $cd)) (include "osh.definitionReady" (list $cd))) }}',
     "the decision must be fed the emitted version, that approval, the existence answer, and the "
     "status of the SAME definition read -- not a value, not a second lookup"),
)
HOLD_BLOCK = ('{{- if eq $hold "true" }}\n'
              '{{- $live := include "osh.liveSpec" (list $ $c.kind $c.name $emitVer $crds) }}\n'
              '{{- if $live }}{{- $ov = $live | fromJson }}{{- end }}\n'
              '{{- end }}\n')
for line, why in WIRING:
    n = comp_code.count(line)
    if n != 1:
        problems.append(f"compositions.yaml contains `{line}` {n} times, not once: {why}.")
if comp_code.count(HOLD_BLOCK) != 1:
    problems.append(
        "compositions.yaml does not consume the decision with the hold block this gate expects "
        "(replace $ov by the live spec read at $emitVer when $hold is \"true\"). Without it the "
        "decision is computed and ignored: the 0.3.65 defect, new values at the old apiVersion.")
else:
    at = comp_code.index(HOLD_BLOCK)
    merge_at = comp_code.find("mergeOverwrite")
    doc_at = comp_code.find('apiVersion: {{ include "osh.apiVersionAt" (list $emitVer) }}')
    if not (0 <= merge_at < at < doc_at):
        problems.append(
            "the hold block is not between the credential merge and the emitted document: either "
            "something it replaces is added after it, or it runs after the document is written.")
    if re.search(r"\$ov\s*=", comp_code[at + len(HOLD_BLOCK):doc_at]):
        problems.append("$ov is reassigned after the hold block, so the held live spec can be "
                        "overwritten with new values before the document is emitted.")
for var in ("$cd", "$approved", "$hold", "$live"):
    if len(re.findall(re.escape(var) + r"\s*:?=(?!=)", comp_code)) != 1:
        problems.append(f"compositions.yaml assigns {var} more than once (or never): the decision "
                        "inputs must come from the one read, not be replaced on the way.")

all_lookups = {p: lookup_args(strip_comments(p.read_text(errors="replace"))) for p in template_files}
cd_lookups = [(p, a) for p, calls in all_lookups.items() for a in calls
              if len(a) >= 2 and unquote(a[1]) == "CompositionDefinition"]
EXPECTED_CD_LOOKUP = ['"core.krateo.io/v1alpha1"', '"CompositionDefinition"', "$top.Release.Namespace", "$name"]
DEF_BODY = ('{{- $top := index . 0 -}}{{- $name := index . 1 -}}'
            '{{- $cd := lookup "core.krateo.io/v1alpha1" "CompositionDefinition" $top.Release.Namespace $name | default dict -}}'
            '{{- $cd | toJson -}}{{- end -}}')
if len(cd_lookups) != 1:
    problems.append(
        f"the chart performs {len(cd_lookups)} CompositionDefinition lookups; it must perform one, "
        "in osh.definition. A second read of the definition can disagree with the first across a "
        "reconcile of it, and lets the decision's status come from somewhere the approval did not.")
elif cd_lookups[0][0] != HELPERS or cd_lookups[0][1] != EXPECTED_CD_LOOKUP:
    problems.append(
        f"the CompositionDefinition lookup is `lookup {' '.join(cd_lookups[0][1])}` in "
        f"{cd_lookups[0][0].relative_to(ROOT)}; it must be `lookup {' '.join(EXPECTED_CD_LOOKUP)}` in "
        "_helpers.tpl: a named GET of the component's own definition, in the release namespace.")
body = define_body(helpers_code, "osh.definition")
if body is None:
    problems.append("osh.definition is missing: nothing reads the component's CompositionDefinition, "
                    "so the hold decision has no status to judge.")
elif re.sub(r"\s+", "", body) != re.sub(r"\s+", "", DEF_BODY):
    problems.append(
        "osh.definition is not the named GET this gate expects, returned as is. Offline `lookup` "
        "returns nothing, so a helper that returns \"{}\" (or anything not from that GET) answers "
        "exactly what the real one answers without a cluster: every probe passes and, on a cluster, "
        "the status the values are released by is not the definition's. Found: "
        + re.sub(r"\s+", " ", body).strip()[:300])

for name in ("osh.holdValues", "osh.definitionApiVersion", "osh.definitionReady", "osh.migrationApprovedFor"):
    b = define_body(helpers_code, name)
    if b is None:
        problems.append(f"{name} is missing from _helpers.tpl.")
        continue
    if re.search(r"\blookup\b", b) or ".Values" in b or "osh.definition\"" in b:
        problems.append(
            f"{name} reads something itself (a lookup, .Values or osh.definition). It must be a pure "
            "function of its arguments: the definition is read once, by osh.definition, and handed "
            "in. A second source is a status the gate cannot see and the approval did not use.")
for name in ("osh.definitionApiVersion", "osh.definitionReady"):
    b = define_body(helpers_code, name) or ""
    if "{{- $cd := index . 0" not in b:
        problems.append(f"{name} does not read the definition from its argument.")
rb = define_body(helpers_code, "osh.definitionReady") or ""
if re.search(r"\.reason\b", rb) or "ControllerActive" in rb:
    problems.append(
        "osh.definitionReady reads a condition's reason or ControllerActive. Ready=True arrives "
        "with reason Available or ScaledToZero, and ControllerActive is absent for a remote "
        "definition and False for an idle scale-to-zero one: gate on Ready's type and status only.")

ls = define_body(helpers_code, "osh.liveSpec")
if ls is None:
    problems.append("osh.liveSpec is missing: nothing reads the live spec the hold must re-emit.")
else:
    calls = lookup_args(ls)
    want = ['(include "osh.apiVersionAt" (list $ver))', "$kind", "$top.Release.Namespace", "$name"]
    if calls != [want]:
        problems.append(
            f"osh.liveSpec performs {calls}; it must perform exactly one named GET, "
            f"`lookup {' '.join(want)}`: the component's own object, at the version the held "
            "document is emitted at, so the spec is read and written through one schema.")
    if "(($o.spec) | default dict) | toJson" not in ls:
        problems.append("osh.liveSpec does not return the looked-up object's spec as JSON.")
n_live = sum(strip_comments(p.read_text(errors="replace")).count('include "osh.liveSpec"') for p in template_files)
if n_live != 1:
    problems.append(f"osh.liveSpec is called {n_live} times across the templates; it must be called "
                    "once, inside the hold block, so a render that holds nothing makes no extra GET.")
for p, calls in all_lookups.items():
    for a in calls:
        if len(a) == 4 and unquote(a[3]) == "" and p == HELPERS:
            problems.append("_helpers.tpl contains a full-list lookup (see validate-origin-version-pin.py "
                            "part 4 for the 13-minute render it costs).")

# ---------------------------------------------------------------------------
# Part 5: the offline render is byte-identical to the chart without the hold.
# ---------------------------------------------------------------------------
HOLD_LINE = WIRING[3][0]
OPEN_STUBS = (("osh.crdExists", "true"), ("osh.depsReady", "true"), ("osh.depsExist", "true"))


def render_selections(chart_dir, label):
    out = {}
    for name, extra in SELECTIONS:
        r = render(chart_dir, *extra)
        if r.returncode != 0:
            measurement_failed(f"the {label} chart does not render {name} -- {helm_error(r.stderr)}")
        out[name] = r.stdout
    return out


if comp_text.count(HOLD_LINE) != 1:
    problems.append("part 5 (the offline byte-identity differential) was NOT run: there is no single "
                    "$hold line to replace with the counterfactual's constant. Fix the wiring above; "
                    "the byte-identity property is unmeasured until then.")
else:
    results = {}
    for variant in ("feature", "counterfactual"):
        for mode in ("plain", "gates-open"):
            d = stage(CHART, f"{variant}-{mode}")
            if variant == "counterfactual":
                cp = d / "templates/compositions.yaml"
                cp.write_text(cp.read_text().replace(HOLD_LINE, '{{- $hold := "" }}'))
            if mode == "gates-open":
                hp = d / "templates/_helpers.tpl"
                h = hp.read_text()
                for name, val in OPEN_STUBS:
                    h = stub_define(h, name, val)
                hp.write_text(h)
            results[(variant, mode)] = render_selections(d, f"{variant} {mode}")
    emitted = sum(results[("feature", "gates-open")][n].count(f"apiVersion: {av(CURRENT)}") for n, _ in SELECTIONS)
    if emitted == 0:
        measurement_failed("with the gates stubbed open the umbrella emitted no component document, so "
                           "the differential would compare empty against empty")
    for mode in ("plain", "gates-open"):
        for name, _ in SELECTIONS:
            if results[("feature", mode)][name] != results[("counterfactual", mode)][name]:
                problems.append(
                    f"with NO CLUSTER ({mode}), the chart and the same chart without the hold render "
                    f"{name} differently. Offline nothing exists, so nothing may be held: a "
                    "difference changes CI's renders, the release's example manifests and every "
                    "`helm template` an operator runs.")

# ---------------------------------------------------------------------------
print(f"two-phase values: {len(HOLD_CASES)} decisions, {len(CD)} synthetic definitions, "
      f"{len(APPROVAL_CASES)} approvals, {len(E2E_CASES)} end-to-end cluster states, "
      f"{len(SELECTIONS)} selections x 2 modes rendered twice")
if problems:
    print()
    for p in problems:
        print("  - " + p)
    print()
    print(f"FAIL: {len(problems)} problem(s). See the module docstring for what each part asserts.")
    sys.exit(1)
print("OK: an existing approved component's new values are written only at the new apiVersion once "
      "its definition serves it (Ready by type and status); fresh and pinned components are "
      "unchanged; the reads are the named GETs; the offline render is byte-identical. NOT covered: "
      "the lookups themselves need a live cluster -- see part 4.")
