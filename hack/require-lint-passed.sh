#!/usr/bin/env bash
# Refuse to publish a commit no gate ever ran against. Called by release-tag.yaml (and by #345's prerelease.yaml)
# before anything is pushed. Needs GH_TOKEN with actions:read on REPO.
#
# On 2026-11-02 a pull request sat with ZERO check runs. Not pending -- never created. The branch existed at the right
# sha, CI was running normally on sibling branches minutes earlier, and closing and reopening the PR did not trigger
# it either; the run had to be dispatched by hand. Nothing distinguished that state from "the checks have not
# finished", which is the whole problem: a quiet check list reads as success. The same defect class as the four gates
# which printed "SKIP: PyYAML unavailable" and exited 0 for weeks (#120). A release is the last point at which the
# empty case can be made loud before the charts are published.
#
# What counts: a concluded lint.yaml run whose head_sha is THIS commit, from an event that ran on the commit as it is
# on main:
#   - merge_group: with the merge queue (docs/release.md), the group's commit is what lands on main, tested before it
#     lands (GITHUB_SHA is the merge group's SHA, GitHub's events reference);
#   - push: the run on main after the commit lands (lint.yaml runs on every push to main);
#   - workflow_dispatch: the recovery when neither ran (the error below says so).
# A pull_request run is for the pull request's head, a different commit; it never matches a commit on main, and is not
# counted even if the API returned one. The head_sha is re-checked on each run, not only passed as a filter.
# It asserts only what it can see: that THIS commit has a successful run. A squash merge's history does not record
# what ran against the branch, so that claim is not made. Extracted from release-tag.yaml (2026-11-18).
#
# Usage: hack/require-lint-passed.sh OWNER/REPO SHA
# hack/validate-require-lint-passed.py runs this against a stand-in gh.
set -euo pipefail
repo="${1:-}"; sha="${2:-}"
[ -n "$repo" ] && [ -n "$sha" ] || { echo "::error::usage: $0 OWNER/REPO SHA" >&2; exit 1; }
filter='[.workflow_runs[] | select(.head_sha == $sha)
         | select(.event == "merge_group" or .event == "push" or .event == "workflow_dispatch")]'
json=$(gh api "repos/${repo}/actions/workflows/lint.yaml/runs?head_sha=${sha}&per_page=100")
runs=$(jq --arg sha "$sha" "$filter | map(select(.status == \"completed\")) | length" <<<"$json")
ok=$(jq --arg sha "$sha" "$filter | map(select(.conclusion == \"success\")) | length" <<<"$json")
how=$(jq -r --arg sha "$sha" "$filter | map(select(.conclusion == \"success\")) | map(.event) | unique | join(\", \")" <<<"$json")
echo "lint runs on ${sha}: ${runs} completed, ${ok} successful${how:+ (${how})}"
if [ "${runs}" -eq 0 ]; then
  echo "::error::No merge_group, push or dispatched lint run has concluded for ${sha}. This is the never-triggered"
  echo "::error::case, not a pending one -- a release built here would ship charts no gate saw."
  echo "::error::Dispatch lint.yaml on this commit, confirm it passes, then publish again."
  exit 1
fi
if [ "${ok}" -eq 0 ]; then
  echo "::error::lint ran on ${sha} but never succeeded. Refusing to publish."
  exit 1
fi
