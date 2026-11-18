#!/usr/bin/env python3
"""hack/require-lint-passed.sh accepts a release commit only with a successful lint run on that exact commit.

    hack/validate-require-lint-passed.py [repo-root]

release-tag.yaml (and prerelease.yaml) publish only a commit that lint passed on. With the merge queue, a
commit that lands is linted twice under its own SHA: by its merge group's run (merge_group, GITHUB_SHA the group's
SHA) and by the push to main. The script must accept a successful run from either, or from a dispatched run (the
recovery it names), and nothing else. It is run against a stand-in gh that answers the runs listing from a fixture:

  - a successful merge_group run, a successful push run, a successful dispatched run: each accepted alone;
  - a failed push run beside a successful merge_group run: accepted;
  - only a successful pull_request run for that SHA: refused (a pull request's head is another commit);
  - only a failed run: refused, "never succeeded";
  - no run at all: refused, the never-triggered case;
  - a successful run whose head_sha is another commit (an API that ignored the filter): refused;
  - the listing must ask for that SHA (head_sha=<sha>).

Fails when bash or jq is missing.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
SHA = "1111111111111111111111111111111111111111"
OTHER = "2222222222222222222222222222222222222222"
STUB = r'''#!/bin/sh
echo "$*" >> "$GH_LOG"
case "$1" in
  api) cat "$GH_FIXTURE" ;;
  *) echo "stub gh: unexpected $*" >&2; exit 3 ;;
esac
'''


def run(event, conclusion, sha=SHA):
    return {"event": event, "status": "completed", "conclusion": conclusion, "head_sha": sha}


CASES = [
    ("a successful merge_group run", [run("merge_group", "success")], True, ""),
    ("a successful push run", [run("push", "success")], True, ""),
    ("a successful dispatched run", [run("workflow_dispatch", "success")], True, ""),
    ("a failed push run beside a successful merge_group run",
     [run("push", "failure"), run("merge_group", "success")], True, ""),
    ("only a successful pull_request run", [run("pull_request", "success")], False, "No merge_group, push"),
    ("only a failed merge_group run", [run("merge_group", "failure")], False, "never succeeded"),
    ("no run at all", [], False, "never-triggered"),
    ("a successful run for another commit", [run("push", "success", OTHER)], False, "No merge_group, push"),
]


def main():
    for tool in ("bash", "jq"):
        if shutil.which(tool) is None:
            print("FAILED MEASUREMENT: %s not on PATH" % tool)
            return 2
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir)
        open(os.path.join(bindir, "gh"), "w").write(STUB)
        os.chmod(os.path.join(bindir, "gh"), 0o755)
        for desc, runs, accept, says in CASES:
            fixture, log = os.path.join(tmp, "runs.json"), os.path.join(tmp, "gh.log")
            json.dump({"total_count": len(runs), "workflow_runs": runs}, open(fixture, "w"))
            open(log, "w").close()
            env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"], GH_FIXTURE=fixture, GH_LOG=log)
            r = subprocess.run(["bash", os.path.join(ROOT, "hack", "require-lint-passed.sh"), "o/r", SHA],
                               env=env, capture_output=True, text=True)
            out = r.stdout + r.stderr
            calls = open(log).read()
            if (r.returncode == 0) != accept:
                failures.append("%s: %s (rc %d), want %s: %s" % (desc, "accepted" if r.returncode == 0 else "refused",
                                                                r.returncode, "accepted" if accept else "refused",
                                                                out.strip()[-200:]))
            elif says and says not in out:
                failures.append("%s: refused without saying %r: %s" % (desc, says, out.strip()[-200:]))
            if "head_sha=%s" % SHA not in calls:
                failures.append("%s: the runs listing did not ask for head_sha=%s (%s)" % (desc, SHA, calls.strip()))
    if failures:
        for f in failures:
            print("FAIL: " + f)
        return 1
    print("OK: require-lint-passed.sh, %d cases: a successful merge_group, push or dispatched run on that exact commit "
          "is accepted; a pull_request run, a failed run, no run, or another commit's run is refused" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
