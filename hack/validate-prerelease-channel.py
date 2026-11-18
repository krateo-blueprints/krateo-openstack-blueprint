#!/usr/bin/env python3
"""A pre-release never reaches the Marketplace index, a release is never a pre-release version,
and pruning deletes only pre-releases.

Why this exists. Every X.Y.Z tag publishes to the Marketplace index, so a change has to be
verified on GKE before a tag exists (Diego, 2026-11-17: "batch on main, GKE-verify, then tag").
prerelease.yaml publishes a commit's charts to GHCR at X.Y.Z-main.g<sha7>, and its stamped
CompositionDefinitions as a workflow artifact, so the GKE pair installs it the way it installs a
release. That is safe only while two properties hold, and both are properties of files this
repository can change in a one-line edit, so they are checked here on every pull request.

  1. THE PRE-RELEASE CANNOT WRITE THE INDEX. Measured 2026-11-17: the Marketplace lists what
     krateo-blueprints.github.io/charts/<channel>/index.yaml lists, live, and through the
     krateo-marketplace-catalog snapshot that krateo-blueprints/marketplace hack/build-index.py
     builds from that same file. Its only writer is krateo-blueprints/charts publish-chart.yaml,
     a reusable workflow that downloads tarballs from the CALLING run and pushes with
     CHARTS_PUBLISH_TOKEN. Nothing reads GHCR, Releases or artifacts into an index. So
     prerelease.yaml must: call no reusable workflow; reference no secret but GITHUB_TOKEN (that
     token writes this repository's packages, never krateo-blueprints/charts); hold exactly
     `contents: read` and `packages: write`, with no job-level override (contents: read is what
     makes a tag or a GitHub release impossible, so no /releases/download/ URL and no tag for
     release-notes' `git describe` to take for the previous release). And no workflow but
     release-tag.yaml may call publish-chart.yaml at all.
  2. A RELEASE IS PLAIN X.Y.Z. release-tag.yaml triggers only on the plain glob, checks the tag's
     shape before anything else, and calls the shared scripts with the `release` channel, which
     refuse a pre-release version before stamping. prerelease.yaml calls them with `prerelease`
     and derives its version with hack/release-version.sh.
  3. PRUNING DELETES ONLY PRE-RELEASES. prune-prereleases.yaml holds exactly `contents: read`,
     references no secret but PRERELEASE_PRUNE_TOKEN, refuses to run without it, and a manual run
     defaults to a dry run. hack/prune-prereleases.py deletes a version only if every tag has the
     pre-release shape; its table, its keep-the-newest plan and delete_version's own refusal of a
     release are exercised against a fake API.

Then it proves the checks can fail. The version table runs against hack/release-version.sh, and
against copies of it with each pattern loosened. hack/publish-charts.sh and
hack/stage-release-assets.sh run on a throwaway tree with a stub `helm`, with a version of the
wrong shape (nothing may be stamped, helm never called, nothing written) and with the right one
(stamped, helm called: the positive control that makes "nothing happened" mean something), and
again with their version check removed. Each workflow check runs against the real file and against
mutated copies; a mutation it does not catch fails the gate.

Exits non-zero when a file it needs is missing: a gate that read nothing has measured nothing.

Usage: hack/validate-prerelease-channel.py [repo-root]
"""
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    print("FAIL: PyYAML is required; nothing was measured", file=sys.stderr)
    sys.exit(2)

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 \
    else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows")
PRE = "prerelease.yaml"
REL = "release-tag.yaml"
SCRIPTS = ("release-version.sh", "publish-charts.sh", "stage-release-assets.sh")
PUBLISHER = "krateo-blueprints/charts/.github/workflows/publish-chart.yaml"
RELEASE_GLOB = "[0-9]+.[0-9]+.[0-9]+"
EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.S)


def current_pin(name, target):
    """`(<target>@<40-hex>, " # <comment>")` as the workflow pins it now. The pin mutations below are anchored on it,
    not on a SHA written here, so bumping a pin cannot break a mutation (krateo-openstack-blueprint#403: the anchors
    named release-preflight@788f1a6 after the pin moved to 1.4.2). No pin found gives an anchor no file contains, so
    mutate() fails loudly instead of skipping."""
    try:
        text = open(os.path.join(WF, name), encoding="utf-8").read()
    except OSError:
        return "%s@<%s unreadable>" % (target, name), ""
    m = re.search(re.escape(target) + r"@([0-9a-f]{40})((?: # [^\n]*)?)", text)
    return ("%s@%s" % (target, m.group(1)), m.group(2)) if m else ("%s@<no pin in %s>" % (target, name), "")


def measurement_failed(msg):
    print(f"FAIL: {msg} -- nothing was measured", file=sys.stderr)
    sys.exit(2)


def strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from strings(k)
            yield from strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from strings(v)


def triggers(doc):
    # PyYAML reads the bare key `on` as the boolean True (YAML 1.1).
    return doc.get("on", doc.get(True)) or {}


def code(run):
    """A step's shell without its comment lines. A comment that names a command is not that command: a check keyed on
    text would otherwise pass on `# hack/publish-charts.sh release` with nothing pushing at all."""
    return "\n".join(l for l in str(run or "").splitlines() if not l.lstrip().startswith("#"))


def runs(doc):
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                yield code(step["run"])


# --- rule 14: charts are pushed only through the pinned shared chart-push action ------------------
CHART_PUSH_PIN = re.compile(r"^krateo-platformops/\.github/\.github/actions/chart-push@[0-9a-f]{40}$")


