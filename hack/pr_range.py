"""Where a pull request's own change starts, from an EXPLICIT base -- shared by the pull-request gates.

    import pr_range   (importlib from hack/, as the gates load their helpers)
    pr_range.base_ref(event, argv) -> (ref, how)        raises pr_range.NoBase
    pr_range.change_base(root, ref) -> sha              the merge base of `ref` and HEAD

A change is measured from the merge base of HEAD and the branch it targets. That is exact for every shape HEAD takes:
the merge ref GitHub checks out on pull_request (its first parent is the target's tip, so the merge base is that tip),
the pull request's own head, and a head that is itself a merge of the target into the branch. The target is never
inferred: on pull_request it is origin/$GITHUB_BASE_REF, which GitHub sets on every pull_request run; a dispatched run
declares origin/main; run by hand, `--base <ref>` is required, or LOG_BASE=<ref> in the environment for a run through
hack/run-gates.py (which passes a gate no arguments). With neither, NoBase: the gate fails, it never guesses.

It used to be inferred from HEAD's shape: a two-parent HEAD was taken for the merge ref and measured from its first
parent. A branch head that had merged main is also two-parent, with the BRANCH as first parent, so the change was
measured from the branch's previous commit and main's merged commits were charged to it: hack/validate-log-untouched.py
failed #412's head locally for main's render of docs/log.md, which the branch never touched. (merge_group and push
measure commit by commit from their fixed BASE, merge_group.base_sha or the previous head, and need none of this.)
"""
import os
import subprocess


class NoBase(Exception):
    pass


def base_ref(event, argv):
    """(ref, how) for a pull_request, a workflow_dispatch, or a run by hand; NoBase when there is no explicit base."""
    if event == "pull_request":
        ref = os.environ.get("GITHUB_BASE_REF", "")
        if not ref:
            raise NoBase("a pull_request run with no GITHUB_BASE_REF; the base is never inferred")
        return "origin/" + ref, "GITHUB_BASE_REF"
    if event == "workflow_dispatch":
        return "origin/main", "a dispatched run's declared base"
    if event:
        raise NoBase("EVENT_NAME %r has no pull-request range" % event)
    for i, a in enumerate(argv):
        if a == "--base" and i + 1 < len(argv):
            return argv[i + 1], "--base"
        if a.startswith("--base="):
            return a.split("=", 1)[1], "--base"
    # LOG_BASE: the same explicit base for a run through hack/run-gates.py, which passes no arguments to a gate.
    if os.environ.get("LOG_BASE"):
        return os.environ["LOG_BASE"], "LOG_BASE"
    raise NoBase("run by hand, the gate needs --base <ref> or LOG_BASE=<ref> (e.g. origin/main): it does not guess "
                 "what your branch is measured against")


def change_base(root, ref):
    r = subprocess.run(["git", "-C", root, "merge-base", ref, "HEAD"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("git merge-base %s HEAD: %s" % (ref, r.stderr.strip()))
    return r.stdout.strip()


def positional(argv):
    """argv's positional arguments, skipping --base's value."""
    out = []
    for i, a in enumerate(argv):
        if a.startswith("--") or (i and argv[i - 1] == "--base"):
            continue
        out.append(a)
    return out
