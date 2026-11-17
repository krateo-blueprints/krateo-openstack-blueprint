#!/usr/bin/env python3
"""A pre-release never reaches the Marketplace index, and a release is never a pre-release version.

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


def runs(doc):
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                yield step["run"]


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
    texts = [s.get("run", "") if isinstance(s, dict) else "" for s in steps]
    check_at = [i for i, t in enumerate(texts)
                if re.search(r'hack/release-version\.sh check release "\$\{GITHUB_REF_NAME\}"', t)]
    push_at = [i for i, t in enumerate(texts) if "hack/publish-charts.sh" in t or "helm push" in t]
    if not check_at:
        problems.append(f"{REL}: package-and-push never checks the tag with "
                        "`hack/release-version.sh check release \"${GITHUB_REF_NAME}\"`")
    elif push_at and check_at[0] > push_at[0]:
        problems.append(f"{REL}: checks the tag's shape only after it pushes")
    for script in ("publish-charts.sh", "stage-release-assets.sh"):
        calls = channel_calls(doc, script)
        if not calls:
            problems.append(f"{REL}: never runs hack/{script}")
        for c in calls:
            if c != "release":
                problems.append(f"{REL}: runs hack/{script} with channel {c!r}, not 'release'")
    return problems


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


def act(scripts, script, channel, version):
    """Run one script on a fresh throwaway tree; report what it did."""
    with tempfile.TemporaryDirectory() as tmp:
        fixture(tmp, scripts)
        log = os.path.join(tmp, "helm.log")
        env = dict(os.environ, PATH=os.path.join(tmp, "bin") + os.pathsep + os.environ["PATH"],
                   HELM_LOG=log)
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
]
VERSION_MUTATIONS = [
    ("release pattern unanchored", 'RELEASE_RE="^${CORE}\\$"', 'RELEASE_RE="^${CORE}"'),
    ("prerelease suffix loosened", "\\.g[0-9a-f]{7}", "\\..*"),
    ("release check always passes", '[[ "$version" =~ $RELEASE_RE ]]', "true"),
    ("prerelease check always passes", '[[ "$version" =~ $PRERELEASE_RE ]]', "true"),
]
CHECK_LINE = '"$(dirname "$0")/release-version.sh" check "$CHANNEL" "$VERSION"'


def main():
    workflows = {}
    for name in sorted(os.listdir(WF)) if os.path.isdir(WF) else []:
        if name.endswith((".yaml", ".yml")):
            workflows[name] = open(os.path.join(WF, name)).read()
    for need in (PRE, REL):
        if need not in workflows:
            measurement_failed(f"no .github/workflows/{need}")
    scripts = {}
    for s in SCRIPTS:
        p = os.path.join(ROOT, "hack", s)
        if not os.path.isfile(p):
            measurement_failed(f"no hack/{s}")
        scripts[s] = open(p).read()

    problems = []
    problems += check_prerelease(workflows[PRE])
    problems += check_release(workflows[REL])
    problems += check_only_release_indexes(workflows)
    with tempfile.TemporaryDirectory() as tmp:
        vs = os.path.join(tmp, "release-version.sh")
        with open(vs, "w") as f:
            f.write(scripts["release-version.sh"])
        problems += version_table(vs)
    problems += scripts_check_first(scripts)

    survived = []
    for label, old, new in PRE_MUTATIONS:
        if not check_prerelease(mutate(workflows[PRE], old, new, label)):
            survived.append(f"{PRE}: {label}")
    for label, old, new in REL_MUTATIONS:
        if not check_release(mutate(workflows[REL], old, new, label)):
            survived.append(f"{REL}: {label}")
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

    n_mut = (len(PRE_MUTATIONS) + len(REL_MUTATIONS) + 1 + len(VERSION_MUTATIONS) + 2)
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
