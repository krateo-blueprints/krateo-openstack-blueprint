#!/usr/bin/env python3
"""A pull request never touches docs/log.md; only the render-log bot's own pull request does.

    hack/validate-log-untouched.py [repo-root]

docs/log.md is rendered from docs/log.d on main, after each merge, by .github/workflows/render-log.yaml, which lands it
through a pull request of its own (hack/render-log-pr.sh). Any other pull request that edits or regenerates it
conflicts with every branch in flight, which is what #340's committed render still did. So the change being judged
must leave docs/log.md alone, unless it is the bot's: its branch starts with bot/render-log- AND its author is the
render-log GitHub App's bot account, `<slug>[bot]` with the slug in hack/render-log-app.yaml. Both, because each is
easy to claim alone: anyone can name a branch, and the author proves who opened it. Until 2026-11-18 the bot was
github-actions[bot] (GITHUB_TOKEN); its merges started no workflow, so they went unlinted, and that login is refused
now.

What is judged, from the environment lint.yaml's job-hooks runner step sets:
  - EVENT_NAME pull_request: the files the pull request itself changes, the branch HEAD_REF, the author PR_AUTHOR.
    Measured from the merge ref's first parent (main's tip as GitHub merged it), or from the merge base of BASE and
    HEAD when HEAD is not a merge commit -- never BASE..HEAD, which counts main's commits since BASE, a bot render of
    docs/log.md among them, as the pull request's (#376). A merge group needs no such care: its base_sha is fixed and
    the group's commits sit on top of it;
  - EVENT_NAME push or merge_group (the merge queue; BASE is the group's base, and each queued pull request is
    one squash commit): each commit between BASE (the previous head) and HEAD that touches docs/log.md must be authored
    by the bot (its squash merge); a range with no previous head (a new branch) is compared with origin/main;
  - anything else (workflow_dispatch on a branch): the files changed since the branch left origin/main, judged as a
    pull request by GITHUB_ACTOR from GITHUB_REF_NAME.
The rule itself is checked first, case by case (--self-test runs only that), so a change to it that widens the
exemption fails here before it can pass a real run. Fails when no range can be determined.
"""
import os
import re
import subprocess
import sys
import tempfile

LOG = "docs/log.md"
BOT_PREFIX = "bot/render-log-"
ZERO = "0" * 40


def app_slug():
    here = os.path.dirname(os.path.abspath(__file__))
    for line in open(os.path.join(here, "render-log-app.yaml")):
        m = re.match(r"^slug:\s*([a-z0-9-]+)\s*$", line)
        if m:
            return m.group(1)
    raise SystemExit("FAILED MEASUREMENT: hack/render-log-app.yaml names no slug, so the bot's login is unknown")


BOT_LOGIN = app_slug() + "[bot]"


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
    ("the retired GITHUB_TOKEN bot", ["docs/log.md"], "bot/render-log-1234567", "github-actions[bot]", False),
    ("the App's name without [bot]", ["docs/log.md"], "bot/render-log-1234567", BOT_LOGIN[:-5], False),
    ("the bot prefix, a human author", ["docs/log.md"], "bot/render-log-1234567", "someone", False),
    ("the bot author, another branch", ["docs/log.md"], "fix/x", BOT_LOGIN, False),
    ("a prefix that only resembles the bot's", ["docs/log.md"], "bot/render-logs", BOT_LOGIN, False),
    ("the bot's name with a different case", ["docs/log.md"], "bot/render-log-1234567", BOT_LOGIN.upper(), False),
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


