#!/usr/bin/env python3
"""The images' upper-constraints, for the gates that pip-install what the images ship.

    hack/constraints.py url                     the constraints file at hack/openstack-constraints.yaml's SHA
    hack/constraints.py check NAME...           each NAME is installed at exactly its constraint, else exit 1
    hack/constraints.py self-test NAME          positive control: NAME installed WITHOUT the constraints, in a
                                                throwaway venv, must fail `check`

A gate installs with `pip install -c "$(python3 hack/constraints.py url)" NAME...` and runs `check NAME...` before
its tests, so a test never runs against a version the images do not ship. `check` fails, never warns, when a name has
no constraint, is not installed, or is installed at another version. `self-test` installs NAME unconstrained (the
latest release, or, if that happens to equal the constraint, the release below it) with --no-deps in a venv and
requires `check` to refuse it there: a check that cannot see an unconstrained install guards nothing.
"""
import importlib.metadata
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch  # noqa: E402

PIN = os.path.join(HERE, "openstack-constraints.yaml")


def pin():
    vals = {}
    for line in open(PIN):
        m = re.match(r"^(repository|branch|sha):\s*(\S+)\s*$", line)
        if m:
            vals[m.group(1)] = m.group(2)
    if not re.fullmatch(r"[0-9a-f]{40}", vals.get("sha", "")):
        sys.exit("FAILED MEASUREMENT: %s names no 40-character commit sha" % PIN)
    return vals


def url():
    p = pin()
    return "https://raw.githubusercontent.com/%s/%s/upper-constraints.txt" % (p["repository"], p["sha"])


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def constraints():
    text = fetch.get(url()).decode()
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)===([^;\s]+)", line.strip())
        if m:
            out[norm(m.group(1))] = m.group(2)
    if len(out) < 100:
        sys.exit("FAILED MEASUREMENT: %s parsed to %d constraints" % (url(), len(out)))
    return out


def check(names, python=None):
    """[(name, problem)]; with python, read the versions installed for that interpreter."""
    want = constraints()
    if python:
        code = ("import importlib.metadata as m, sys\n"
                "for n in sys.argv[1:]:\n"
                "    try: print(n, m.version(n))\n"
                "    except m.PackageNotFoundError: print(n, '-')\n")
        r = subprocess.run([python, "-c", code, *names], capture_output=True, text=True, check=True)
        have = dict(l.split(" ", 1) for l in r.stdout.splitlines())
    else:
        have = {}
        for n in names:
            try:
                have[n] = importlib.metadata.version(n)
            except importlib.metadata.PackageNotFoundError:
                have[n] = "-"
    bad = []
    for n in names:
        c = want.get(norm(n))
        if c is None:
            bad.append((n, "has no constraint in %s" % url()))
        elif have.get(n) == "-":
            bad.append((n, "is not installed (its constraint is %s)" % c))
        elif have.get(n) != c:
            bad.append((n, "is installed at %s, its constraint is %s" % (have.get(n), c)))
    return bad


def self_test(name):
    c = constraints().get(norm(name))
    if c is None:
        sys.exit("FAILED MEASUREMENT: %s has no constraint, so the control has nothing to differ from" % name)
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([sys.executable, "-m", "venv", tmp], check=True)
        py = os.path.join(tmp, "bin", "python")
        pip = [py, "-m", "pip", "install", "-q", "--disable-pip-version-check", "--no-deps"]
        subprocess.run(pip + [name], check=True)  # deliberately unconstrained: the latest release
        got = check([name], python=py)
        if not got:
            subprocess.run(pip + ["%s<%s" % (name, c)], check=True)  # the latest WAS the constraint: go below it
            got = check([name], python=py)
        if not got:
            print("FAIL: an unconstrained install of %s passed the check; it guards nothing" % name)
            return 1
        print("OK: positive control: an unconstrained %s is refused (%s)" % (name, got[0][1]))
        return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("url", "check", "self-test"):
        sys.exit("usage: hack/constraints.py url | check NAME... | self-test NAME")
    cmd, names = sys.argv[1], sys.argv[2:]
    if cmd == "url":
        print(url())
        return 0
    if cmd == "self-test":
        if len(names) != 1:
            sys.exit("usage: hack/constraints.py self-test NAME")
        return self_test(names[0])
    if not names:
        sys.exit("FAILED MEASUREMENT: check names no package")
    bad = check(names)
    for n, problem in bad:
        print("FAIL: %s %s: a test must run against the images' version" % (n, problem))
    if bad:
        return 1
    print("OK: %d package(s) installed at the images' constraints (%s): %s"
          % (len(names), pin()["branch"], ", ".join(names)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
