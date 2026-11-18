#!/usr/bin/env python3
"""hack/render-log-pr.sh lands docs/log.md through one bot pull request, and does nothing when it is current.

    hack/validate-render-log-pr.py [repo-root]

The script runs on main after every merge (.github/workflows/render-log.yaml). It is run here against a local bare
remote and a stand-in `gh` that records every call, in a throwaway clone holding this repo's docs/log.d, renderer and
script:

  0. the workflow: render-log.yaml's push paths leave docs/log.md out (the bot's commit changes it alone, case 2, so
     the bot's merge cannot start render-log again), it mints the token with actions/create-github-app-token pinned
     to a commit, it refuses without the App's secrets, and GITHUB_TOKEN gets no write permission;
  1. docs/log.md already current: exit 0, no commit, no branch pushed, no gh pull-request call;
  2. a new docs/log.d entry on main: one branch bot/render-log-<sha7>, one commit by the App's bot account
     (<slug>[bot], <id>+<slug>[bot]@users.noreply.github.com) that changes docs/log.md alone to exactly the render
     and carries no skip marker; then gh pr create and a squash merge of that number;
  3. a bot pull request already open: that branch gets the fresh render as a fast-forward, no second pull request is
     opened, and that number is merged;
  4. once the render has landed on main: nothing again (idempotent);
  5. a token from an App with another slug: refused before anything is committed;
  3b. the update path from a checkout at render-log.yaml's own fetch depth: a depth-1 clone first reproduces git's
     "refusing to merge unrelated histories" (run 44281145388) and the script stops; at the workflow's depth the merge
     of main into the bot branch succeeds. A workflow left at depth 1 fails here;
  6. a merge queue on main (a merge_queue rule), and the required checks are not reported yet when the bot looks (run
     44280066151 gave up one second after opening #384): it polls the check runs of exactly the commit it pushed,
     ignores a failing check the ruleset does not require, and when every required check (the ruleset's own list) has
     passed adds the pull request to the queue with the GraphQL enqueuePullRequest mutation and its node id. It never
     runs gh pr merge (auto-merge is disabled here, and gh pr merge, with or without --auto, calls
     enablePullRequestAutoMerge);
  7. a merge queue, and a required check fails on the bot's pull request: refused at once, nothing enqueued or merged;
  8. the required checks never reported within the appear limit: fails saying so, nothing enqueued;
  9. a required check reported but never finished within the finish limit: fails saying so, nothing enqueued;
  10. a ruleset that requires no check: refused rather than enqueued unchecked.

Fails when git is missing or a case cannot be set up.
"""
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so the workflow was not read")
    sys.exit(2)

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
SLUG = re.search(r"^slug:\s*([a-z0-9-]+)\s*$", open(os.path.join(ROOT, "hack", "render-log-app.yaml")).read(), re.M).group(1)
BOT = SLUG + "[bot]"
BOT_ID = "123456789"
BOT_EMAIL = "%s+%s@users.noreply.github.com" % (BOT_ID, BOT)
SKIP = re.compile(r"\[\s*(?:skip\s+ci|ci\s+skip|no\s+ci|skip\s+actions|actions\s+skip)\s*\]|^\s*skip-checks\s*:\s*true", re.I | re.M)
STUB = r'''#!/bin/sh
echo "$*" >> "$GH_LOG"
case "$1 $2" in
  "api users/"*) echo "$GH_BOT_ID" ;;
  "api repos/"*"/check-runs"*)
    n=$(( $(cat "$GH_RUNS_DIR/count" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$GH_RUNS_DIR/count"
    f="$GH_RUNS_DIR/runs.$n"
    [ -f "$f" ] || f=$(ls "$GH_RUNS_DIR"/runs.* 2>/dev/null | sort -t. -k2 -n | tail -n1)
    [ -n "$f" ] && cat "$f"
    exit 0 ;;
  "api repos/"*"/rules/branches/"*)
    case "$*" in
      *required_status_checks*) [ -n "${GH_REQUIRED:-}" ] && printf '%b\n' "$GH_REQUIRED"; exit 0 ;;
      *) echo "${GH_QUEUE:-0}" ;;
    esac ;;
  "api graphql") echo '{"data":{"enqueuePullRequest":{"mergeQueueEntry":{"position":1}}}}' ;;
  "pr view") echo "PR_kwDOGATE42" ;;
  "pr list") printf '%s\n' "$GH_OPEN" ;;
  "pr create") echo "https://github.com/o/r/pull/42" ;;
  "pr merge") exit 0 ;;
  *) echo "stub gh: unexpected $*" >&2; exit 3 ;;
esac
'''
failures = []


