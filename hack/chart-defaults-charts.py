#!/usr/bin/env python3
"""The charts lint.yaml's chart-defaults job checks, derived from the tree.

    hack/chart-defaults-charts.py [--matrix | --self-test] [repo-root]

Every directory holding a Chart.yaml is a chart, except a vendored subchart (one under another chart's charts/).
A chart with a values.schema.json is a Composition: it is in the matrix. A chart without one must be listed, with a
reason, in hack/chart-defaults-exempt.yaml, so a new chart cannot skip the check unseen. Fails on:

  - a chart with no values.schema.json that is not listed;
  - a listed path that is not a chart, or whose chart has a values.schema.json (stale: drop the entry);
  - an entry with no reason, or a path listed twice;
  - no Composition chart at all (a matrix of nothing is not a pass).

--matrix prints {"include": [{"dir": ...}, ...]} for GitHub's matrix. Without a flag it prints the verdict.
"""
import json
import os
import sys
import tempfile

import yaml

EXEMPT = "hack/chart-defaults-exempt.yaml"


def charts(root):
    found = []
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d != ".git")
        if "Chart.yaml" not in fns:
            continue
        rel = os.path.relpath(dp, root).replace(os.sep, "/")
        parent = os.path.dirname(rel)
        if os.path.basename(parent) == "charts" and os.path.isfile(os.path.join(root, os.path.dirname(parent), "Chart.yaml")):
            continue  # a vendored subchart of the chart above
        found.append((rel, os.path.isfile(os.path.join(dp, "values.schema.json"))))
    return found


def check(root):
    """Return (composition dirs, failures)."""
    fails = []
    path = os.path.join(root, EXEMPT)
    try:
        doc = yaml.safe_load(open(path)) or {}
    except (OSError, yaml.YAMLError) as e:
        return [], ["%s: cannot read: %s" % (EXEMPT, e)]
    entries = doc.get("exempt")
    if not isinstance(entries, list):
        return [], ["%s: `exempt` must be a list (empty is `exempt: []`)" % EXEMPT]
    listed = {}
    for e in entries:
        p = e.get("path") if isinstance(e, dict) else None
        if not p or not isinstance(e.get("reason"), str) or not e["reason"].strip():
            fails.append("%s: every entry needs path and reason: %r" % (EXEMPT, e))
        elif p in listed:
            fails.append("%s: %s listed twice" % (EXEMPT, p))
        else:
            listed[p] = e
    comp, schemaless = [], []
    for rel, has_schema in charts(root):
        (comp if has_schema else schemaless).append(rel)
    for rel in schemaless:
        if rel not in listed:
            fails.append("%s has a Chart.yaml and no values.schema.json, so the chart-defaults check cannot run on it. If it "
                         "is a Composition, give it a values.schema.json; if not, list it in %s with a reason." % (rel, EXEMPT))
    for p in listed:
        if p in comp:
            fails.append("%s lists %s, which has a values.schema.json now: drop the entry" % (EXEMPT, p))
        elif p not in schemaless:
            fails.append("%s lists %s, which is not a chart directory: drop the entry" % (EXEMPT, p))
    if not comp:
        fails.append("no chart with a values.schema.json under %s: the check would read nothing" % root)
    return comp, fails


def self_test():
    def build(files):
        d = tempfile.mkdtemp()
        for rel, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
            open(os.path.join(d, rel), "w").write(text)
        return d
    ok = "exempt:\n  - path: lib\n    reason: library\n"
    base = {"a/Chart.yaml": "", "a/values.schema.json": "{}", "lib/Chart.yaml": "", EXEMPT: ok}
    cases = [
        ("clean", base, None),
        ("new schemaless chart", dict(base, **{"b/Chart.yaml": ""}), "b has a Chart.yaml and no values.schema.json"),
        ("stale: gained a schema", dict(base, **{"lib/values.schema.json": "{}"}), "drop the entry"),
        ("stale: not a chart", dict(base, **{EXEMPT: ok + "  - path: gone\n    reason: x\n"}), "not a chart directory"),
        ("no reason", dict(base, **{EXEMPT: "exempt:\n  - path: lib\n    reason: ''\n"}), "needs path and reason"),
        ("nothing to check", {"lib/Chart.yaml": "", EXEMPT: ok}, "would read nothing"),
        ("vendored subchart ignored", dict(base, **{"a/charts/sub/Chart.yaml": ""}), None),
    ]
    bad = 0
    for name, files, want in cases:
        comp, fails = check(build(files))
        good = (not fails) if want is None else any(want in f for f in fails)
        print("%s: %s" % ("ok  " if good else "FAIL", name))
        bad += not good
    return 1 if bad else 0


def main():
    args = sys.argv[1:]
    mode = None
    for f in ("--matrix", "--self-test"):
        if f in args:
            mode = f
            args.remove(f)
    if mode == "--self-test":
        return self_test()
    root = args[0] if args else "."
    comp, fails = check(root)
    if fails:
        for f in fails:
            print("::error::" + f, file=sys.stderr)
        return 1
    if mode == "--matrix":
        print(json.dumps({"include": [{"dir": c} for c in comp]}))
    else:
        print("%d Composition chart(s) in the chart-defaults matrix; every other chart is listed in %s" % (len(comp), EXEMPT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
