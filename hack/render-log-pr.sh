#!/usr/bin/env bash
# Render docs/log.md on main and land it through a bot pull request. Run by .github/workflows/render-log.yaml.
#
#   hack/render-log-pr.sh [repo-root]
#
# Pull requests never touch docs/log.md (hack/validate-log-untouched.py); they add a docs/log.d entry. After a merge
# to main this renders docs/log.md and, only if it changed:
#   - with no bot pull request open: commits it on a new branch bot/render-log-<sha7> as github-actions[bot],
#     pushes it, opens a pull request and squash-merges it at once;
#   - with one open (its last merge failed): brings main into that branch, commits the fresh render there, pushes
#     (a fast-forward, never a force) and merges it, instead of opening a second one.
# main's ruleset requires a pull request and lets nothing bypass it, which is why this is a pull request and not a
# push (Diego, 2026-11-18). Events made with GITHUB_TOKEN start no workflow, the commit says [skip ci], and the
# workflow runs only when docs/log.d or this renderer changes, so a bot merge cannot start another run.
# hack/validate-render-log-pr.py runs this against a local remote and a stand-in gh.
set -euo pipefail
ROOT=${1:-.}
cd "$ROOT"
PREFIX=bot/render-log-
BASE_BRANCH=${BASE_BRANCH:-main}
BOT=(-c "user.name=github-actions[bot]" -c "user.email=41898282+github-actions[bot]@users.noreply.github.com")
MSG="docs(log): render docs/log.md from docs/log.d [skip ci]"

python3 hack/render-log.py .
if git diff --quiet -- docs/log.md; then
  echo "docs/log.md is already the render of docs/log.d; nothing to commit"
  exit 0
fi
rendered=$(mktemp)
trap 'rm -f "$rendered"' EXIT
cp docs/log.md "$rendered"
sha7=$(git rev-parse --short=7 HEAD)

open=$(gh pr list --base "$BASE_BRANCH" --state open --author "app/github-actions" --json number,headRefName \
  --jq "[.[] | select(.headRefName | startswith(\"$PREFIX\"))] | first | if . then \"\(.number) \(.headRefName)\" else \"\" end")

if [ -n "$open" ]; then
  number=${open%% *}
  branch=${open#* }
  echo "bot pull request #$number ($branch) is open; updating it"
  git checkout -- docs/log.md
  git fetch origin "$branch"
  git checkout -B "$branch" "origin/$branch"
  git "${BOT[@]}" merge --no-commit --no-ff "origin/$BASE_BRANCH" || true
  cp "$rendered" docs/log.md
  git add docs/log.md
  unmerged=$(git diff --name-only --diff-filter=U)
  if [ -n "$unmerged" ]; then
    echo "bringing $BASE_BRANCH into $branch left conflicts outside docs/log.md: $unmerged" >&2
    exit 1
  fi
  git "${BOT[@]}" commit -q -m "$MSG"
  git push origin "$branch"
else
  branch="$PREFIX$sha7"
  git checkout -b "$branch"
  git add docs/log.md
  git "${BOT[@]}" commit -q -m "$MSG"
  git push origin "$branch"
  url=$(gh pr create --base "$BASE_BRANCH" --head "$branch" --title "docs(log): render docs/log.md" \
    --body "Rendered by .github/workflows/render-log.yaml from docs/log.d after $sha7. Only docs/log.md changes.")
  number=${url##*/}
fi

for attempt in 1 2 3 4 5; do
  if gh pr merge "$number" --squash --delete-branch --subject "$MSG"; then
    echo "merged bot pull request #$number"
    exit 0
  fi
  sleep $((attempt * 5))
done
echo "could not merge bot pull request #$number; the next run updates it" >&2
exit 1
