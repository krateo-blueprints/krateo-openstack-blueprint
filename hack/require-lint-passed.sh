#!/usr/bin/env bash
# Refuse to publish a commit no gate ever ran against. Called by release-tag.yaml and by
# prerelease.yaml before anything is pushed. Needs GH_TOKEN with actions:read on REPO.
#
# On 2026-11-02 a pull request sat with ZERO check runs. Not pending -- never created. The
# branch existed at the right sha, CI was running normally on sibling branches minutes
# earlier, and closing and reopening the PR did not trigger it either; the run had to be
# dispatched by hand. Nothing distinguished that state from "the checks have not finished",
# which is the whole problem: a quiet check list reads as success.
#
# That is the same defect class as the four gates which printed "SKIP: PyYAML unavailable"
# and exited 0 for weeks (#120) -- a check that cannot fail, reported as a check that
# passed. The remedy is the same: make the empty case loud. A release is the last point at
# which it can be made loud before the charts are published.
#
# This asserts only what it can actually see: that THIS commit has a concluded, successful
# run of the lint workflow. It does not try to prove every merged pull request had one --
# a squash merge creates a new commit whose history does not record what ran against the
# branch, so that claim is not available here and is not made.
#
# Moved here verbatim from release-tag.yaml (2026-11-17).
#
# Usage: hack/require-lint-passed.sh OWNER/REPO SHA
set -euo pipefail
repo="${1:-}"; sha="${2:-}"
[ -n "$repo" ] && [ -n "$sha" ] || { echo "::error::usage: $0 OWNER/REPO SHA" >&2; exit 1; }
runs=$(gh api \
  "repos/${repo}/actions/workflows/lint.yaml/runs?head_sha=${sha}&per_page=100" \
  --jq '[.workflow_runs[] | select(.status == "completed")] | length')
ok=$(gh api \
  "repos/${repo}/actions/workflows/lint.yaml/runs?head_sha=${sha}&per_page=100" \
  --jq '[.workflow_runs[] | select(.conclusion == "success")] | length')
echo "lint runs on ${sha}: ${runs} completed, ${ok} successful"
if [ "${runs}" -eq 0 ]; then
  echo "::error::No lint run exists for ${sha}. This is the never-triggered case, not"
  echo "::error::a pending one -- a release built here would ship charts no gate saw."
  echo "::error::Dispatch lint.yaml on this commit, confirm it passes, then publish again."
  exit 1
fi
if [ "${ok}" -eq 0 ]; then
  echo "::error::lint ran on ${sha} but never succeeded. Refusing to publish."
  exit 1
fi
