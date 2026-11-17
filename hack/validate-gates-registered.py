#!/usr/bin/env python3
"""Every gate script is registered in hack/gates.d, and every registration runs (lint.yaml's runner steps).

    hack/validate-gates-registered.py [repo-root]

Gates are one file each in hack/gates.d/ and lint.yaml's jobs run them through hack/run-gates.py (see that script).
A gate written and never registered is a gate that never runs, which looks exactly like a gate that passes. So:

  1. every hack/validate-*.py and hack/validate-*.sh is named (as hack/<file>) in the `run` of exactly one gate file;
  2. every hack/... or tools/... script a gate file's `run` or `setup` names exists;
  3. every gate file has name, job, order (an integer) and run, and no other key than run-gates.py knows; PyYAML reads
     it exactly as run-gates.py's own parser does; and no two gates of one job share an order;
  4. every gate's job is a lint.yaml job with one runner step, `python3 hack/run-gates.py --job <job> .`, which sets
     every name the gate lists under workflow-env; every job with a runner step has at least one gate;
  5. no lint.yaml step runs a hack/validate-* script itself: a gate lives in gates.d, or two copies drift.

Fails when PyYAML is missing, and when there is no gate file or no validate script (no input is not a pass).
"""
import importlib.util
import os
import re
import sys

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")
    sys.exit(2)

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
HACK = os.path.join(ROOT, "hack")
WF = os.path.join(ROOT, ".github", "workflows", "lint.yaml")
REQUIRED = ("name", "job", "order", "run")
SCRIPT = re.compile(r"\b((?:hack|tools)/[A-Za-z0-9_./-]+\.(?:py|sh))\b")
failures = []


def runner():
    spec = importlib.util.spec_from_file_location("run_gates", os.path.join(HACK, "run-gates.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    rg = runner()
    gd = os.path.join(HACK, "gates.d")
    files = sorted(f for f in os.listdir(gd) if f.endswith(".yaml")) if os.path.isdir(gd) else []
    scripts = sorted(f for f in os.listdir(HACK) if re.fullmatch(r"validate-.+\.(py|sh)", f))
    if not files:
        failures.append("hack/gates.d holds no gate file, so lint.yaml runs nothing")
    if not scripts:
        failures.append("hack/ holds no validate-* script; this check is looking in the wrong place")
    wf = yaml.safe_load(open(WF))
    runner_steps = {}
    for jname, job in (wf.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            run = str(step.get("run") or "")
            m = re.fullmatch(r"python3 hack/run-gates\.py --job ([a-z0-9-]+) \.", run.strip())
            if m:
                if m.group(1) != jname:
                    failures.append("lint.yaml job %s runs the gates of job %s" % (jname, m.group(1)))
                if jname in runner_steps:
                    failures.append("lint.yaml job %s has two runner steps" % jname)
                runner_steps[jname] = step
            elif re.search(r"hack/validate-", run):
                failures.append("lint.yaml job %s, step %r runs a validate script itself; move it to hack/gates.d"
                                % (jname, step.get("name")))
    named = {s: [] for s in scripts}
    orders, jobs_with_gates = {}, set()
    for f in files:
        text = open(os.path.join(gd, f)).read()
        where = "hack/gates.d/" + f
        try:
            g = rg.parse(text, where)
        except ValueError as e:
            failures.append(str(e))
            continue
        try:
            same = yaml.safe_load(text) == g
        except yaml.YAMLError as e:
            failures.append("%s: not valid YAML: %s" % (where, str(e).splitlines()[0]))
            continue
        if not same:
            failures.append("%s: PyYAML and run-gates.py read it differently; use only the forms run-gates.py "
                            "documents" % where)
        for k in REQUIRED:
            if k not in g:
                failures.append("%s: no %r" % (where, k))
        for k in g:
            if k not in rg.KEYS:
                failures.append("%s: unknown key %r (run-gates.py knows %s)" % (where, k, ", ".join(rg.KEYS)))
        if not isinstance(g.get("order"), int):
            failures.append("%s: order %r is not an integer" % (where, g.get("order")))
        job = g.get("job")
        jobs_with_gates.add(job)
        key = (job, g.get("order"))
        if key in orders:
            failures.append("%s and %s: both are job %s, order %s" % (orders[key], where, job, g.get("order")))
        orders[key] = where
        step = runner_steps.get(job)
        if step is None:
            failures.append("%s: job %r has no runner step in lint.yaml, so this gate never runs" % (where, job))
        else:
            env = step.get("env") or {}
            for v in g.get("workflow-env") or []:
                if v not in env:
                    failures.append("%s: needs %s, which job %s's runner step does not set" % (where, v, job))
        for s in SCRIPT.findall(str(g.get("run", "")) + "\n" + str(g.get("setup", ""))):
            if not os.path.isfile(os.path.join(ROOT, s)):
                failures.append("%s: runs %s, which does not exist" % (where, s))
            base = os.path.basename(s)
            if s.startswith("hack/") and base in named and where not in named[base]:
                named[base].append(where)
    for s, where in named.items():
        if not where:
            failures.append("hack/%s is registered in no hack/gates.d file, so CI never runs it" % s)
        elif len(where) > 1:
            failures.append("hack/%s is registered %d times (%s); once is the rule" % (s, len(where), ", ".join(where)))
    for jname in sorted(set(runner_steps) - jobs_with_gates):
        failures.append("lint.yaml job %s has a runner step and no gate" % jname)
    if failures:
        for f in failures:
            print("FAIL: " + f)
        return 1
    print("OK: %d gate file(s) across %d job(s); all %d validate script(s) registered once; every script named exists"
          % (len(files), len(runner_steps), len(scripts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