def pr_change_base(root, base):
    """The commit a pull request's own change is measured from.

    Not BASE itself: pull_request.base.sha can be older than main's tip, and a two-dot diff BASE..HEAD then counts
    every commit main gained since -- a bot render of docs/log.md among them -- as the pull request's change (#376 at
    8d5240e failed so, touching no docs/log.md). On a pull_request event actions/checkout checks out the merge ref,
    refs/pull/<n>/merge, a merge commit whose first parent is main's tip as GitHub merged it and whose second is the
    pull request's head: first parent..HEAD is exactly what the pull request changes on main. Checked out at the
    pull request's head instead (no merge commit), the merge base of BASE and HEAD, as BASE...HEAD would use.
    """
    parents = git(root, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    if len(parents) == 2:
        return parents[0], "the merge ref's first parent"
    return git(root, "merge-base", base, "HEAD").strip(), "the merge base"


def range_cases():
    """The pull-request range against throwaway repositories: [(description, ok)]."""
    out = []

    def g(cwd, *a):
        r = subprocess.run(["git", "-C", cwd, "-c", "user.name=T", "-c", "user.email=t@example.invalid", *a],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr)
        return r.stdout.strip()

    def write(cwd, path, text):
        os.makedirs(os.path.dirname(os.path.join(cwd, path)), exist_ok=True)
        open(os.path.join(cwd, path), "w").write(text)
        g(cwd, "add", "-A")
        g(cwd, "commit", "-q", "-m", "change " + path)

    def changed(cwd, base):
        frm, _ = pr_change_base(cwd, base)
        return g(cwd, "diff", "--name-only", frm, "HEAD").split()

    with tempfile.TemporaryDirectory() as tmp:
        for edits_log in (False, True):
            r = os.path.join(tmp, "edits" if edits_log else "entry")
            os.makedirs(r)
            g(r, "init", "-q", "-b", "main")
            write(r, LOG, "rendered\n")
            fork = g(r, "rev-parse", "HEAD")
            g(r, "checkout", "-q", "-b", "fix/x")
            write(r, "docs/log.d/2026-01-01-01-x.md", "entry\n")
            if edits_log:
                write(r, LOG, "edited by hand\n")
            head = g(r, "rev-parse", "HEAD")
            g(r, "checkout", "-q", "main")
            write(r, LOG, "rendered again by the bot after the fork\n" if not edits_log else "rendered\nagain\n")
            tip = g(r, "rev-parse", "HEAD")
            # the merge ref GitHub checks out on pull_request: main's tip, then the pull request's head
            g(r, "checkout", "-q", "--detach", tip)
            m = subprocess.run(["git", "-C", r, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "merge",
                                "--no-ff", "-q", "-m", "Merge", head], capture_output=True, text=True)
            if m.returncode != 0:  # the hand edit conflicts with the render: resolve as the branch's
                g(r, "checkout", "--theirs", LOG)
                g(r, "add", LOG)
                g(r, "commit", "-q", "-m", "Merge")
            got = LOG in changed(r, fork)
            out.append(("merge ref, base older than main's tip, the branch %s docs/log.md"
                        % ("edits" if edits_log else "adds a log.d entry and leaves"), got == edits_log))
            # checked out at the pull request's head instead, BASE main's newer tip
            g(r, "checkout", "-q", "--detach", head)
            got = LOG in changed(r, tip)
            out.append(("the pull request's head, base newer than the fork, the branch %s docs/log.md"
                        % ("edits" if edits_log else "leaves"), got == edits_log))
    return out


def main():
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else ".")
    bad = self_test()
    for b in bad:
        print("FAIL (rule): " + b)
    if bad:
        return 1
    print("OK: the rule holds in all %d cases (the exemption needs the bot's branch prefix and its author)" % len(CASES))
    try:
        ranges = range_cases()
    except RuntimeError as e:
        print("FAILED MEASUREMENT: the range cases could not be set up: %s" % e)
        return 2
    wrong = [d for d, ok in ranges if not ok]
    for d in wrong:
        print("FAIL (range): " + d)
    if wrong:
        return 1
    print("OK: the pull-request range holds in all %d cases (a main-side render after the fork is not the branch's)"
          % len(ranges))
    if "--self-test" in sys.argv:
        return 0
    event = os.environ.get("EVENT_NAME", "")
    base = os.environ.get("BASE", "")
    try:
        if event in ("push", "merge_group"):
            if not base or base == ZERO:
                base = git(root, "merge-base", "origin/main", "HEAD").strip()
            commits = git(root, "rev-list", "%s..HEAD" % base, "--", LOG).split()
            wrong = []
            for c in commits:
                name, email = git(root, "show", "-s", "--format=%an%n%ae", c).splitlines()[:2]
                if name != BOT_LOGIN and not email.endswith("+%s@users.noreply.github.com" % BOT_LOGIN):
                    wrong.append("%s by %s <%s>" % (c[:7], name, email))
            if wrong:
                print("FAIL: commits that change %s and are not the render-log bot's: %s" % (LOG, ", ".join(wrong)))
                return 1
            print("OK: %s %s..HEAD: %d commit(s) touch %s, all the bot's" % (event, base[:7], len(commits), LOG))
            return 0
        if event == "pull_request":
            branch, author = os.environ.get("HEAD_REF", ""), os.environ.get("PR_AUTHOR", "")
            if not base:
                print("FAILED MEASUREMENT: a pull request with no BASE commit; nothing to compare")
                return 2
        else:
            branch, author = os.environ.get("GITHUB_REF_NAME", ""), os.environ.get("GITHUB_ACTOR", "")
            base = git(root, "merge-base", "origin/main", "HEAD").strip()
        how = "since the branch left main"
        if event == "pull_request":
            base, how = pr_change_base(root, base)
        files = git(root, "diff", "--name-only", base, "HEAD").split()
    except RuntimeError as e:
        print("FAILED MEASUREMENT: %s" % e)
        return 2
    why = judge(files, branch, author)
    if why:
        print("FAIL: " + why)
        return 1
    print("OK: %s..HEAD (%s) changes %d file(s) and leaves %s alone%s" % (
        base[:7], how, len(files), LOG, " (the bot's own pull request)" if LOG in files else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
