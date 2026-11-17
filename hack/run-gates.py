#!/usr/bin/env python3
"""Run one lint.yaml job's gates, as declared in hack/gates.d/.

    hack/run-gates.py --job <job> [repo-root]     run every gate whose `job:` is <job>, in `order`
    hack/run-gates.py --list [repo-root]          print every gate as JSON lines (job, order, file, name, ...)

Gate steps used to be listed in .github/workflows/lint.yaml, so every pull request that added a gate edited the same
file in the same places and branches in flight conflicted there. Now each gate is one file, hack/gates.d/<gate>.yaml,
and each lint.yaml job has one step that runs this script. The jobs stay as they were: they exist for parallelism and
for the tools each one installs. hack/validate-gates-registered.py keeps the two in step.

A gate file holds, in block style only:
  name               what the gate asserts (was the step name)
  job                the lint.yaml job that runs it
  order              an integer; gates of one job run in ascending order
  run                the command, a block scalar, run as GitHub runs a step: bash --noprofile --norc -eo pipefail
  setup              optional; a command run just before `run`, at the same point (was a step of its own)
  working-directory  optional; relative to the repo root
  workflow-env       optional; names of variables the job's runner step sets from a GitHub expression (${{ }}),
                     which this script cannot evaluate. A gate fails if one is not in its environment.
Comments above the keys say why the gate exists.

Every gate of the job runs even after one fails, so one run reports all of them; the script then exits 1. It reads the
files with its own small parser (only the forms above), so it needs no PyYAML in the jobs that install none;
validate-gates-registered.py checks that PyYAML reads every file the same way.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

KEYS = ("name", "job", "order", "run", "setup", "working-directory", "workflow-env")


def parse(text, where="gate file"):
    """The restricted YAML of a gate file: scalars (plain, double-quoted or int), `|` block scalars, scalar lists."""
    out, lines, i = {}, text.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = re.match(r"^([a-z-]+):(?: (.*))?$", line)
        if not m:
            raise ValueError("%s: line %d is not `key: value`: %r" % (where, i + 1, line))
        key, val = m.group(1), (m.group(2) or "").strip()
        if key in out:
            raise ValueError("%s: key %r twice" % (where, key))
        i += 1
        if val == "|":
            block = []
            while i < len(lines) and (lines[i].startswith("  ") or not lines[i].strip()):
                block.append(lines[i][2:] if lines[i].strip() else "")
                i += 1
            while block and not block[-1]:
                block.pop()
            out[key] = "\n".join(block) + "\n"
        elif val == "":
            items = []
            while i < len(lines) and lines[i].startswith("  - "):
                items.append(lines[i][4:].strip())
                i += 1
            out[key] = items
        elif val.startswith('"'):
            out[key] = json.loads(val)
        elif re.fullmatch(r"-?\d+", val):
            out[key] = int(val)
        else:
            out[key] = val
    return out


def load(root):
    gd = os.path.join(root, "hack", "gates.d")
    gates = []
    for f in sorted(os.listdir(gd)) if os.path.isdir(gd) else []:
        if f.endswith(".yaml"):
            g = parse(open(os.path.join(gd, f)).read(), "hack/gates.d/" + f)
            g["file"] = f
            gates.append(g)
    return gates


def run_bash(script, cwd, env):
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(script)
    try:
        return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", fh.name], cwd=cwd, env=env).returncode
    finally:
        os.unlink(fh.name)


def main():
    args = sys.argv[1:]
    job = None
    if "--job" in args:
        k = args.index("--job")
        job = args[k + 1]
        del args[k:k + 2]
    listing = "--list" in args
    args = [a for a in args if a != "--list"]
    root = os.path.abspath(args[0] if args else ".")
    gates = load(root)
    if listing:
        for g in sorted(gates, key=lambda g: (g["job"], g["order"])):
            print(json.dumps({k: g.get(k) for k in ("job", "order", "file") + KEYS if k in g}, sort_keys=True))
        return 0
    if not job:
        print("usage: hack/run-gates.py --job <job> [repo-root] | --list [repo-root]")
        return 2
    mine = sorted((g for g in gates if g.get("job") == job), key=lambda g: g["order"])
    if not mine:
        print("::error::no hack/gates.d file has job: %s, so this job checks nothing" % job)
        return 1
    failed = []
    for g in mine:
        title = "%s (hack/gates.d/%s)" % (g["name"], g["file"])
        print("::group::" + title, flush=True)
        env = dict(os.environ)
        missing = [v for v in g.get("workflow-env") or [] if v not in env]
        cwd = os.path.join(root, g.get("working-directory") or ".")
        if missing:
            print("lint.yaml's runner step for job %s does not set %s" % (job, ", ".join(missing)))
            rc = 1
        else:
            rc = run_bash(g["setup"], cwd, env) if g.get("setup") else 0
            if rc == 0:
                rc = run_bash(g["run"], cwd, env)
        print("::endgroup::", flush=True)
        if rc != 0:
            failed.append(title)
            print("::error title=gate failed::%s exited %d" % (title, rc), flush=True)
        else:
            print("passed: " + title, flush=True)
    print("%d of %d gate(s) of job %s passed" % (len(mine) - len(failed), len(mine), job))
    for t in failed:
        print("FAILED: " + t)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