def check_push_wiring(name, doc, text, channel):
    """The workflow locates the shared push.sh with one pinned chart-push step, and its one step that runs
    hack/publish-charts.sh <channel> comes after it with CHART_PUSH and the registry credentials set. No step runs
    `helm push` itself. A workflow with no push step fails here: a check that found nothing to check has checked
    nothing (hack/validate-prerelease-channel.py once keyed on "helm push", which the migration removed)."""
    problems = []
    steps = [s for job in (doc.get("jobs") or {}).values() for s in ((job or {}).get("steps") or [])
             if isinstance(s, dict)]
    locate = [i for i, s in enumerate(steps) if s.get("id") == "chart-push"]
    if len(locate) != 1:
        problems.append(f"{name}: needs exactly one step with id chart-push (the shared chart-push action), found "
                        f"{len(locate)}")
    else:
        uses = str(steps[locate[0]].get("uses", ""))
        if not CHART_PUSH_PIN.match(uses):
            problems.append(f"{name}: the chart-push step uses {uses!r}; it must be "
                            "krateo-platformops/.github/.github/actions/chart-push@<40-hex commit> (rules 9 and 14)")
        elif not re.search(r"^\s*uses: " + re.escape(uses) + r" # (\d+\.\d+\.\d+|main \d{4}-\d{2}-\d{2})\s*$",
                           text, re.M):
            problems.append(f"{name}: the chart-push pin has no `# X.Y.Z` (or `# main YYYY-MM-DD`) comment")
    push = [i for i, s in enumerate(steps) if re.search(r"hack/publish-charts\.sh\s+%s\b" % channel, code(s.get("run")))]
    if len(push) != 1:
        problems.append(f"{name}: needs exactly one step that runs hack/publish-charts.sh {channel} (comments do not "
                        f"count), found {len(push)}")
    else:
        env = steps[push[0]].get("env") or {}
        if "steps.chart-push.outputs.path" not in str(env.get("CHART_PUSH", "")):
            problems.append(f"{name}: the publish step's CHART_PUSH must be the chart-push step's path output, got "
                            f"{env.get('CHART_PUSH')!r}")
        for v in ("REGISTRY_USERNAME", "REGISTRY_PASSWORD"):
            if not env.get(v):
                problems.append(f"{name}: the publish step does not set {v} (push.sh reads the manifest back with it)")
        if locate and push[0] < locate[0]:
            problems.append(f"{name}: the publish step runs before the chart-push step locates push.sh")
    for i, s in enumerate(steps):
        if re.search(r"\bhelm\s+push\b", code(s.get("run"))):
            problems.append(f"{name}: step {s.get('name', i)!r} runs `helm push` itself; charts are pushed only "
                            "through the shared chart-push action (charts PUBLISHING.md rule 14)")
    return problems


def channel_calls(doc, script):
    return re.findall(r"hack/" + re.escape(script) + r"\s+(\S+)", "\n".join(runs(doc)))


def load(name, text):
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return None, [f"{name}: does not parse: {e}"]
    if not isinstance(doc, dict):
        return None, [f"{name}: is not a mapping"]
    return doc, []


def check_prerelease(text):
    doc, problems = load(PRE, text)
    if doc is None:
        return problems
    if doc.get("permissions") != {"contents": "read", "packages": "write"}:
        problems.append(f"{PRE}: workflow permissions must be exactly contents: read and packages: "
                        f"write, got {doc.get('permissions')!r}; contents: write could create a tag "
                        "or a GitHub release")
    jobs = doc.get("jobs") or {}
    if not jobs:
        problems.append(f"{PRE}: has no jobs")
    for name, job in jobs.items():
        job = job or {}
        if "permissions" in job:
            problems.append(f"{PRE}: job {name} overrides permissions")
        if "uses" in job:
            problems.append(f"{PRE}: job {name} calls the reusable workflow {job['uses']}; a "
                            "pre-release calls none (publish-chart.yaml is the index's writer)")
        if "secrets" in job:
            problems.append(f"{PRE}: job {name} passes secrets ({job['secrets']!r})")
    for s in strings(doc):
        for expr in EXPR.findall(s):
            for m in re.finditer(r"\bsecrets\b(\.\w+)?", expr):
                if m.group(1) != ".GITHUB_TOKEN":
                    problems.append(f"{PRE}: references `{m.group(0)}` in `${{{{{expr.strip()}}}}}`; "
                                    "only secrets.GITHUB_TOKEN may appear (CHARTS_PUBLISH_TOKEN "
                                    "writes the Marketplace index)")
        if PUBLISHER.split("/.github/")[1] in s:
            problems.append(f"{PRE}: names publish-chart.yaml")
    for script in ("publish-charts.sh", "stage-release-assets.sh"):
        calls = channel_calls(doc, script)
        if not calls:
            problems.append(f"{PRE}: never runs hack/{script}")
        for c in calls:
            if c != "prerelease":
                problems.append(f"{PRE}: runs hack/{script} with channel {c!r}, not 'prerelease'")
    if not re.search(r"hack/release-version\.sh derive\b", "\n".join(runs(doc))):
        problems.append(f"{PRE}: does not derive its version with hack/release-version.sh derive")
    problems += check_push_wiring(PRE, doc, text, "prerelease")
    return problems


def check_release(text):
    doc, problems = load(REL, text)
    if doc is None:
        return problems
    on = triggers(doc)
    if on != {"push": {"tags": [RELEASE_GLOB]}}:
        problems.append(f"{REL}: must trigger only on push of tags {RELEASE_GLOB!r} (GitHub matches "
                        f"the whole tag name, so it cannot match a pre-release), got {on!r}")
    steps = ((doc.get("jobs") or {}).get("package-and-push") or {}).get("steps") or []
    texts = [code(s.get("run", "")) if isinstance(s, dict) else "" for s in steps]
    check_at = [i for i, t in enumerate(texts)
                if re.search(r'hack/release-version\.sh check release "\$\{GITHUB_REF_NAME\}"', t)]
    # The push step is the one that runs hack/publish-charts.sh release (rule 14 put every push in there). Keyed on
    # the invocation in the step's code, never on its comments, and its absence is a failure, not a pass: up to
    # 2026-11-18 this keyed on "helm push", which the migration removed from every step.
    push_at = [i for i, t in enumerate(texts) if re.search(r"hack/publish-charts\.sh\s+release\b", t)]
    if not push_at:
        problems.append(f"{REL}: package-and-push has no step that runs hack/publish-charts.sh release, so nothing "
                        "can be checked to come after the tag-shape check")
    if not check_at:
        problems.append(f"{REL}: package-and-push never checks the tag with "
                        "`hack/release-version.sh check release \"${GITHUB_REF_NAME}\"`")
    elif push_at and check_at[0] > push_at[0]:
        problems.append(f"{REL}: checks the tag's shape only after it pushes")
    problems += check_push_wiring(REL, doc, text, "release")
    for script in ("publish-charts.sh", "stage-release-assets.sh"):
        calls = channel_calls(doc, script)
        if not calls:
            problems.append(f"{REL}: never runs hack/{script}")
        for c in calls:
            if c != "release":
                problems.append(f"{REL}: runs hack/{script} with channel {c!r}, not 'release'")
    problems += check_release_wiring(doc, text)
    return problems


