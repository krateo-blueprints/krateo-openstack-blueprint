#!/usr/bin/env python3
"""Every version claim about the per-cluster capabilities chart names the release it was really
first published at, and the source of that fact is the release history, not prose.

Why this exists. #218 moved the capability ClusterRoles and the compute-tier guard out of the
umbrella into the per-cluster chart `openstack-capabilities`. It was written for 0.3.47, slipped to
0.3.48, and every doc and template comment it added still said "from 0.3.47" / "up to 0.3.46".
openstack-capabilities 0.3.47 was never published, so a README reader at BP_VERSION=0.3.47 was
told to `helm upgrade --install` a version that does not exist, and told to skip that step only up
to 0.3.46 (#230).

One source. `krateo.io/first-release` in blueprints/openstack/capabilities/Chart.yaml is the
version the chart was first released at. This gate checks it against git: it must be the FIRST tag
(in version order) whose tree contains that Chart.yaml, or, before any tag does, a version newer
than every tag (the release about to be cut). The release before it is LAST_UMBRELLA, the last
release whose umbrella still rendered the objects itself.

Then every capability-context claim in the docs and comments must agree:
  "from X", "Published from X", "since X", "BP_VERSION=X  # or later"  -> X == FIRST
  "up to X", "upgraded from X", "Upgrading from X", "X or earlier",
  "a X install"                                                      -> X == LAST_UMBRELLA
A line that records a MEASUREMENT ("measured", "Measured") is history and is left alone.

Needs the tags: the CI step checks out with fetch-depth 0. With no tags visible it FAILS rather
than passing on nothing (a gate with no input must fail).
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
CHART = "blueprints/openstack/capabilities/Chart.yaml"
FILES = [
    "README.md",
    "docs/configuration.md",
    "docs/overview.md",
    "docs/api.md",
    CHART,
    "blueprints/openstack/capabilities/templates/_capabilities.tpl",
    "blueprints/openstack/chart/templates/_capabilities.tpl",
    "blueprints/openstack/chart/templates/capability-handoff.yaml",
    "hack/validate-capability-roles.py",
    ".github/workflows/lint.yaml",
]
CONTEXT = re.compile(r"capabilit|per-cluster|openstack-view|compute-tier|take-?ownership|handoff|hand.over|"
                     r"own release|umbrella rendered|umbrella's four|change owner", re.I)
VER = r"(\d+\.\d+\.\d+)"
FIRST_PATTERNS = [
    re.compile(r"(?i)\bpublished from " + VER),
    re.compile(r"(?i)(?<!upgraded )(?<!upgrading )(?<!upgrade )\bfrom " + VER + r"(?!\s+or earlier)"),
    re.compile(r"(?i)\bsince " + VER),
    re.compile(r"BP_VERSION=" + VER + r"\s+#\s*or later"),
]
PREV_PATTERNS = [
    re.compile(r"(?i)\bup to " + VER),
    re.compile(r"(?i)\bupgrad(?:ed|ing|e) from " + VER),
    re.compile(VER + r" or earlier"),
    re.compile(r"(?i)\ba " + VER + r" install\b"),
]
MEASURED = re.compile(r"\bmeasured\b", re.I)


def vkey(v):
    return tuple(int(x) for x in v.split("."))


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


def main():
    problems = []
    chart = (ROOT / CHART).read_text()
    m = re.search(r'^\s+krateo\.io/first-release:\s*"?' + VER + r'"?\s*$', chart, re.M)
    if not m:
        print(f"FAIL: {CHART} has no annotations.krateo.io/first-release")
        return 1
    first = m.group(1)

    tags = sorted((t for t in git("tag", "--list").stdout.split() if re.fullmatch(r"\d+\.\d+\.\d+", t)), key=vkey)
    if not tags:
        print("FAIL: no release tags visible. This gate needs the tags (checkout with fetch-depth: 0); "
              "with none it cannot know which release the chart first appeared in.")
        return 1
    containing = [t for t in tags if git("cat-file", "-e", f"{t}:{CHART}").returncode == 0]
    if containing:
        truth = containing[0]
        if first != truth:
            problems.append(f"{CHART}: krateo.io/first-release is {first}, but the first release tag containing "
                            f"the chart is {truth}")
    elif vkey(first) <= vkey(tags[-1]):
        problems.append(f"{CHART}: krateo.io/first-release is {first}, but no tag up to {tags[-1]} contains the "
                        f"chart, so its first release is still to come and must be newer than {tags[-1]}")
    earlier = [t for t in tags if vkey(t) < vkey(first)]
    if not earlier:
        print(f"FAIL: no release before {first}: cannot name the last umbrella-owned release")
        return 1
    prev = earlier[-1]

    claims = 0
    for rel in FILES:
        p = ROOT / rel
        if not p.exists():
            problems.append(f"{rel}: listed claim file is missing")
            continue
        lines = p.read_text().splitlines()
        # Scan by PARAGRAPH (blank, bare "#" or comment-delimiter lines separate them), joined so a
        # claim that wraps ("From\n0.3.48") is still seen, and so context never leaks across paragraphs.
        paras, cur = [], []
        for i, line in enumerate(lines):
            if line.strip() in ("", "#", "*/}}", "{{/*", "-->", "<!--", '"""') or line.strip().startswith("```"):
                if cur: paras.append(cur); cur = []
                continue
            cur.append((i + 1, line))
        if cur: paras.append(cur)
        for para in paras:
            text = " ".join(re.sub(r"^\s*#\s?", "", l) for _, l in para)
            if not CONTEXT.search(text):
                continue
            starts, pos = [], 0
            for n, l in para:
                starts.append((pos, n)); pos += len(re.sub(r"^\s*#\s?", "", l)) + 1
            def lineno(off):
                return [n for o, n in starts if o <= off][-1]
            for pats, want, what in ((FIRST_PATTERNS, first, "first release"), (PREV_PATTERNS, prev, "last umbrella-owned release")):
                for pat in pats:
                    for mm in pat.finditer(text):
                        ln = lineno(mm.start())
                        if MEASURED.search(dict(para)[ln]):
                            continue
                        claims += 1
                        if mm.group(1) != want:
                            problems.append(f"{rel}:{ln}: says {mm.group(0)!r}; the {what} is {want}")
    if claims == 0:
        problems.append("no capability version claims found in any listed file: the scan matched nothing, "
                        "which is a broken gate, not a clean tree")
    if problems:
        print(f"FAIL: first release {first}, last umbrella-owned release {prev}:")
        for x in problems:
            print("  - " + x)
        return 1
    print(f"OK: openstack-capabilities first released at {first} (git agrees); {claims} claim(s) in "
          f"{len(FILES)} files name {first} or {prev} as they should")
    return 0


if __name__ == "__main__":
    sys.exit(main())
