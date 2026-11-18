#!/usr/bin/env python3
"""Every gate that pip-installs a library the images ship installs it against the images' constraints, and checks it.

    hack/validate-pip-constrained.py [repo-root]

On 2026-11-18 oslo.service 4.9.0 reached PyPI; the worker-probe gate installed oslo.service unpinned, picked it up, and
failed on every pull request. The two other gates that installed image libraries pinned versions the images do not
ship (Django 5.2.19 and cryptography 46.0.5 against the horizon image's 4.2.28 and 43.0.3; psutil 7.0.0 against the nova
image's 7.2.2). So, for every hack/gates.d/*.yaml whose `setup` runs `pip install`:

  - each package it installs other than PyYAML (the gates' own parser, not an image library) must be installed with
    `-c "$(python3 hack/constraints.py url)"`, with no version of its own (the constraint is the version);
  - its `run` must call `python3 hack/constraints.py check` naming exactly those packages, before anything else runs.

The rule is checked against a constrained, an unconstrained, a self-pinned and an unchecked example first, so a change
that loosens it fails here. Fails when no gate installs anything (nothing measured).
"""
import os
import re
import sys

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so no gate file was read")
    sys.exit(2)

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
CONSTRAINT = '-c "$(python3 hack/constraints.py url)"'
EXEMPT = {"pyyaml"}


def installs(setup):
    """[(packages, constrained)] for each `pip install` command in a setup script (continuations joined)."""
    text = re.sub(r"\\\n\s*", " ", setup or "")
    out = []
    for line in text.splitlines():
        m = re.search(r"\bpip3?\s+install\b(.*)$", line)
        if not m:
            continue
        rest = m.group(1)
        constrained = CONSTRAINT in rest
        rest = rest.replace(CONSTRAINT, " ")
        pkgs = [t.strip("'\"") for t in rest.split() if not t.startswith("-")]
        out.append((pkgs, constrained))
    return out


def judge(setup, run):
    """[problem] for one gate's setup and run."""
    problems, names = [], []
    for pkgs, constrained in installs(setup):
        image = [p for p in pkgs if re.split(r"[=<>!~\[]", p)[0].lower() not in EXEMPT]
        if not image:
            continue
        if not constrained:
            problems.append("installs %s without %s" % (", ".join(image), CONSTRAINT))
        pinned = [p for p in image if re.search(r"[=<>!~]", p)]
        if pinned:
            problems.append("pins %s itself; the constraint is the version" % ", ".join(pinned))
        names += [re.split(r"[=<>!~\[]", p)[0] for p in image]
    if names:
        first = next((l.strip() for l in (run or "").splitlines() if l.strip()), "")
        m = re.match(r"^python3 hack/constraints\.py check (.+)$", first)
        if not m:
            problems.append("its run does not start with `python3 hack/constraints.py check %s`" % " ".join(names))
        elif sorted(m.group(1).split()) != sorted(names):
            problems.append("checks %s but installs %s" % (sorted(m.group(1).split()), sorted(names)))
    return problems, names


CASES = [
    ("constrained and checked", 'pip install -c "$(python3 hack/constraints.py url)" psutil',
     "python3 hack/constraints.py check psutil\npython3 hack/x.py .", 0),
    ("unconstrained", 'pip install psutil', "python3 hack/constraints.py check psutil", 1),
    ("self-pinned", 'pip install -c "$(python3 hack/constraints.py url)" "psutil==7.0.0"',
     "python3 hack/constraints.py check psutil", 1),
    ("not checked", 'pip install -c "$(python3 hack/constraints.py url)" psutil', "python3 hack/x.py .", 1),
    ("checked for another package", 'pip install -c "$(python3 hack/constraints.py url)" psutil Django',
     "python3 hack/constraints.py check psutil", 1),
    ("PyYAML only", "pip install PyYAML", "python3 hack/x.py .", 0),
]


def main():
    bad = []
    for desc, setup, run, want in CASES:
        got = len(judge(setup, run)[0]) > 0
        if got != bool(want):
            bad.append("%s: judged %s, want %s" % (desc, "refused" if got else "allowed", "refused" if want else "allowed"))
    for b in bad:
        print("FAIL (rule): " + b)
    if bad:
        return 1
    d = os.path.join(ROOT, "hack", "gates.d")
    files = sorted(f for f in os.listdir(d) if f.endswith(".yaml")) if os.path.isdir(d) else []
    problems, measured = [], []
    for f in files:
        g = yaml.safe_load(open(os.path.join(d, f))) or {}
        p, names = judge(g.get("setup"), g.get("run"))
        problems += ["hack/gates.d/%s %s" % (f, x) for x in p]
        if names:
            measured.append("%s (%s)" % (f, ", ".join(names)))
    if not measured:
        print("FAILED MEASUREMENT: no gate installs an image library, so nothing was checked")
        return 2
    for p in problems:
        print("FAIL: " + p)
    if problems:
        return 1
    print("OK: the rule holds in all %d cases; %d gate(s) install image libraries against the constraints and check "
          "them: %s" % (len(CASES), len(measured), "; ".join(measured)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