# --- rule 12 (preflight) and rule 9 (pins) in release-tag.yaml ----------------------------------
CHARTS_WF = "krateo-blueprints/charts/.github/workflows/"
PIN = re.compile(r"^" + re.escape(CHARTS_WF) + r"(publish-chart|release-preflight)\.yaml@([0-9a-f]{40})$")


def needs_of(job):
    n = (job or {}).get("needs") or []
    return [n] if isinstance(n, str) else list(n)


def check_release_wiring(doc, text):
    """The preflight job exists, is called with the tag, and gates the push; both charts reusables
    are pinned to one full commit SHA with a version comment (PUBLISHING.md rules 9 and 12)."""
    problems = []
    jobs = doc.get("jobs") or {}
    pins = {}
    for job_name, wf in (("preflight", "release-preflight"), ("publish-index", "publish-chart")):
        uses = str((jobs.get(job_name) or {}).get("uses", ""))
        m = PIN.match(uses)
        if not m or m.group(1) != wf:
            problems.append(f"{REL}: job {job_name} must use {CHARTS_WF}{wf}.yaml@<40-hex commit>, "
                            f"got {uses!r}; a branch or tag can move under an org-wide token (rule 9)")
            continue
        pins[wf] = m.group(2)
        if not re.search(r"^\s*uses: " + re.escape(uses) + r" # \d+\.\d+\.\d+\s*$", text, re.M):
            problems.append(f"{REL}: {wf}.yaml's pin has no `# X.Y.Z` version comment (rule 9)")
    if len(set(pins.values())) > 1:
        problems.append(f"{REL}: publish-chart and release-preflight are pinned to different commits "
                        f"{pins}; they are one release of krateo-blueprints/charts")
    version = str(((jobs.get("preflight") or {}).get("with") or {}).get("version", ""))
    if version.replace(" ", "") != "${{github.ref_name}}":
        problems.append(f"{REL}: the preflight must get the tag as its version, got {version!r}")
    if "preflight" not in needs_of(jobs.get("package-and-push")):
        problems.append(f"{REL}: package-and-push does not need preflight, so a refused preflight would "
                        "still stamp and push (rule 12)")
    if "package-and-push" not in needs_of(jobs.get("publish-index")):
        problems.append(f"{REL}: publish-index does not need package-and-push")
    return problems


# --- hack/release-preflight ------------------------------------------------------------------------
HOOK = "release-preflight"
STUB_CURL = """#!/bin/sh
echo "$*" >> "$STUB_LOG"
case "$*" in
  *ghcr.io/token*) [ "$STUB_TOKEN" = fail ] && exit 22; echo '{"token":"t"}' ;;
  *manifests*) [ "$STUB_CODE" = unreachable ] && exit 6; printf '%s' "$STUB_CODE" ;;
  *) exit 2 ;;
esac
"""
STUB_GIT = '#!/bin/sh\n[ "$1 $2" = "rev-parse HEAD" ] && echo abcdef1234567890abcdef1234567890abcdef12 && exit 0\nexit 2\n'
HOOK_CASES = [
    # (version, GHCR answer, token, passes)
    ("0.3.74", "200", "ok", True),
    ("0.3.74", "404", "ok", False),
    ("0.3.74", "500", "ok", False),
    ("0.3.74", "unreachable", "ok", False),
    ("0.3.74", "200", "fail", False),
    ("0.3.74-main.gabc1234", "200", "ok", False),
    ("", "200", "ok", False),
]


def hook_behaviour(hook_text, version_sh_text):
    problems = []
    for version, code, token, want in HOOK_CASES:
        with tempfile.TemporaryDirectory() as tmp:
            for name, body in (("hack/" + HOOK, hook_text), ("hack/release-version.sh", version_sh_text),
                               ("bin/curl", STUB_CURL), ("bin/git", STUB_GIT)):
                p = os.path.join(tmp, name)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    f.write(body)
                os.chmod(p, 0o755)
            log = os.path.join(tmp, "curl.log")
            env = dict(os.environ, PATH=os.path.join(tmp, "bin") + os.pathsep + os.environ["PATH"],
                       STUB_LOG=log, STUB_CODE=code, STUB_TOKEN=token,
                       GITHUB_REPOSITORY_OWNER="Krateo-Blueprints")
            r = subprocess.run([os.path.join(tmp, "hack", HOOK), version], cwd=tmp, env=env,
                               capture_output=True, text=True)
            asked = open(log).read() if os.path.exists(log) else ""
            label = f"hack/{HOOK} {version!r} (GHCR {code}, token {token})"
            if (r.returncode == 0) != want:
                problems.append(f"{label}: {'passed' if r.returncode == 0 else 'refused'}, expected "
                                f"{'pass' if want else 'refusal'}")
            if want and "krateo-blueprints/charts/openstack/manifests/0.3.74-main.gabcdef1" not in asked:
                problems.append(f"{label}: did not ask GHCR for openstack 0.3.74-main.gabcdef1 "
                                f"(asked: {asked.strip()!r})")
            if (not version or PRE_RELEASE_SHAPED(version)) and asked:
                problems.append(f"{label}: probed GHCR for a version it should have refused first")
    return problems


def PRE_RELEASE_SHAPED(v):
    return re.search(r"-(main|dev)\.g[0-9a-f]{7}$", v) is not None