def run(cmd, cwd, env=None, check=True):
    # LC_ALL=C: git's messages are matched below, and a localized git prints them translated.
    env = dict(env if env is not None else os.environ, LC_ALL="C", LANG="C")
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError("%s: %s%s" % (" ".join(cmd), r.stdout, r.stderr))
    return r


def git(cwd, *a):
    return run(["git", *a], cwd).stdout.strip()


def setup(tmp):
    remote = os.path.join(tmp, "remote.git")
    work = os.path.join(tmp, "work")
    run(["git", "init", "-q", "--bare", "-b", "main", remote], tmp)
    os.makedirs(os.path.join(work, "hack"))
    shutil.copytree(os.path.join(ROOT, "docs", "log.d"), os.path.join(work, "docs", "log.d"))
    for f in ("hack/render-log.py", "hack/render-log-pr.sh", "hack/render-log-app.yaml"):
        shutil.copy(os.path.join(ROOT, f), os.path.join(work, f))
    for f in os.listdir(os.path.join(ROOT, "docs")):  # link targets of the entries
        p = os.path.join(ROOT, "docs", f)
        if os.path.isfile(p) and f != "log.md":
            shutil.copy(p, os.path.join(work, "docs", f))
        elif os.path.isdir(p) and f != "log.d":
            # A subdirectory holds link targets too: #312's entry links ../runbooks/openstack-api-down.md. Without it
            # the renderer here refused a link the real tree resolves, and the setup failed for the fixture, not the bot.
            shutil.copytree(p, os.path.join(work, "docs", f))
    run(["git", "init", "-q", "-b", "main"], work)
    git(work, "config", "user.name", "Maintainer")
    git(work, "config", "user.email", "maintainer@example.invalid")
    run([sys.executable, "hack/render-log.py", "."], work)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "initial")
    git(work, "remote", "add", "origin", remote)
    git(work, "push", "-q", "origin", "main")
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir)
    open(os.path.join(bindir, "gh"), "w").write(STUB)
    os.chmod(os.path.join(bindir, "gh"), 0o755)
    return remote, work, bindir


REQUIRED = "lint-docs / lint-docs\nvalues-schema"
PASSING = "lint-docs / lint-docs\tcompleted\tsuccess\nvalues-schema\tcompleted\tsuccess"
RAW = []  # every gh call of the last script() run, lookups included


def script(work, bindir, tmp, open_pr="", slug=None, queue="0", runs=None, required=REQUIRED):
    """runs: what successive check-runs reads return, one TSV text per poll (name, status, conclusion); the last one
    repeats. The script polls with no sleep and limits of 3 polls to appear and 5 to finish."""
    log = os.path.join(tmp, "gh.log")
    open(log, "w").close()
    rdir = tempfile.mkdtemp(dir=tmp)
    for i, text in enumerate(runs if runs is not None else [PASSING], 1):
        open(os.path.join(rdir, "runs.%d" % i), "w").write(text + ("\n" if text else ""))
    env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"], GH_LOG=log, GH_OPEN=open_pr,
               GH_BOT_ID=BOT_ID, APP_SLUG=SLUG if slug is None else slug, GH_QUEUE=queue, GH_RUNS_DIR=rdir,
               GH_REQUIRED=required, CHECKS_POLL_SECONDS="0", CHECKS_APPEAR_POLLS="3", CHECKS_DONE_POLLS="5")
    env.pop("BOT_USER_ID", None)
    r = run(["bash", "hack/render-log-pr.sh", "."], work, env=env, check=False)
    RAW[:] = [l for l in open(log).read().splitlines() if l]
    # the bot account's id and the branch's rules are lookups, not pull-request calls; the enqueue mutation is one
    return r, [l for l in open(log).read().splitlines()
               if l and (not l.startswith("api ") or l.startswith("api graphql"))]


