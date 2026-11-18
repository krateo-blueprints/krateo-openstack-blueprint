#!/usr/bin/env python3
"""No pull request title, body or commit message carries a marker that makes GitHub skip workflows.

    hack/validate-no-skip-markers.py [repo-root]
    hack/validate-no-skip-markers.py [repo-root] --self-test

GitHub skips every push and pull_request workflow for a commit whose message contains one of its skip markers, the
bracketed "skip ci" family or a "skip-checks: true" trailer, wherever it appears in the message, even quoted. #347's
squash commit (ca645b7) quoted the bot's marker in its body, so main ran no workflow at all for it, lint included,
and prerelease and release-tag need a green lint on the exact commit. A squash message is the pull request's title
and body, or its commit messages, so all of them are read. The render-log bot no longer uses a marker either (its
loop guard is render-log.yaml's paths filter), so nothing is exempt.

What is read, from the environment lint.yaml's job-hooks runner step sets:
  - EVENT_NAME pull_request: PR_TITLE, PR_BODY, and every commit message between BASE and HEAD;
  - EVENT_NAME push or merge_group: every commit message between BASE (the previous head, or the merge group's
    base) and HEAD. In a merge group each queued pull request is its squash commit, whose message is the final one,
    so this is where a marker in the message that would land is seen;
  - anything else (workflow_dispatch on a branch): every commit message since the branch left origin/main.
Markers are matched as GitHub matches them, ignoring case and the space inside the brackets. To name one in prose, say
"the skip marker" instead. The matcher is checked case by case first (--self-test runs only that).
"""
import os
import re
import subprocess
import sys

ZERO = "0" * 40
# GitHub's markers ([skip ci], [ci skip], [no ci], [skip actions], [actions skip]) and the skip-checks trailer.
MARKER = re.compile(r"\[\s*(?:skip\s+ci|ci\s+skip|no\s+ci|skip\s+actions|actions\s+skip)\s*\]"
                    r"|^\s*skip-checks\s*:\s*true\s*$", re.I | re.M)

CASES = [
    ("an ordinary message", "fix(x): a change\n\nWhy it changed.", False),
    ("[skip ci] in a title", "docs: a change [skip ci]", True),
    ("[ci skip] in a body", "fix: x\n\nbody [ci skip] more", True),
    ("[no ci]", "fix: x [no ci]", True),
    ("[skip actions]", "fix: x [skip actions]", True),
    ("[actions skip]", "fix: x [actions skip]", True),
    ("upper case", "fix: x [SKIP CI]", True),
    ("quoted in backticks, as #347's body did", "the bot's commit says `[skip ci]`, so", True),
    ("a skip-checks trailer", "fix: x\n\nskip-checks: true", True),
    ("the words without brackets", "the skip marker; skip ci runs elsewhere", False),
    ("skip-checks: false", "fix: x\n\nskip-checks: false", False),
]


def self_test():
    return ["%s: matched %s, want %s" % (d, not w, w) for d, text, w in CASES if bool(MARKER.search(text)) != w]


def git(root, *a):
    r = subprocess.run(["git", "-C", root, *a], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(a), r.stderr.strip()))
    return r.stdout


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = os.path.abspath(args[0] if args else ".")
    bad = self_test()
    for b in bad:
        print("FAIL (matcher): " + b)
    if bad:
        return 1
    print("OK: the matcher holds in all %d cases" % len(CASES))
    if "--self-test" in sys.argv:
        return 0
    event, base = os.environ.get("EVENT_NAME", ""), os.environ.get("BASE", "")
    texts = []
    try:
        if event == "pull_request":
            if not base:
                print("FAILED MEASUREMENT: a pull request with no BASE commit; nothing to read")
                return 2
            texts += [("the pull request's title", os.environ.get("PR_TITLE", "")),
                      ("the pull request's body", os.environ.get("PR_BODY", ""))]
        elif event not in ("push", "merge_group") or not base or base == ZERO:
            base = git(root, "merge-base", "origin/main", "HEAD").strip()
        for c in git(root, "rev-list", "%s..HEAD" % base).split():
            texts.append(("commit %s" % c[:7], git(root, "show", "-s", "--format=%B", c)))
    except RuntimeError as e:
        print("FAILED MEASUREMENT: %s" % e)
        return 2
    found = []
    for where, text in texts:
        m = MARKER.search(text or "")
        if m:
            found.append("%s carries %r" % (where, m.group(0).strip()))
    if found:
        for f in found:
            print("FAIL: %s, which makes GitHub skip every workflow for the commit it reaches (a squash message is "
                  "the title and body, or the commit messages). Say \"the skip marker\" instead." % f)
        return 1
    print("OK: %d text(s) read (%s), none carries a skip marker" % (len(texts), event or "branch"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