HOOK_MUTATIONS = [
    ("a 404 passes", "  200)\n", "  200|404)\n"),
    ("an unreachable registry passes", '|| code="unreachable"', '|| code="200"'),
    ("any other answer passes", "  *)\n    echo \"::error::GHCR answered", "  *)\n    exit 0\n    echo \"::error::GHCR answered"),
    ("probes the release tag, not the pre-release", 'pre="${version}-main.g${sha:0:7}"', 'pre="${version}"'),
    ("no version check", '"$here/release-version.sh" check release "$version"\n', ""),
    ("a failed token request passes", "refusing rather than releasing unchecked\"\n  exit 1\n}",
     "refusing rather than releasing unchecked\"\n  exit 0\n}"),
]


def check_only_release_indexes(workflows):
    # Parsed, so a comment that explains the publisher (prerelease.yaml's header does) is not a call.
    problems = []
    for name, text in sorted(workflows.items()):
        if name == REL:
            continue
        doc, errs = load(name, text)
        problems += errs
        if doc is not None and any("publish-chart.yaml" in s for s in strings(doc)):
            problems.append(f"{name}: names publish-chart.yaml; only {REL} may write the "
                            "Marketplace index")
    return problems


# --- version shapes -----------------------------------------------------------------------------
GOOD = {
    "release": ["0.3.74", "1.0.0", "10.20.300"],
    "prerelease": ["0.3.74-main.gabc1234", "0.3.74-dev.g0123456", "1.0.0-main.g0000000"],
}
BAD = {
    "release": ["0.3.74-main.gabc1234", "v0.3.74", "0.3.074", "0.3", "0.3.74.1", "0.3.74+x",
                "0.3.74-rc.1", ""],
    "prerelease": ["0.3.74", "0.3.74-main.abc1234", "0.3.74-main.gABC1234", "0.3.74-main.gabc12",
                   "0.3.74-main.gabc12345", "0.3.74-rc.1", "v0.3.74-main.gabc1234",
                   "0.3.74-main.gabc1234x", ""],
}


def version_table(script):
    problems = []
    for channel, versions in GOOD.items():
        for v in versions:
            r = subprocess.run(["bash", script, "check", channel, v], capture_output=True, text=True)
            if r.returncode != 0:
                problems.append(f"release-version.sh refuses {channel} {v!r}")
    for channel, versions in BAD.items():
        for v in versions:
            r = subprocess.run(["bash", script, "check", channel, v], capture_output=True, text=True)
            if r.returncode == 0:
                problems.append(f"release-version.sh accepts {channel} {v!r}")
    return problems


