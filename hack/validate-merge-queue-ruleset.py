#!/usr/bin/env python3
"""The merge-queue ruleset draft requires exactly lint.yaml's gating checks, and lint.yaml runs on merge groups.

    hack/validate-merge-queue-ruleset.py [repo-root]

hack/merge-queue-ruleset.json is the repo ruleset for main (docs/release.md, "How a pull request merges"): a merge
queue and the status checks a merge group must pass. A required check no workflow reports on a merge group holds every
group until the queue's timeout and then drops it, so a renamed or added lint job must move the ruleset with it, and
the workflow must trigger on merge_group. Checked:

  1. the ruleset targets refs/heads/main, has no bypass actor, a merge_queue rule (SQUASH, ALLGREEN, a group size and a
     check timeout) and a required_status_checks rule whose checks all come from GitHub Actions (integration 15368);
  2. lint.yaml triggers on merge_group (checks_requested);
  3. an aggregate job (AGGREGATES) needs exactly the jobs it stands for, runs `if: always()` and requires each one's
     result to be `success`; those jobs are covered by it and not required one by one;
  4. the required contexts are exactly the check names lint.yaml's jobs report: a job's `name` or id, `id (value)` for
     each value of a one-axis matrix, `id / <job>` for a job that calls a reusable workflow; minus the jobs listed in
     NOT_REQUIRED with the reason.

Fails when PyYAML is missing or a file is unreadable.
"""
import json
import os
import sys

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")
    sys.exit(2)

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
ACTIONS = 15368  # the GitHub Actions app, as every check on this repo's pull requests reports it
NOT_REQUIRED = {
    "lint": "informational by design: it surfaces helm template and lint warnings and never fails",
}
# Jobs a required aggregate job stands for: the aggregate is required, the jobs it needs are not listed one by one (a matrix
# job's check names change with the charts). Each aggregate must `needs` exactly these, run `if: always()`, and read each
# needed job's result and require `success`, so a failed, cancelled or skipped job fails it.
AGGREGATES = {
    "chart-defaults-ok": ("chart-defaults-matrix", "chart-defaults"),
}
# A job that calls a reusable workflow reports `<caller id> / <inner job name>`; the inner name is the reusable's,
# read from a pull request's checks (2026-11-18).
REUSABLE_INNER = {
    "lint-docs": "lint-docs",
    # krateo-blueprints/charts pinned-uses.yaml's one job, id `pinned-uses` with no `name`. Read from the checks of
    # this repository's #376 run 44266587182 and the charts#20 rollout PRs: `pinned-uses / pinned-uses` (2026-11-18).
    # Required: it is how charts PUBLISHING.md rule 9 is enforced, and it runs on merge groups like every lint job.
    "pinned-uses": "pinned-uses",
}


def expected(wf):
    out, problems = set(), []
    covered = {n for needs in AGGREGATES.values() for n in needs}
    jobs = wf.get("jobs") or {}
    for agg, needs in AGGREGATES.items():
        job = jobs.get(agg)
        if job is None:
            problems.append("lint.yaml has no aggregate job %s" % agg)
            continue
        got = job.get("needs")
        got = [got] if isinstance(got, str) else list(got or [])
        if sorted(got) != sorted(needs):
            problems.append("aggregate %s needs %s; it must need exactly %s" % (agg, sorted(got), sorted(needs)))
        if str(job.get("if", "")).replace("${{", "").replace("}}", "").strip() != "always()":
            problems.append("aggregate %s must run `if: always()`, or a failed needed job skips it and a skipped required "
                            "check passes the queue" % agg)
        text = "\n".join(str(s.get("run", "")) + " " + " ".join(str(x) for x in (s.get("env") or {}).values())
                         for s in job.get("steps") or [])
        for n in needs:
            if "needs.%s.result" % n not in text:
                problems.append("aggregate %s does not read needs.%s.result" % (agg, n))
        runs = "\n".join(str(s.get("run", "")) for s in job.get("steps") or [])
        if runs.count("= success") < len(needs):
            problems.append("aggregate %s must compare each of its %d needed jobs' result with `= success` in its run "
                            "script, found %d" % (agg, len(needs), runs.count("= success")))
        stray = sorted(j for j in jobs if j.startswith(agg.rsplit("-ok", 1)[0]) and j != agg and j not in needs)
        if stray:
            problems.append("jobs %s share the prefix of aggregate %s but it does not need them" % (stray, agg))
    for jid, job in jobs.items():
        if jid in NOT_REQUIRED or jid in covered:
            continue
        if "uses" in job:
            if jid not in REUSABLE_INNER:
                problems.append("lint.yaml job %s calls a reusable workflow whose check name is not in REUSABLE_INNER"
                                % jid)
                continue
            out.add("%s / %s" % (jid, REUSABLE_INNER[jid]))
            continue
        name = job.get("name", jid)
        matrix = ((job.get("strategy") or {}).get("matrix")) or {}
        axes = {k: v for k, v in matrix.items() if k not in ("include", "exclude")}
        if not axes:
            out.add(name)
        elif len(axes) == 1:
            for v in list(axes.values())[0]:
                out.add("%s (%s)" % (name, v))
        else:
            problems.append("lint.yaml job %s has a %d-axis matrix; teach this gate its check names" % (jid, len(axes)))
    return out, problems


