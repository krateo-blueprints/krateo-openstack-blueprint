#!/usr/bin/env python3
"""A pull request never touches docs/log.md; only the render-log bot's own pull request does.

    hack/validate-log-untouched.py [repo-root] [--base REF] [--self-test]

docs/log.md is rendered from docs/log.d on main, after each merge, by .github/workflows/render-log.yaml, which lands it
through a pull request of its own (hack/render-log-pr.sh). Any other pull request that edits or regenerates it
conflicts with every branch in flight, which is what #340's committed render still did. So the change being judged
must leave docs/log.md alone, unless it is the bot's: its branch starts with bot/render-log- AND its author is the
render-log GitHub App's bot account, `<slug>[bot]` with the slug in hack/render-log-app.yaml. Both, because each is
easy to claim alone: anyone can name a branch, and the author proves who opened it. Until 2026-11-18 the bot was
github-actions[bot] (GITHUB_TOKEN); its merges started no workflow, so they went unlinted, and that login is refused
now.

What is judged, against a base that is always EXPLICIT -- never inferred from the shape of HEAD:
  - EVENT_NAME pull_request: the files the pull request changes, measured from the merge base of HEAD and
    origin/$GITHUB_BASE_REF (the branch the pull request targets, set by GitHub on every pull_request run). That is
    exact whether HEAD is the merge ref GitHub checks out (its first parent is the base branch's tip) or the pull
    request's own head, including a head that is itself a merge of main into the branch. It used to be inferred: a
    two-parent HEAD was taken for the merge ref and measured from its first parent, so a branch head that had merged
    main was measured from the branch's previous commit and charged with main's docs/log.md render (#412, locally).
    Never BASE..HEAD either, which counts main's commits since BASE, a bot render among them, as the pull request's
    (#376). The branch HEAD_REF and author PR_AUTHOR are judged.
  - EVENT_NAME merge_group (the merge queue) or push: each commit between BASE (merge_group.base_sha, or the previous
    head) and HEAD that touches docs/log.md must be authored by the bot; each queued pull request is one squash
    commit on top of the group's base. A missing or all-zero BASE fails: there is nothing to measure from.
  - EVENT_NAME workflow_dispatch: a dispatched run has no pull request, so it is judged as one against main, the
    declared base (origin/main), as GITHUB_ACTOR on GITHUB_REF_NAME.
  - Locally (no EVENT_NAME): `--base <ref>` is required, e.g. `--base origin/main`, or LOG_BASE=<ref> for a run
    through hack/run-gates.py; the branch and author are the checked-out branch and `git config user.name`. With
    neither the gate FAILS -- it does not guess a base.
The rule itself is checked first, case by case (--self-test runs only that), so a change to it that widens the
exemption fails here before it can pass a real run. Fails when no range can be determined.
"""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile

_spec = importlib.util.spec_from_file_location("pr_range", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                        "pr_range.py"))
pr_range = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr_range)
change_base = pr_range.change_base

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


def bot_commits_only(root, base):
    """[commits between base and HEAD that touch docs/log.md and are not the bot's]."""
    wrong = []
    for c in git(root, "rev-list", "%s..HEAD" % base, "--", LOG).split():
        name, email = git(root, "show", "-s", "--format=%an%n%ae", c).splitlines()[:2]
        if name != BOT_LOGIN and not email.endswith("+%s@users.noreply.github.com" % BOT_LOGIN):
            wrong.append("%s by %s <%s>" % (c[:7], name, email))
    return wrong


def range_cases():
    """The ranges against throwaway repositories: [(description, ok)]. Each case states what the gate must see."""
    out = []

    def g(cwd, *a, author="T"):
        r = subprocess.run(["git", "-C", cwd, "-c", "user.name=%s" % author, "-c", "user.email=t@example.invalid", *a],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr)
        return r.stdout.strip()

    def write(cwd, path, text, author="T"):
        os.makedirs(os.path.dirname(os.path.join(cwd, path)), exist_ok=True)
        open(os.path.join(cwd, path), "w").write(text)
        g(cwd, "add", "-A")
        g(cwd, "commit", "-q", "-m", "change " + path, author=author)

    def merge(cwd, other, prefer_theirs):
        m = subprocess.run(["git", "-C", cwd, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "merge",
                            "--no-ff", "-q", "-m", "Merge", other], capture_output=True, text=True)
        if m.returncode != 0:  # a hand edit conflicts with the render
            g(cwd, "checkout", "--theirs" if prefer_theirs else "--ours", LOG)
            g(cwd, "add", LOG)
            g(cwd, "commit", "-q", "-m", "Merge")

    def changed(cwd):
        return LOG in g(cwd, "diff", "--name-only", change_base(cwd, "main"), "HEAD").split()

    with tempfile.TemporaryDirectory() as tmp:
        for edits_log in (False, True):
            what = "edits" if edits_log else "leaves"
            r = os.path.join(tmp, what)
            os.makedirs(r)
            g(r, "init", "-q", "-b", "main")
            write(r, LOG, "rendered\n")
            g(r, "checkout", "-q", "-b", "fix/x")
            write(r, "docs/log.d/2026-01-01-01-x.md", "entry\n")
            if edits_log:
                write(r, LOG, "edited by hand\n")
            head = g(r, "rev-parse", "HEAD")
            g(r, "checkout", "-q", "main")
            write(r, LOG, "rendered again by the bot after the fork\n", author=BOT_LOGIN)
            tip = g(r, "rev-parse", "HEAD")
            # 1. the merge ref GitHub checks out on pull_request: main's tip, then the pull request's head
            g(r, "checkout", "-q", "--detach", tip)
            merge(r, head, prefer_theirs=True)
            out.append(("the pull_request merge ref; the branch %s docs/log.md" % what, changed(r) == edits_log))
            # 2. the pull request's own head
            g(r, "checkout", "-q", "--detach", head)
            out.append(("the pull request's head; the branch %s docs/log.md" % what, changed(r) == edits_log))
            # 3. a branch head that is itself a merge of main into the branch (#412's local false FAIL): a two-parent
            #    HEAD whose first parent is the BRANCH, not main
            g(r, "checkout", "-q", "-B", "fix/x-merged", head)
            merge(r, tip, prefer_theirs=False)
            out.append(("a branch head that merged main after main's render; the branch %s docs/log.md" % what,
                        changed(r) == edits_log))
            # 4. a merge_group: one squash commit per queued pull request on top of the group's base_sha
            g(r, "checkout", "-q", "--detach", tip)
            write(r, "docs/log.d/2026-01-01-02-y.md", "another queued pull request\n")
            if edits_log:
                write(r, LOG, "a queued pull request edits it\n")
            else:
                write(r, LOG, "the bot's render in the group\n", author=BOT_LOGIN)
            out.append(("a merge_group whose queued commit %s docs/log.md" %
                        ("(by a human) edits" if edits_log else "is the bot's render of"),
                        bool(bot_commits_only(r, tip)) == edits_log))
    return out