# --- the scripts check before they act ----------------------------------------------------------
def fixture(tmp, scripts):
    for rel, text in scripts.items():
        p = os.path.join(tmp, "hack", rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        os.chmod(p, 0o755)
    files = {
        "blueprints/demo/compositiondefinition.yaml": "spec:\n  chart:\n    version: CHART_VERSION\n",
        "blueprints/demo/chart/Chart.yaml": "apiVersion: v2\nname: demo\nversion: CHART_VERSION\n",
        "blueprints/openstack/node-reader-rbac.yaml": "kind: ClusterRole\n",
        "blueprints/openstack/portal-visibility-rbac.yaml": "kind: Role\n",
        "examples/demo.yaml": "apiVersion: composition.krateo.io/COMPOSITION_API_VERSION\n",
    }
    for rel, text in files.items():
        p = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
    stub = os.path.join(tmp, "bin", "helm")
    os.makedirs(os.path.dirname(stub))
    with open(stub, "w") as f:
        f.write('#!/bin/sh\necho "$@" >> "$HELM_LOG"\nexit 1\n')
    os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
    with open(os.path.join(tmp, "bin", "push.sh"), "w") as f:
        f.write('#!/bin/sh\necho "$@" >> "$HELM_LOG.push"\nexit 1\n')


def act(scripts, script, channel, version):
    """Run one script on a fresh throwaway tree; report what it did."""
    with tempfile.TemporaryDirectory() as tmp:
        fixture(tmp, scripts)
        log = os.path.join(tmp, "helm.log")
        env = dict(os.environ, PATH=os.path.join(tmp, "bin") + os.pathsep + os.environ["PATH"],
                   HELM_LOG=log, CHART_PUSH=os.path.join(tmp, "bin", "push.sh"))
        args = {"publish-charts.sh": [channel, version, "oci://example.invalid/charts", "dist"],
                "stage-release-assets.sh": [channel, version, "out"]}[script]
        r = subprocess.run(["bash", os.path.join("hack", script), *args], cwd=tmp, env=env,
                           capture_output=True, text=True)
        cd = open(os.path.join(tmp, "blueprints/demo/compositiondefinition.yaml")).read()
        return {
            "rc": r.returncode,
            "stamped": "CHART_VERSION" not in cd,
            "helm": os.path.exists(log),
            "out": os.path.isdir(os.path.join(tmp, "out")) or os.path.isdir(os.path.join(tmp, "dist")),
        }


WRONG = (("release", "0.3.74-main.gabc1234"), ("prerelease", "0.3.74"))
RIGHT = {"release": "0.3.74", "prerelease": "0.3.74-main.gabc1234"}


def scripts_check_first(scripts):
    problems = []
    for script in ("publish-charts.sh", "stage-release-assets.sh"):
        for channel, version in WRONG:
            got = act(scripts, script, channel, version)
            if got["rc"] == 0 or got["stamped"] or got["helm"] or got["out"]:
                problems.append(f"{script} {channel} {version}: acted on a version of the wrong "
                                f"shape ({got})")
        for channel, version in RIGHT.items():
            got = act(scripts, script, channel, version)
            # The positive control. Without it, "nothing was stamped" above could mean the fixture
            # is one the script never touches, and the refusal would be unobserved.
            did = got["helm"] if script == "publish-charts.sh" else got["out"]
            if not did:
                problems.append(f"{script} {channel} {version}: did nothing with a version of the "
                                f"right shape ({got}), so its refusals above prove nothing")
    return problems


# --- publish-charts.sh pushes through $CHART_PUSH, once per chart (rule 14) ---------------------
PUSH_HELM = r'''#!/bin/bash
echo "$*" >> "$HELM_LOG"
case "$1" in
  dependency) exit 0 ;;
  package)
    # helm package DIR --version V --destination DEST: an archive with the chart's name as its top directory
    dir=$2 v=$4 dest=$6
    name=$(awk '/^name:/{print $2; exit}' "$dir/Chart.yaml")
    t=$(mktemp -d); cp -R "$dir" "$t/$name"; tar czf "$dest/$name-$v.tgz" -C "$t" "$name"; rm -rf "$t" ;;
  show) exit 0 ;;
  *) exit 3 ;;
esac
'''
PUSH_STUB = r'''#!/bin/sh
echo "$1 $2 user=${REGISTRY_USERNAME:-} password=${REGISTRY_PASSWORD:+set}" >> "$PUSH_LOG"
case "$1" in *"${PUSH_FAIL_ON:-^}"*) exit 1 ;; esac
exit 0
'''


def push_once_per_chart(scripts):
    """publish-charts.sh, run on two blueprint charts with stand-ins for helm and push.sh: CHART_PUSH is called once per
    chart with its archive and the repository, with the registry credentials in its environment, helm never pushes, a
    push that fails stops the release with no second attempt, and with no CHART_PUSH nothing is stamped."""
    problems = []

    def go(env_extra, label):
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in scripts.items():
                p = os.path.join(tmp, "hack", rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "w").write(text)
                os.chmod(p, 0o755)
            for n in ("alpha", "beta"):
                files = {f"blueprints/{n}/compositiondefinition.yaml": "spec:\n  chart:\n    version: CHART_VERSION\n",
                         f"blueprints/{n}/chart/Chart.yaml": f"apiVersion: v2\nname: {n}\nversion: CHART_VERSION\n",
                         f"blueprints/{n}/chart/values.schema.json": "{}\n"}
                for rel, text in files.items():
                    os.makedirs(os.path.dirname(os.path.join(tmp, rel)), exist_ok=True)
                    open(os.path.join(tmp, rel), "w").write(text)
            os.makedirs(os.path.join(tmp, "bin"))
            for name, body in (("helm", PUSH_HELM), ("push.sh", PUSH_STUB)):
                open(os.path.join(tmp, "bin", name), "w").write(body)
                os.chmod(os.path.join(tmp, "bin", name), 0o755)
            env = dict(os.environ, PATH=os.path.join(tmp, "bin") + os.pathsep + os.environ["PATH"],
                       HELM_LOG=os.path.join(tmp, "helm.log"), PUSH_LOG=os.path.join(tmp, "push.log"),
                       CHART_PUSH=os.path.join(tmp, "bin", "push.sh"), REGISTRY_USERNAME="bot", REGISTRY_PASSWORD="t")
            for k, v in env_extra.items():
                if v is None:
                    env.pop(k, None)
                else:
                    env[k] = v
            r = subprocess.run(["bash", "hack/publish-charts.sh", "release", "0.3.74", "oci://example.invalid/charts",
                                "dist"], cwd=tmp, env=env, capture_output=True, text=True)
            read = lambda p: open(p).read().splitlines() if os.path.exists(p) else []
            return (r, read(os.path.join(tmp, "push.log")), read(os.path.join(tmp, "helm.log")),
                    "CHART_VERSION" in open(os.path.join(tmp, "blueprints/alpha/compositiondefinition.yaml")).read())

    r, pushes, helm, _ = go({}, "both")
    want = sorted("%s-0.3.74.tgz oci://example.invalid/charts user=bot password=set" % n for n in ("alpha", "beta"))
    if r.returncode != 0 or sorted(pushes) != want or any(l.startswith("push") for l in helm):
        problems.append(f"publish-charts.sh: pushes {pushes}, helm calls {helm}, rc {r.returncode}, stderr "
                        f"{r.stderr[-300:]!r}; want $CHART_PUSH once per chart ({want}) and no helm push")
    r, pushes, _, _ = go({"PUSH_FAIL_ON": "beta-"}, "fail")
    if r.returncode == 0 or sum("beta-" in l for l in pushes) != 1:
        problems.append(f"publish-charts.sh: with beta's push failing, rc {r.returncode} and pushes {pushes}; want a "
                        "failure after exactly one attempt at beta (push.sh retries only GHCR's tag race itself)")
    r, pushes, helm, unstamped = go({"CHART_PUSH": None}, "missing")
    if r.returncode == 0 or pushes or helm or not unstamped:
        problems.append(f"publish-charts.sh: with no CHART_PUSH, rc {r.returncode}, pushes {pushes}, helm {helm}, "
                        f"CD still unstamped {unstamped}; want a refusal before anything is stamped")
    return problems


PUBLISH_MUTATIONS = [
    ("pushes with helm push again", 'if ! bash "${CHART_PUSH}" "${name}-${VERSION}.tgz" "${OCI_REPO}"; then',
     'if ! helm push "${name}-${VERSION}.tgz" "${OCI_REPO}"; then'),
    ("a failed push is ignored", 'if ! bash "${CHART_PUSH}" "${name}-${VERSION}.tgz" "${OCI_REPO}"; then',
     'if ! { bash "${CHART_PUSH}" "${name}-${VERSION}.tgz" "${OCI_REPO}" || true; }; then'),
    ("no CHART_PUSH check", 'if [ -z "${CHART_PUSH:-}" ] || [ ! -f "${CHART_PUSH}" ]; then', "if false; then"),
]


# --- mutations ----------------------------------------------------------------------------------
def mutate(text, old, new, label):
    if old not in text:
        measurement_failed(f"mutation {label!r} cannot apply: {old!r} is not in the file any more; "
                           "update the mutation to the file's current text")
    return text.replace(old, new, 1)


PRE_MUTATIONS = [
    ("contents: write", "  contents: read\n", "  contents: write\n"),
    ("job-level permissions", "    runs-on: ubuntu-latest\n",
     "    runs-on: ubuntu-latest\n    permissions:\n      contents: write\n"),
    ("CHARTS_PUBLISH_TOKEN in a step", "          BASE: ${{ github.event.inputs.base }}\n",
     "          BASE: ${{ github.event.inputs.base }}\n"
     "          T: ${{ secrets.CHARTS_PUBLISH_TOKEN }}\n"),
    ("toJSON(secrets) in a run", 'echo "pre-release ${version} of ${sha}"',
     "echo '${{ toJSON(secrets) }}'"),
    ("a publish-index job", "\njobs:\n",
     "\njobs:\n  publish-index:\n    needs: publish\n"
     f"    uses: {PUBLISHER}@0000000000000000000000000000000000000000\n"
     "    with:\n      channel: blueprints\n"
     "    secrets:\n      PUBLISH_TOKEN: ${{ secrets.CHARTS_PUBLISH_TOKEN }}\n"),
    ("secrets: inherit to a reusable", "\njobs:\n",
     "\njobs:\n  other:\n    uses: ./.github/workflows/lint.yaml\n    secrets: inherit\n"),
    ("publish-charts with the release channel", "hack/publish-charts.sh prerelease",
     "hack/publish-charts.sh release"),
    ("stage-release-assets with the release channel", "hack/stage-release-assets.sh prerelease",
     "hack/stage-release-assets.sh release"),
    ("version not derived", "hack/release-version.sh derive", "echo"),
    ("prerelease's push without CHART_PUSH", "          CHART_PUSH: ${{ steps.chart-push.outputs.path }}\n", ""),
    ("prerelease pushes with helm push", 'run: hack/publish-charts.sh prerelease "${VERSION}" "${OCI_REPO}" charts-dist',
     'run: |\n          hack/publish-charts.sh prerelease "${VERSION}" "${OCI_REPO}" charts-dist\n'
     '          helm push extra.tgz "${OCI_REPO}"'),
]
# Rule 14's mutants for release-tag.yaml. "Only a comment" is the one that proves comments cannot satisfy the gate:
# the push step's command is commented out, so its text is still in the run block, and nothing pushes.
PUSH_STEP = ('      - name: Package and push every chart\n'
             '        env:\n'
             '          CHART_PUSH: ${{ steps.chart-push.outputs.path }}\n'
             '          REGISTRY_USERNAME: ${{ github.actor }}\n'
             '          REGISTRY_PASSWORD: ${{ secrets.GITHUB_TOKEN }}\n'
             '        run: hack/publish-charts.sh release "${VERSION}" "${OCI_REPO}" charts-dist\n')
PRE_PIN, PRE_COMMENT = current_pin(REL, "release-preflight.yaml")
PUB_PIN, PUB_COMMENT = current_pin(REL, "publish-chart.yaml")
CP_PIN, CP_COMMENT = current_pin(REL, "actions/chart-push")
OTHER_COMMIT = "0" * 40  # a commit no pin names, so "different commits" always differs
REL_PUSH_MUTATIONS = [
    ("the push step removed", PUSH_STEP, ""),
    ("the push step only a comment", 'run: hack/publish-charts.sh release "${VERSION}" "${OCI_REPO}" charts-dist',
     'run: |\n          # hack/publish-charts.sh release "${VERSION}" "${OCI_REPO}" charts-dist\n          true'),
    ("helm push beside the script", 'run: hack/publish-charts.sh release "${VERSION}" "${OCI_REPO}" charts-dist',
     'run: |\n          hack/publish-charts.sh release "${VERSION}" "${OCI_REPO}" charts-dist\n'
     '          helm push extra.tgz "${OCI_REPO}"'),
    ("chart-push pinned to a branch", CP_PIN + CP_COMMENT, "actions/chart-push@main"),
    ("chart-push pin without its comment", CP_PIN + CP_COMMENT, CP_PIN),
    ("no chart-push step", "        id: chart-push\n", ""),
    ("CHART_PUSH not passed", "          CHART_PUSH: ${{ steps.chart-push.outputs.path }}\n", ""),
    ("no registry password for the read-back", "          REGISTRY_PASSWORD: ${{ secrets.GITHUB_TOKEN }}\n", ""),
]
REL_MUTATIONS = [
    ("trigger loosened to any suffix", f'      - "{RELEASE_GLOB}"\n', f'      - "{RELEASE_GLOB}*"\n'),
    ("a second trigger", "\non:\n", "\non:\n  workflow_dispatch:\n"),
    ("tag shape not checked", 'run: hack/release-version.sh check release "${GITHUB_REF_NAME}"',
     'run: "true"'),
    ("publish-charts with the prerelease channel", "hack/publish-charts.sh release",
     "hack/publish-charts.sh prerelease"),
    ("stage-release-assets with the prerelease channel", "hack/stage-release-assets.sh release",
     "hack/stage-release-assets.sh prerelease"),
    ("preflight dropped from needs", "    needs:\n      - preflight\n", ""),
    ("preflight pinned to a branch", PRE_PIN, "release-preflight.yaml@main"),
    ("publisher pinned to a tag", PUB_PIN + PUB_COMMENT, "publish-chart.yaml@" + PUB_COMMENT.replace("#", "").strip()),
    ("publisher and preflight at different commits", PUB_PIN, "publish-chart.yaml@" + OTHER_COMMIT),
    ("preflight version comment dropped", PRE_PIN + PRE_COMMENT, PRE_PIN),
    ("preflight given a fixed version", "      version: ${{ github.ref_name }}\n",
     "      version: main\n"),
    ("publish-index no longer needs package-and-push", "    needs: package-and-push\n", ""),
]
VERSION_MUTATIONS = [
    ("release pattern unanchored", 'RELEASE_RE="^${CORE}\\$"', 'RELEASE_RE="^${CORE}"'),
    ("prerelease suffix loosened", "\\.g[0-9a-f]{7}", "\\..*"),
    ("release check always passes", '[[ "$version" =~ $RELEASE_RE ]]', "true"),
    ("prerelease check always passes", '[[ "$version" =~ $PRERELEASE_RE ]]', "true"),
]
CHECK_LINE = '"$(dirname "$0")/release-version.sh" check "$CHANNEL" "$VERSION"'


# --- prune-prereleases.yaml and hack/prune-prereleases.py ---------------------------------------
PRUNE = "prune-prereleases.yaml"
PRUNE_SECRET = "PRERELEASE_PRUNE_TOKEN"


def check_prune(text):
    doc, problems = load(PRUNE, text)
    if doc is None:
        return problems
    if doc.get("permissions") != {"contents": "read"}:
        problems.append(f"{PRUNE}: workflow permissions must be exactly contents: read, got "
                        f"{doc.get('permissions')!r}; deletion goes through {PRUNE_SECRET} only")
    jobs = doc.get("jobs") or {}
    if not jobs:
        problems.append(f"{PRUNE}: has no jobs")
    for name, job in jobs.items():
        job = job or {}
        for key in ("permissions", "uses", "secrets"):
            if key in job:
                problems.append(f"{PRUNE}: job {name} sets `{key}`")
    for s in strings(doc):
        for expr in EXPR.findall(s):
            for m in re.finditer(r"\bsecrets\b(\.\w+)?", expr):
                if m.group(1) != "." + PRUNE_SECRET:
                    problems.append(f"{PRUNE}: references `{m.group(0)}`; only secrets.{PRUNE_SECRET} "
                                    "may appear")
    on = triggers(doc)
    inputs = ((on.get("workflow_dispatch") or {}).get("inputs") or {})
    if (inputs.get("dry-run") or {}).get("default") is not True:
        problems.append(f"{PRUNE}: a manual run must default to dry-run (input `dry-run`, default true)")
    steps = [s for job in jobs.values() for s in ((job or {}).get("steps") or [])
             if isinstance(s, dict)]
    texts = [s.get("run", "") for s in steps]
    guard = [i for i, t in enumerate(texts) if re.search(r'-z "\$\{PRUNE_TOKEN\}"', t)]
    prune = [i for i, t in enumerate(texts) if "hack/prune-prereleases.py" in t]
    if not prune:
        problems.append(f"{PRUNE}: never runs hack/prune-prereleases.py")
    if not guard or (prune and guard[0] > prune[0]):
        problems.append(f"{PRUNE}: does not refuse to run without {PRUNE_SECRET} before pruning")
    for i in prune:
        dry = str((steps[i].get("env") or {}).get("DRY_RUN", ""))
        if "workflow_dispatch" not in dry or "dry-run" not in dry:
            problems.append(f"{PRUNE}: the prune step's DRY_RUN does not follow the manual dry-run input")
    return problems


def load_prune(source):
    ns = {"__name__": "prune_prereleases", "__file__": os.path.join(ROOT, "hack", "prune-prereleases.py")}
    exec(compile(source, "prune-prereleases.py", "exec"), ns)
    return ns


DELETABLE = [
    (["0.3.74-main.gabc1234"], True),
    (["0.3.74-dev.g0123abc"], True),
    (["0.3.74-main.gabc1234", "0.3.74-main.gdef5678"], True),
    (["0.3.73"], False),
    (["0.3.74-main.gabc1234", "0.3.74"], False),
    ([], False),
    (["latest"], False),
    (["0.3.74-rc.1"], False),
    (["sha256-0123abcd"], False),
]


class FakeGitHub:
    def __init__(self):
        self.deleted = []

    def delete(self, name, version_id):
        self.deleted.append((name, version_id))


def prune_behaviour(ns):
    problems = []
    for tags, want in DELETABLE:
        if bool(ns["deletable"](tags)) != want:
            problems.append(f"prune-prereleases.py: deletable({tags}) is {not want}, expected {want}")
    # Six pre-releases and two releases, in two packages; keep 2.
    def v(i, tag, day):
        return {"id": i, "created_at": f"2026-11-{day:02d}T00:00:00Z",
                "metadata": {"container": {"tags": [tag]}}}
    pre = [f"0.3.74-main.g{n:07x}" for n in range(1, 7)]
    packages = {
        "openstack": [v(1, "0.3.72", 1), v(2, "0.3.73", 2)] + [v(10 + k, t, 3 + k) for k, t in enumerate(pre)],
        "keystone": [v(3, "0.3.73", 2)] + [v(20 + k, t, 3 + k) for k, t in enumerate(pre)],
    }
    kept, doomed = ns["plan"](packages, 2)
    if kept != set(pre[-2:]):
        problems.append(f"prune-prereleases.py: plan keeps {sorted(kept)}, expected the newest two")
    doomed_tags = {t for _, ver in doomed for t in ns["tags_of"](ver)}
    if doomed_tags != set(pre[:-2]) or len(doomed) != 8:
        problems.append(f"prune-prereleases.py: plan deletes {sorted(doomed_tags)} ({len(doomed)} "
                        "versions), expected the four oldest pre-releases in both packages")
    gh = FakeGitHub()
    try:
        ns["delete_version"](gh, "openstack", v(2, "0.3.73", 2))
        problems.append("prune-prereleases.py: delete_version accepted a release version")
    except RuntimeError:
        pass
    if gh.deleted:
        problems.append(f"prune-prereleases.py: delete_version deleted {gh.deleted} for a release")
    ns["delete_version"](gh, "openstack", v(10, pre[0], 3))
    if gh.deleted != [("openstack", 10)]:
        problems.append("prune-prereleases.py: delete_version did not delete a pre-release (the "
                        "positive control: without it, its refusal above proves nothing)")
    return problems


PRUNE_MUTATIONS = [
    ("contents: write", "  contents: read\n", "  contents: write\n"),
    ("packages: write added", "  contents: read\n", "  contents: read\n  packages: write\n"),
    ("another secret", "          KEEP: ${{ github.event.inputs.keep || '5' }}\n",
     "          KEEP: ${{ github.event.inputs.keep || '5' }}\n"
     "          T: ${{ secrets.CHARTS_PUBLISH_TOKEN }}\n"),
    ("dry-run defaults off", "        type: boolean\n        required: true\n        default: true\n",
     "        type: boolean\n        required: true\n        default: false\n"),
    ("no token refusal", 'if [ -z "${PRUNE_TOKEN}" ]; then', "if false; then"),
    ("DRY_RUN ignores the input",
     "DRY_RUN: ${{ github.event_name == 'workflow_dispatch' && github.event.inputs.dry-run != 'false' }}",
     "DRY_RUN: false"),
    ("a reusable job", "\njobs:\n", "\njobs:\n  other:\n    uses: ./.github/workflows/lint.yaml\n"),
]
PRUNE_SCRIPT_MUTATIONS = [
    ("any pre-release tag suffices", 'return all(shape("prerelease", t) for t in tags)',
     'return any(shape("prerelease", t) for t in tags)'),
    ("untagged is deletable", "    if not tags:\n        return False\n", ""),
    ("delete_version trusts the plan", "    if not deletable(tags):\n        raise RuntimeError(",
     "    if False:\n        raise RuntimeError("),
    ("plan keeps the oldest", "key=lambda t: (newest[t], t), reverse=True)",
     "key=lambda t: (newest[t], t))"),
]


def main():
    workflows = {}
    for name in sorted(os.listdir(WF)) if os.path.isdir(WF) else []:
        if name.endswith((".yaml", ".yml")):
            workflows[name] = open(os.path.join(WF, name)).read()
    for need in (PRE, REL, PRUNE):
        if need not in workflows:
            measurement_failed(f"no .github/workflows/{need}")
    prune_py = os.path.join(ROOT, "hack", "prune-prereleases.py")
    if not os.path.isfile(prune_py):
        measurement_failed("no hack/prune-prereleases.py")
    prune_src = open(prune_py).read()
    scripts = {}
    for s in SCRIPTS:
        p = os.path.join(ROOT, "hack", s)
        if not os.path.isfile(p):
            measurement_failed(f"no hack/{s}")
        scripts[s] = open(p).read()

    hook_path = os.path.join(ROOT, "hack", HOOK)
    if not os.path.isfile(hook_path):
        measurement_failed(f"no hack/{HOOK}")
    hook_src = open(hook_path).read()

    problems = []
    if not os.access(hook_path, os.X_OK):
        problems.append(f"hack/{HOOK} is not executable, so charts release-preflight.yaml would refuse "
                        "every release; commit it with mode 100755")
    problems += check_prerelease(workflows[PRE])
    problems += check_release(workflows[REL])
    problems += check_only_release_indexes(workflows)
    with tempfile.TemporaryDirectory() as tmp:
        vs = os.path.join(tmp, "release-version.sh")
        with open(vs, "w") as f:
            f.write(scripts["release-version.sh"])
        problems += version_table(vs)
    problems += scripts_check_first(scripts)
    problems += hook_behaviour(hook_src, scripts["release-version.sh"])
    problems += check_prune(workflows[PRUNE])
    problems += prune_behaviour(load_prune(prune_src))

    survived = []
    for label, old, new in HOOK_MUTATIONS:
        if not hook_behaviour(mutate(hook_src, old, new, label), scripts["release-version.sh"]):
            survived.append(f"hack/{HOOK}: {label}")
    for label, old, new in PRUNE_MUTATIONS:
        if not check_prune(mutate(workflows[PRUNE], old, new, label)):
            survived.append(f"{PRUNE}: {label}")
    for label, old, new in PRUNE_SCRIPT_MUTATIONS:
        if not prune_behaviour(load_prune(mutate(prune_src, old, new, label))):
            survived.append(f"prune-prereleases.py: {label}")
    for label, old, new in PRE_MUTATIONS:
        if not check_prerelease(mutate(workflows[PRE], old, new, label)):
            survived.append(f"{PRE}: {label}")
    for label, old, new in REL_MUTATIONS + REL_PUSH_MUTATIONS:
        if not check_release(mutate(workflows[REL], old, new, label)):
            survived.append(f"{REL}: {label}")
    # The push step removed must be caught by the push-step check itself, not only by a neighbouring one that
    # happens to read the same step (the "never runs hack/publish-charts.sh" channel check).
    removed = check_release(mutate(workflows[REL], PUSH_STEP, "", "the push step removed"))
    if not any("has no step that runs hack/publish-charts.sh release" in p for p in removed) \
            or not any("needs exactly one step that runs hack/publish-charts.sh release" in p for p in removed):
        survived.append(f"{REL}: the push step removed, as seen by the push-step checks ({removed})")
    problems += push_once_per_chart(scripts)
    for label, old, new in PUBLISH_MUTATIONS:
        m = dict(scripts)
        m["publish-charts.sh"] = mutate(scripts["publish-charts.sh"], old, new, label)
        if not push_once_per_chart(m):
            survived.append(f"publish-charts.sh: {label}")
    lint = dict(workflows)
    if "lint.yaml" not in workflows:
        measurement_failed("no .github/workflows/lint.yaml to mutate")
    lint["lint.yaml"] = mutate(workflows["lint.yaml"], "\njobs:\n",
                               f"\njobs:\n  index:\n    uses: {PUBLISHER}@x\n",
                               "another workflow calling publish-chart.yaml")
    if not check_only_release_indexes(lint):
        survived.append("another workflow naming publish-chart.yaml")
    for label, old, new in VERSION_MUTATIONS:
        with tempfile.TemporaryDirectory() as tmp:
            vs = os.path.join(tmp, "release-version.sh")
            with open(vs, "w") as f:
                f.write(mutate(scripts["release-version.sh"], old, new, label))
            if not version_table(vs):
                survived.append(f"release-version.sh: {label}")
    for s in ("publish-charts.sh", "stage-release-assets.sh"):
        m = dict(scripts)
        m[s] = mutate(scripts[s], CHECK_LINE, ":", f"{s} without its check")
        if not scripts_check_first(m):
            survived.append(f"{s}: version check removed")

    n_mut = (len(PRE_MUTATIONS) + len(REL_MUTATIONS) + 1 + len(VERSION_MUTATIONS) + 2
             + len(PRUNE_MUTATIONS) + len(PRUNE_SCRIPT_MUTATIONS) + len(HOOK_MUTATIONS)
             + len(REL_PUSH_MUTATIONS) + 1 + len(PUBLISH_MUTATIONS))
    for p in problems:
        print(f"  - {p}")
    for s in survived:
        print(f"  - MUTATION SURVIVED: {s} (the check that should catch it cannot fail)")
    if problems or survived:
        print(f"FAIL: {len(problems)} problem(s), {len(survived)} of {n_mut} mutation(s) survived")
        return 1
    print(f"OK: {PRE} cannot write the Marketplace index and {REL} publishes only plain X.Y.Z; "
          f"version table {sum(map(len, GOOD.values())) + sum(map(len, BAD.values()))} cases; "
          f"all {n_mut} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
