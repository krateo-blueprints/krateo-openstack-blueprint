#!/usr/bin/env python3
"""A pull request never touches docs/log.md; only the render-log bot's own pull request does.

    hack/validate-log-untouched.py [repo-root]

docs/log.md is rendered from docs/log.d on main, after each merge, by .github/workflows/render-log.yaml, which lands it
through a pull request of its own (hack/render-log-pr.sh). Any other pull request that edits or regenerates it
conflicts with every branch in flight, which is what #340's committed render still did. So the change being judged
must leave docs/log.md alone, unless it is the bot's: its branch starts with bot/render-log- AND its author is
github-actions[bot]. Both, because each is easy to claim alone: anyone can name a branch, and the author proves who
opened it.

What is judged, from the environment lint.yaml's job-hooks runner step sets:
  - EVENT_NAME pull_request: the files changed between BASE (the base commit) and HEAD, the branch HEAD_REF, the
    author PR_AUTHOR;
  - EVENT_NAME push: each commit between BASE (the previous head) and HEAD that touches docs/log.md must be authored
    by the bot (its squash merge); a range with no previous head (a new branch) is compared with origin/main;
  - anything else (workflow_dispatch on a branch): the files changed since the branch left origin/main, judged as a
    pull request by GITHUB_ACTOR from GITHUB_REF_NAME.
The rule itself is checked first, case by case (--self-test runs only that), so a change to it that widens the
exemption fails here before it can pass a real run. Fails when no range can be determined.
"""
import os
import subprocess
import sys

LOG = "docs/log.md"
BOT_PREFIX = "bot/render-log-"
BOT_LOGIN = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
ZERO = "0" * 40


def judge(files, branch, author):
    """None if the change may stand, else the reason it may not."""
    if LOG not in files:
        return None
    if branch.startswith(BOT_PREFIX) and author == BOT_LOGIN:
        return None
    return ("it changes %s, which only the render-log bot writes (its branch %s* and author %s; this is branch %r by "
            "%r). Add a docs/log.d entry instead, and take main's docs/log.md: git checkout origin/main -- %s"
            % (LOG, BOT_PREFIX, BOT_LOGIN, branch, author, LOG))


CASES = [
    # (description, files, branch, author, allowed)
    ("an ordinary change", ["hack/x.py", "docs/log.d/2026-11-18-01-x.md"], "fix/x", "someone", True),
    ("a change that edits docs/log.md", ["docs/log.md", "docs/log.d/2026-11-18-01-x.md"], "fix/x", "someone", False),
    ("the bot's own pull request", ["docs/log.md"], "bot/render-log-1234567", BOT_LOGIN, True),
    ("the bot prefix, a human author", ["docs/log.md"], "bot/render-log-1234567", "someone", False),
    ("the bot author, another branch", ["docs/log.md"], "fix/x", BOT_LOGIN, False),
    ("a prefix that only resembles the bot's", ["docs/log.md"], "bot/render-logs", BOT_LOGIN, False),
    ("the bot's name with a different case", ["docs/log.md"], "bot/render-log-1234567", "GitHub-Actions[bot]", False),
    ("another bot", ["docs/log.md"], "bot/render-log-1234567", "dependabot[bot]", False),
]


def self_test():
    bad = []
    for desc, files, branch, author, allowed in CASES:
        got = judge(files, branch, author) is None
        if got != allowed:
            bad.append("%s: judged %s, want %s" % (desc, "allowed" if got else "refused",
                                                   "allowed" if allowed else "refused"))
    return bad


def git(root, *a):
    r = subprocess.run(["git", "-C", root, *a], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(a), r.stderr.strip()))
    return r.stdout


def main():
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else ".")
    bad = self_test()
    for b in bad:
        print("FAIL (rule): " + b)
    if bad:
        return 1
    print("OK: the rule holds in all %d cases (the exemption needs the bot's branch prefix and its author)" % len(CASES))
    if "--self-test" in sys.argv:
        return 0
    event = os.environ.get("EVENT_NAME", "")
    base = os.environ.get("BASE", "")
    try:
        if event == "push":
            if not base or base == ZERO:
                base = git(root, "merge-base", "origin/main", "HEAD").strip()
            commits = git(root, "rev-list", "%s..HEAD" % base, "--", LOG).split()
            wrong = []
            for c in commits:
                name, email = git(root, "show", "-s", "--format=%an%n%ae", c).splitlines()[:2]
                if email != BOT_EMAIL and name != BOT_LOGIN:
                    wrong.append("%s by %s <%s>" % (c[:7], name, email))
            if wrong:
                print("FAIL: commits that change %s and are not the render-log bot's: %s" % (LOG, ", ".join(wrong)))
                return 1
            print("OK: push %s..HEAD: %d commit(s) touch %s, all the bot's" % (base[:7], len(commits), LOG))
            return 0
        if event == "pull_request":
            branch, author = os.environ.get("HEAD_REF", ""), os.environ.get("PR_AUTHOR", "")
            if not base:
                print("FAILED MEASUREMENT: a pull request with no BASE commit; nothing to compare")
                return 2
        else:
            branch, author = os.environ.get("GITHUB_REF_NAME", ""), os.environ.get("GITHUB_ACTOR", "")
            base = git(root, "merge-base", "origin/main", "HEAD").strip()
        files = git(root, "diff", "--name-only", base, "HEAD").split()
    except RuntimeError as e:
        print("FAILED MEASUREMENT: %s" % e)
        return 2
    why = judge(files, branch, author)
    if why:
        print("FAIL: " + why)
        return 1
    print("OK: %s..HEAD changes %d file(s) and leaves %s alone%s" % (
        base[:7], len(files), LOG, " (the bot's own pull request)" if LOG in files else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