def main():
    failures = []
    rs = json.load(open(os.path.join(ROOT, "hack", "merge-queue-ruleset.json")))
    wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "lint.yaml")))
    if rs.get("target") != "branch" or rs.get("enforcement") != "active":
        failures.append("the ruleset must target a branch and be active")
    if (rs.get("conditions") or {}).get("ref_name", {}).get("include") != ["refs/heads/main"]:
        failures.append("the ruleset must include refs/heads/main alone")
    if rs.get("bypass_actors"):
        failures.append("the ruleset has bypass actors %s; nothing may skip the queue" % rs["bypass_actors"])
    rules = {r.get("type"): r.get("parameters") or {} for r in rs.get("rules") or []}
    mq = rules.get("merge_queue")
    if mq is None:
        failures.append("no merge_queue rule")
    else:
        if mq.get("merge_method") != "SQUASH" or mq.get("grouping_strategy") != "ALLGREEN":
            failures.append("merge_queue must squash and group ALLGREEN, got %s/%s" % (mq.get("merge_method"),
                                                                                     mq.get("grouping_strategy")))
        for k in ("max_entries_to_build", "max_entries_to_merge", "min_entries_to_merge",
                  "check_response_timeout_minutes"):
            if not isinstance(mq.get(k), int) or mq[k] < 1:
                failures.append("merge_queue %s is %r; want a positive integer" % (k, mq.get(k)))
    rsc = rules.get("required_status_checks")
    contexts = []
    if rsc is None:
        failures.append("no required_status_checks rule")
    else:
        for c in rsc.get("required_status_checks") or []:
            contexts.append(c.get("context"))
            if c.get("integration_id") != ACTIONS:
                failures.append("required check %r is not pinned to GitHub Actions (%d)" % (c.get("context"), ACTIONS))
    if len(contexts) != len(set(contexts)):
        failures.append("a required check is listed twice: %s" % sorted(contexts))
    on = wf.get("on") or wf.get(True) or {}
    types = ((on.get("merge_group") or {}).get("types")) if isinstance(on.get("merge_group"), dict) else None
    if "merge_group" not in on or (types is not None and "checks_requested" not in types):
        failures.append("lint.yaml does not trigger on merge_group (checks_requested), so no required check is ever "
                        "reported on a merge group")
    want, problems = expected(wf)
    failures += problems
    missing, extra = sorted(want - set(contexts)), sorted(set(contexts) - want)
    if missing:
        failures.append("lint.yaml jobs not required by the ruleset: %s (require them, or list them in NOT_REQUIRED "
                        "with the reason)" % missing)
    if extra:
        failures.append("required checks no lint.yaml job reports: %s (a merge group would wait for them until the "
                        "timeout)" % extra)
    if not contexts:
        failures.append("the ruleset requires no check; nothing was compared")
    if failures:
        for f in failures:
            print("FAIL: " + f)
        return 1
    print("OK: the merge-queue ruleset requires exactly lint.yaml's %d gating checks (%s not required: %s), and lint.yaml"
          " runs on merge groups" % (len(contexts), ", ".join(NOT_REQUIRED), "; ".join(NOT_REQUIRED.values())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