def workflow_check():
    """Case 0, read from .github/workflows/render-log.yaml."""
    wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "render-log.yaml")))
    on = wf.get("on") or wf.get(True) or {}
    paths = ((on.get("push") or {}).get("paths")) or []
    if not paths:
        failures.append("0: render-log.yaml's push trigger has no paths filter, so the bot's merge would start it again")
    for pat in paths:
        rx = re.escape(pat).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        if re.fullmatch(rx, "docs/log.md") or fnmatch.fnmatch("docs/log.md", pat):
            failures.append("0: render-log.yaml's push path %r matches docs/log.md, so the bot's merge starts it again"
                            % pat)
    perms = [wf.get("permissions") or {}] + [j.get("permissions") or {} for j in (wf.get("jobs") or {}).values()]
    if any(v == "write" for p in perms if isinstance(p, dict) for v in p.values()) or "write-all" in perms:
        failures.append("0: render-log.yaml gives GITHUB_TOKEN a write permission; the App's token does the writing")
    steps = [s for j in (wf.get("jobs") or {}).values() for s in j.get("steps") or []]
    app = [s for s in steps if str(s.get("uses", "")).startswith("actions/create-github-app-token@")]
    if len(app) != 1 or not re.fullmatch(r"actions/create-github-app-token@[0-9a-f]{40}", app[0]["uses"]):
        failures.append("0: render-log.yaml must mint its token with one actions/create-github-app-token step pinned to "
                        "a 40-hex commit, found %s" % [s.get("uses") for s in app])
    refuse = [s for s in steps if "RENDER_LOG_APP_PRIVATE_KEY" in str(s.get("env", "")) and "exit 1" in str(s.get("run"))]
    if not refuse or steps.index(refuse[0]) > (steps.index(app[0]) if app else 0):
        failures.append("0: render-log.yaml must refuse, before minting, when the App's secrets are absent")
    run_steps = [s for s in steps if "render-log-pr.sh" in str(s.get("run", ""))]
    env = (run_steps[0].get("env") or {}) if run_steps else {}
    if "steps.app.outputs.token" not in str(env.get("GH_TOKEN")) or "app-slug" not in str(env.get("APP_SLUG")):
        failures.append("0: render-log-pr.sh must run with the App's token (GH_TOKEN) and slug (APP_SLUG), got %s" % env)


def checkout_depth():
    """The fetch depth of render-log.yaml's actions/checkout step: 0 (None here) is the full history, and an absent
    value is actions/checkout's default, 1."""
    wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "render-log.yaml")))
    steps = [s for j in (wf.get("jobs") or {}).values() for s in j.get("steps") or []
             if str(s.get("uses", "")).startswith("actions/checkout@")]
    if len(steps) != 1:
        failures.append("0: render-log.yaml should have one actions/checkout step, found %d" % len(steps))
        return 1
    depth = int((steps[0].get("with") or {}).get("fetch-depth", 1))
    return None if depth == 0 else depth


def checkout_like_actions(url, dest, depth):
    """What actions/checkout does for `ref: main`: a new repository whose origin keeps git's default fetch refspec, and
    one fetch of main alone, at `depth` (None: the full history). `git clone --branch` would differ: with --depth it
    implies --single-branch, so the script's later `git fetch origin <branch>` would not create origin/<branch> at all,
    a different failure from the one CI had."""
    os.makedirs(dest)
    run(["git", "init", "-q"], dest)
    run(["git", "remote", "add", "origin", url], dest)
    run(["git", "fetch", "-q"] + (["--depth=%d" % depth] if depth else []) +
        ["origin", "+refs/heads/main:refs/remotes/origin/main"], dest)
    run(["git", "checkout", "-q", "-B", "main", "refs/remotes/origin/main"], dest)


def remote_branches(remote):
    return sorted(l.split("refs/heads/")[1] for l in git(remote, "for-each-ref", "--format=%(refname)").splitlines())


def add_entry(work, name):
    p = os.path.join(work, "docs", "log.d", name)
    open(p, "w").write("---\ntype: Log\ntitle: Gate entry\ndescription: x\n---\n- **Gate entry (#0).** A test.\n")
    git(work, "checkout", "-q", "main")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "add an entry")
    git(work, "push", "-q", "origin", "main")


