#!/usr/bin/env bash
# Render docs/log.md on main and land it through a bot pull request. Run by .github/workflows/render-log.yaml.
#
#   hack/render-log-pr.sh [repo-root]
#
# Pull requests never touch docs/log.md (hack/validate-log-untouched.py); they add a docs/log.d entry. After a merge
# to main this renders docs/log.md and, only if it changed:
#   - with no bot pull request open: commits it on a new branch bot/render-log-<sha7> as the render-log App's bot
#     account, pushes it, opens a pull request and squash-merges it at once;
#   - with one open (its last merge failed): brings main into that branch, commits the fresh render there, pushes
#     (a fast-forward, never a force) and merges it, instead of opening a second one.
# main's ruleset requires a pull request and lets nothing bypass it, which is why this is a pull request and not a
# push (Diego, 2026-11-18).
#
# It acts with a GitHub App token (GH_TOKEN), not GITHUB_TOKEN, so its pull request and its merge commit on main
# start workflows like any other: lint runs on them (prerelease and release-tag require a green lint on the exact
# commit). No skip marker: GitHub honours one anywhere in a commit message, and #347's squash message quoting it
# skipped every push workflow on main. The loop guard is render-log.yaml's paths filter: the bot's commit changes
# docs/log.md alone, which the filter leaves out (hack/validate-render-log-pr.py checks both).
#
# Environment: GH_TOKEN (the App token), APP_SLUG (the App's slug, which must be hack/render-log-app.yaml's), and
# optionally BOT_USER_ID (the bot account's numeric id, for its noreply e-mail; looked up when unset).
# hack/validate-render-log-pr.py runs this against a local remote and a stand-in gh.
set -euo pipefail
ROOT=${1:-.}
cd "$ROOT"
PREFIX=bot/render-log-
BASE_BRANCH=${BASE_BRANCH:-main}
MSG="docs(log): render docs/log.md from docs/log.d"

want=$(sed -n 's/^slug: *//p' hack/render-log-app.yaml)
if [ -z "$want" ] || [ "${APP_SLUG:-}" != "$want" ]; then
  echo "the App token belongs to '${APP_SLUG:-}', not '$want' (hack/render-log-app.yaml): refusing, because" \
    "validate-log-untouched exempts only $want[bot]" >&2
  exit 1
fi
bot="$want[bot]"
id=${BOT_USER_ID:-$(gh api "users/$bot" --jq .id)}
BOT=(-c "user.name=$bot" -c "user.email=$id+$bot@users.noreply.github.com")

python3 hack/render-log.py .
if git diff --quiet -- docs/log.md; then
  echo "docs/log.md is already the render of docs/log.d; nothing to commit"
  exit 0
fi
rendered=$(mktemp)
trap 'rm -f "$rendered"' EXIT
cp docs/log.md "$rendered"
sha7=$(git rev-parse --short=7 HEAD)

open=$(gh pr list --base "$BASE_BRANCH" --state open --author "app/$want" --json number,headRefName \
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

# With a merge queue on main (docs/release.md, "How a pull request merges"), the bot does not merge: it enables
# auto-merge, so the pull request joins the queue once its checks pass and lands only after its merge group's lint
# run, like any other. That needs the App token: a merge group enqueued with GITHUB_TOKEN would start no merge_group
# workflow (GitHub's documented GITHUB_TOKEN rule lists no merge_group exception), and the queue would wait out its
# timeout. Without a queue it squash-merges at once, as before.
queued=$(gh api "repos/{owner}/{repo}/rules/branches/$BASE_BRANCH" --jq '[.[] | select(.type == "merge_queue")] | length')
for attempt in 1 2 3 4 5; do
  if [ "$queued" != "0" ]; then
    if gh pr merge "$number" --auto --squash; then
      echo "bot pull request #$number joins the merge queue once its checks pass"
      exit 0
    fi
  elif gh pr merge "$number" --squash --delete-branch --subject "$MSG" --body ""; then
    echo "merged bot pull request #$number"
    exit 0
  fi
  sleep $((attempt * 5))
done
echo "could not merge or enqueue bot pull request #$number; the next run updates it" >&2
exit 1