def base_cases():
    """How the base is chosen with no EVENT_NAME: [(description, ok)]."""
    out = []
    saved = os.environ.pop("LOG_BASE", None)
    try:
        try:
            pr_range.base_ref("", [])
            out.append(("by hand with neither --base nor LOG_BASE: refused", False))
        except pr_range.NoBase:
            out.append(("by hand with neither --base nor LOG_BASE: refused", True))
        os.environ["LOG_BASE"] = "origin/gate-env-base"
        out.append(("LOG_BASE alone is the base", pr_range.base_ref("", []) == ("origin/gate-env-base", "LOG_BASE")))
        out.append(("--base wins over LOG_BASE",
                    pr_range.base_ref("", ["--base", "origin/gate-arg-base"]) == ("origin/gate-arg-base", "--base")))
        out.append(("LOG_BASE never applies to a pull_request run",
                    _pr_ref_ignores_log_base()))
    finally:
        os.environ.pop("LOG_BASE", None)
        if saved is not None:
            os.environ["LOG_BASE"] = saved
    return out


def _pr_ref_ignores_log_base():
    saved = os.environ.get("GITHUB_BASE_REF")
    os.environ["GITHUB_BASE_REF"] = "main"
    try:
        return pr_range.base_ref("pull_request", []) == ("origin/main", "GITHUB_BASE_REF")
    finally:
        if saved is None:
            os.environ.pop("GITHUB_BASE_REF", None)
        else:
            os.environ["GITHUB_BASE_REF"] = saved


def main():
    args = pr_range.positional(sys.argv[1:])
    root = os.path.abspath(args[0] if args else ".")
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
    print("OK: the ranges hold in all %d cases (merge ref, the pull request's head, a branch head that merged main, "
          "a merge group; each with docs/log.md left and edited)" % len(ranges))
    bases = base_cases()
    wrong = [d for d, ok in bases if not ok]
    for d in wrong:
        print("FAIL (base): " + d)
    if wrong:
        return 1
    print("OK: the base is explicit in all %d cases (neither given: refused; LOG_BASE; --base over LOG_BASE; "
          "a pull request ignores LOG_BASE)" % len(bases))
    if "--self-test" in sys.argv:
        return 0
    event = os.environ.get("EVENT_NAME", "")
    try:
        if event in ("push", "merge_group"):
            base = os.environ.get("BASE", "")
            if not base or base == ZERO:
                print("FAILED MEASUREMENT: %s with no BASE (merge_group.base_sha or the previous head); nothing to "
                      "measure from, and the gate does not guess one" % event)
                return 2
            wrong = bot_commits_only(root, base)
            if wrong:
                print("FAIL: commits that change %s and are not the render-log bot's: %s" % (LOG, ", ".join(wrong)))
                return 1
            print("OK: %s %s..HEAD: no commit but the bot's touches %s" % (event, base[:7], LOG))
            return 0
        try:
            base_ref, how = pr_range.base_ref(event, sys.argv[1:])
        except pr_range.NoBase as e:
            print("FAILED MEASUREMENT: %s" % e)
            return 2
        if event == "pull_request":
            branch, author = os.environ.get("HEAD_REF", ""), os.environ.get("PR_AUTHOR", "")
        elif event == "workflow_dispatch":
            branch, author = os.environ.get("GITHUB_REF_NAME", ""), os.environ.get("GITHUB_ACTOR", "")
        else:
            branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
            author = subprocess.run(["git", "-C", root, "config", "user.name"], capture_output=True,
                                    text=True).stdout.strip()
        base = change_base(root, base_ref)
        files = git(root, "diff", "--name-only", base, "HEAD").split()
    except RuntimeError as e:
        print("FAILED MEASUREMENT: %s" % e)
        return 2
    why = judge(files, branch, author)
    if why:
        print("FAIL: " + why)
        return 1
    print("OK: merge-base(%s, HEAD) %s..HEAD (%s) changes %d file(s) and leaves %s alone%s" % (
        base_ref, base[:7], how, len(files), LOG, " (the bot's own pull request)" if LOG in files else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