def main():
    if shutil.which("git") is None:
        print("FAILED MEASUREMENT: git not on PATH")
        return 2
    checked = 0
    with tempfile.TemporaryDirectory() as tmp:
        try:
            remote, work, bindir = setup(tmp)
        except RuntimeError as e:
            print("FAILED MEASUREMENT: cannot set up the throwaway repository: %s" % e)
            return 2
        workflow_check()
        checked += 1
        # 1. current
        head = git(remote, "rev-parse", "main")
        r, calls = script(work, bindir, tmp)
        if r.returncode != 0 or calls or remote_branches(remote) != ["main"] or git(remote, "rev-parse", "main") != head:
            failures.append("1 (docs/log.md current): rc %d, gh calls %s, remote branches %s; want rc 0, no call, "
                            "main alone and unmoved" % (r.returncode, calls, remote_branches(remote)))
        checked += 1
        # 2. a new entry
        add_entry(work, "2099-01-01-01-gate-entry.md")
        sha7 = git(work, "rev-parse", "--short=7", "HEAD")
        r, calls = script(work, bindir, tmp)
        branch = "bot/render-log-" + sha7
        if r.returncode != 0:
            failures.append("2 (new entry): rc %d: %s" % (r.returncode, (r.stdout + r.stderr)[-400:]))
        elif remote_branches(remote) != sorted(["main", branch]):
            failures.append("2 (new entry): remote branches %s, want main and %s" % (remote_branches(remote), branch))
        else:
            log = [l.strip() for l in git(remote, "log", "--format=%ae|%an|%B%x00", "main.." + branch).split("\x00")
                   if l.strip()]
            files = git(remote, "diff", "--name-only", "main", branch).split()
            run([sys.executable, "hack/render-log.py", "."], work)
            want = open(os.path.join(work, "docs", "log.md")).read()
            git(work, "checkout", "-q", "--", "docs/log.md")
            got = run(["git", "show", branch + ":docs/log.md"], remote).stdout
            if len(log) != 1 or not log[0].startswith("%s|%s|" % (BOT_EMAIL, BOT)) or SKIP.search(log[0]):
                failures.append("2 (new entry): bot branch commits %s; want one by %s, with no skip marker"
                                % (log, BOT_EMAIL))
            if files != ["docs/log.md"]:
                failures.append("2 (new entry): the bot branch changes %s; want docs/log.md alone" % files)
            if got != want:
                failures.append("2 (new entry): the bot branch's docs/log.md is not the render")
            kinds = [" ".join(c.split()[:2]) for c in calls]
            if kinds != ["pr list", "pr create", "pr merge"] or "--squash" not in calls[-1] or " 42 " not in " %s " % calls[-1]:
                failures.append("2 (new entry): gh calls %s; want pr list, pr create, then pr merge 42 --squash" % calls)
        checked += 1
        # 3. a bot pull request already open: add another entry; the open branch is updated, not a second one opened
        before = git(remote, "rev-parse", branch)
        add_entry(work, "2099-01-02-01-gate-entry.md")
        r, calls = script(work, bindir, tmp, open_pr="7 " + branch)
        kinds = [" ".join(c.split()[:2]) for c in calls]
        if r.returncode != 0:
            failures.append("3 (open bot pull request): rc %d: %s" % (r.returncode, (r.stdout + r.stderr)[-400:]))
        else:
            ff = run(["git", "merge-base", "--is-ancestor", before, branch], remote, check=False).returncode == 0
            got = run(["git", "show", branch + ":docs/log.md"], remote).stdout
            if not ff:
                failures.append("3 (open bot pull request): %s was rewritten, not fast-forwarded" % branch)
            if got.count("Gate entry (#0)") != 2:
                failures.append("3 (open bot pull request): its docs/log.md lacks the new entry")
            if remote_branches(remote) != sorted(["main", branch]):
                failures.append("3 (open bot pull request): remote branches %s; want no second bot branch"
                                % remote_branches(remote))
            if kinds != ["pr list", "pr merge"] or " 7 " not in " %s " % calls[-1]:
                failures.append("3 (open bot pull request): gh calls %s; want pr list then pr merge 7, no pr create"
                                % calls)
        checked += 1
        # 3b. the same update path from a checkout of the depth render-log.yaml uses. main moves on after the bot
        #     branch forked; from a depth-1 clone main's one commit has no parents, so git refuses to merge it into the
        #     bot branch (run 44281145388). First prove that refusal is real with a depth-1 clone, then run the path
        #     at the workflow's own depth, which must merge. A workflow left at depth 1 fails the second half.
        add_entry(work, "2099-01-02-02-gate-entry.md")
        before = git(remote, "rev-parse", branch)
        url = "file://" + remote
        shallow = os.path.join(tmp, "shallow")
        checkout_like_actions(url, shallow, 1)
        r, calls = script(shallow, bindir, tmp, open_pr="7 " + branch)
        out = r.stdout + r.stderr
        if r.returncode == 0 or "unrelated histories" not in out or git(remote, "rev-parse", branch) != before:
            failures.append("3b (update path, depth-1 clone): rc %d, branch moved %s, output %r; want git's refusal "
                            "to merge unrelated histories and the script stopping with the branch untouched (this half "
                            "proves the defect is reproduced)" % (r.returncode, git(remote, "rev-parse", branch) != before,
                                                                  out[-300:]))
        depth = checkout_depth()
        deep = os.path.join(tmp, "deep")
        checkout_like_actions(url, deep, depth)
        r, calls = script(deep, bindir, tmp, open_pr="7 " + branch)
        got = run(["git", "show", branch + ":docs/log.md"], remote, check=False).stdout
        ff = run(["git", "merge-base", "--is-ancestor", before, branch], remote, check=False).returncode == 0
        if r.returncode != 0 or not ff or got.count("Gate entry (#0)") != 3:
            failures.append("3b (update path, render-log.yaml's checkout depth %s): rc %d, fast-forward %s, entries %d, "
                            "output %r; want the merge of main to succeed and the branch to carry the new entry"
                            % (depth or "full", r.returncode, ff, got.count("Gate entry (#0)"),
                               (r.stdout + r.stderr)[-300:]))
        checked += 1
        # 4. the render landed on main: nothing to do
        git(work, "checkout", "-q", "main")
        run([sys.executable, "hack/render-log.py", "."], work)
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "the bot's squash merge")
        git(work, "push", "-q", "origin", "main")
        head = git(remote, "rev-parse", "main")
        branches = remote_branches(remote)
        r, calls = script(work, bindir, tmp)
        if r.returncode != 0 or calls or remote_branches(remote) != branches or git(remote, "rev-parse", "main") != head:
            failures.append("4 (after landing): rc %d, gh calls %s; want rc 0 and nothing done" % (r.returncode, calls))
        checked += 1
        # 6. a merge queue on main, and the required checks are not reported yet when the bot looks (run 44280066151
        #    gave up one second after opening #384): it keeps polling the check runs of exactly the commit it pushed,
        #    ignores a failing check the ruleset does not require, and once every required one has passed enqueues
        #    with the GraphQL enqueuePullRequest mutation and the node id; it never runs gh pr merge (auto-merge is
        #    off here, and gh pr merge, with or without --auto, calls enablePullRequestAutoMerge)
        add_entry(work, "2099-01-04-01-gate-entry.md")
        sha7 = git(work, "rev-parse", "--short=7", "HEAD")
        r, calls = script(work, bindir, tmp, queue="1", runs=[
            "",
            "lint-docs / lint-docs\tin_progress\t\nlint\tcompleted\tfailure",
            PASSING + "\nlint\tcompleted\tfailure",
        ])
        pushed = git(remote, "rev-parse", "bot/render-log-" + sha7) if r.returncode == 0 else "?"
        polled = [c for c in RAW if "/check-runs" in c]
        merges = [c for c in calls if c.startswith("pr merge")]
        enq = [c for c in calls if c.startswith("api graphql")]
        if (r.returncode != 0 or merges or len(enq) != 1 or "enqueuePullRequest" not in enq[0]
                or "id=PR_kwDOGATE42" not in enq[0] or len(polled) != 3
                or any("/commits/%s/check-runs" % pushed not in c for c in polled)
                or [c for c in calls if c.startswith("pr checks")]):
            failures.append("6 (merge queue, checks reported late): rc %d, %d check-run polls %s, calls %s, stderr %r; "
                            "want 3 polls of the pushed commit %s, then one enqueuePullRequest with the node id, no gh "
                            "pr merge" % (r.returncode, len(polled), polled[:1], calls, r.stderr[-300:], pushed))
        checked += 1
        # 7. a merge queue, and a required check fails on the bot's pull request: refused at once, nothing enqueued
        add_entry(work, "2099-01-05-01-gate-entry.md")
        r, calls = script(work, bindir, tmp, queue="1", runs=[
            "values-schema\tcompleted\tfailure\nlint-docs / lint-docs\tin_progress\t"])
        polled = [c for c in RAW if "/check-runs" in c]
        if (r.returncode == 0 or [c for c in calls if c.startswith("api graphql") or c.startswith("pr merge")]
                or "failed a required check" not in r.stderr or "values-schema (failure)" not in r.stderr
                or len(polled) != 1):
            failures.append("7 (merge queue, failed check): rc %d, %d polls, calls %s, stderr %r; want an immediate "
                            "failure naming values-schema and no enqueue or merge"
                            % (r.returncode, len(polled), calls, r.stderr[-300:]))
        checked += 1
        # 8. a merge queue, and the required checks never appear: fails after the appear limit, saying so
        add_entry(work, "2099-01-06-01-gate-entry.md")
        r, calls = script(work, bindir, tmp, queue="1", runs=["lint\tcompleted\tsuccess"])
        polled = [c for c in RAW if "/check-runs" in c]
        if (r.returncode == 0 or [c for c in calls if c.startswith("api graphql") or c.startswith("pr merge")]
                or "never reported" not in r.stderr or "values-schema" not in r.stderr or len(polled) != 3):
            failures.append("8 (merge queue, checks never reported): rc %d, %d polls, stderr %r; want a failure after "
                            "3 polls saying the required checks never reported, and no enqueue"
                            % (r.returncode, len(polled), r.stderr[-300:]))
        checked += 1
        # 9. a merge queue, and a required check is reported but never finishes: fails after the finish limit
        add_entry(work, "2099-01-07-01-gate-entry.md")
        r, calls = script(work, bindir, tmp, queue="1", runs=[
            "lint-docs / lint-docs\tcompleted\tsuccess\nvalues-schema\tqueued\t"])
        polled = [c for c in RAW if "/check-runs" in c]
        if (r.returncode == 0 or [c for c in calls if c.startswith("api graphql") or c.startswith("pr merge")]
                or "still not finished" not in r.stderr or len(polled) != 5):
            failures.append("9 (merge queue, check never finishes): rc %d, %d polls, stderr %r; want a failure after 5 "
                            "polls and no enqueue" % (r.returncode, len(polled), r.stderr[-300:]))
        checked += 1
        # 10. a merge queue whose ruleset requires no check: refused, never enqueued unchecked
        add_entry(work, "2099-01-08-01-gate-entry.md")
        r, calls = script(work, bindir, tmp, queue="1", required="")
        if (r.returncode == 0 or [c for c in calls if c.startswith("api graphql") or c.startswith("pr merge")]
                or "requires no status check" not in r.stderr):
            failures.append("10 (merge queue, nothing required): rc %d, stderr %r; want a refusal and no enqueue"
                            % (r.returncode, r.stderr[-300:]))
        checked += 1
        # 5. a token from another App: refused before anything is committed or called
        add_entry(work, "2099-01-03-01-gate-entry.md")
        head = git(remote, "rev-parse", "main")
        branches = remote_branches(remote)
        r, calls = script(work, bindir, tmp, slug="some-other-app")
        if r.returncode == 0 or calls or remote_branches(remote) != branches or "refusing" not in r.stderr:
            failures.append("5 (another App's token): rc %d, gh calls %s, stderr %r; want a refusal and nothing done"
                            % (r.returncode, calls, r.stderr[-200:]))
        checked += 1
    if failures:
        for f in failures:
            print("FAIL: " + f)
        return 1
    print("OK: render-log-pr.sh, %d cases: nothing when current; one bot branch, commit and squash-merged pull request "
          "for a new entry, by the App's bot with no skip marker; an open bot pull request updated, never a second; "
          "idempotent once landed; another App refused; the workflow cannot re-trigger itself; the update path merges "
          "from the workflow's checkout depth (a depth-1 clone reproduces the refusal); under a merge queue it waits "
          "for the ruleset's required checks on the pushed commit (late, failed, never reported, never finished, none "
          "required) and enqueues through enqueuePullRequest instead of merging" % checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
