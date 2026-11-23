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

# Wait until every check the ruleset requires has been REPORTED on exactly this commit, then until each has passed.
# `gh pr checks --required --watch --fail-fast`, used up to #379, exits non-zero when no check exists yet on a head
# that was pushed a second earlier, and that was read as a failure: run 44280066151 opened #384 at 09:53:05Z and
# gave up on it at 09:53:06Z. So the two states are told apart here:
#   - not reported yet (missing, or queued/in progress): keep polling, up to CHECKS_APPEAR_POLLS for a check to
#     appear at all and CHECKS_DONE_POLLS for every one to finish, then fail saying which and that it timed out;
#   - reported and failed (any conclusion but success, skipped or neutral): fail at once, naming it.
# The required set is the ruleset's own (the branch's required_status_checks rules), not a list kept here; an empty
# set is refused, since a queue that requires nothing would land the pull request unchecked.
CHECKS_POLL_SECONDS=${CHECKS_POLL_SECONDS:-15}
CHECKS_APPEAR_POLLS=${CHECKS_APPEAR_POLLS:-40}   # 10 minutes at 15 s
CHECKS_DONE_POLLS=${CHECKS_DONE_POLLS:-240}      # 60 minutes at 15 s: lint's slowest job takes about 20
wait_for_required_checks() {
  # Every gh call checks its own status: this function runs as `wait_for_required_checks ... || exit 1`, and bash
  # ignores `set -e` inside a function called in a `||` list, so an unchecked failure would read as "nothing yet".
  local sha=$1 required runs polls=0 missing pending failed name state conclusion line
  required=$(gh api "repos/{owner}/{repo}/rules/branches/$BASE_BRANCH" \
    --jq '.[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context') || {
    echo "could not read the $BASE_BRANCH ruleset's required checks; bot pull request #$number is not enqueued" >&2
    return 1
  }
  required=$(printf '%s\n' "$required" | sed '/^$/d' | sort -u)
  if [ -z "$required" ]; then
    echo "the $BASE_BRANCH ruleset requires no status check; refusing to enqueue bot pull request #$number unchecked" >&2
    return 1
  fi
  while :; do
    polls=$((polls + 1))
    runs=$(gh api "repos/{owner}/{repo}/commits/$sha/check-runs?per_page=100" --paginate \
      --jq '.check_runs[] | [.name, .status, (.conclusion // "")] | @tsv') || {
      echo "could not read the check runs of $sha; bot pull request #$number is not enqueued" >&2
      return 1
    }
    missing=() pending=() failed=()
    while IFS= read -r name; do
      line=$(printf '%s\n' "$runs" | awk -F'\t' -v n="$name" '$1 == n' | tail -n1)
      if [ -z "$line" ]; then missing+=("$name"); continue; fi
      state=$(printf '%s' "$line" | cut -f2)
      conclusion=$(printf '%s' "$line" | cut -f3)
      if [ "$state" != "completed" ]; then pending+=("$name"); continue; fi
      case "$conclusion" in
        success|skipped|neutral) ;;
        *) failed+=("$name ($conclusion)") ;;
      esac
    done <<< "$required"
    if [ "${#failed[@]}" -gt 0 ]; then
      echo "bot pull request #$number failed a required check on $sha: ${failed[*]}; it is not enqueued (the next run" \
        "updates it)" >&2
      return 1
    fi
    if [ "${#missing[@]}" -eq 0 ] && [ "${#pending[@]}" -eq 0 ]; then
      echo "every required check passed on $sha"
      return 0
    fi
    if [ "${#missing[@]}" -gt 0 ] && [ "$polls" -ge "$CHECKS_APPEAR_POLLS" ]; then
      echo "required checks never reported on $sha within $CHECKS_APPEAR_POLLS polls of ${CHECKS_POLL_SECONDS}s:" \
        "${missing[*]}; bot pull request #$number is not enqueued (the next run updates it)" >&2
      return 1
    fi
    if [ "$polls" -ge "$CHECKS_DONE_POLLS" ]; then
      echo "required checks still not finished on $sha after $CHECKS_DONE_POLLS polls of ${CHECKS_POLL_SECONDS}s:" \
        "${pending[*]} ${missing[*]}; bot pull request #$number is not enqueued (the next run updates it)" >&2
      return 1
    fi
    sleep "$CHECKS_POLL_SECONDS"
  done
}

# Whether the pull request with node id $1 is in the merge queue, read the moment it is asked. An answer that is not
# true or false (an API error, a renamed field) stops the script: guessing "not queued" is what produced GH006.
in_merge_queue() {
  local answer
  answer=$(gh api graphql -f query='query($id: ID!) { node(id: $id) { ... on PullRequest { isInMergeQueue } } }' \
    -f id="$1" --jq '.data.node.isInMergeQueue')
  case "$answer" in
    true) return 0 ;;
    false) return 1 ;;
    *) echo "could not tell whether pull request $1 is in the merge queue (got '$answer')" >&2; exit 1 ;;
  esac
}

dequeue() {
  gh api graphql -f query='mutation($id: ID!) { dequeuePullRequest(input: {id: $id}) { mergeQueueEntry { id } } }' \
    -f id="$1" >/dev/null
  echo "took bot pull request #$number out of the merge queue to update it; it is enqueued again once its required" \
    "checks pass"
}

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
  # A conflict is expected (both sides changed docs/log.md) and resolved below by taking the fresh render. Any other
  # failure is not, and was swallowed by `|| true` up to #379: from a depth-1 checkout, origin/main's one commit has no
  # parents, so it shares no commit with the bot branch and git refuses ("refusing to merge unrelated histories",
  # run 44281145388). render-log.yaml checks out with fetch-depth 0 for that reason; this stops the script if the merge
  # did not even start, rather than committing a render onto a branch that never got main.
  if ! git "${BOT[@]}" merge --no-commit --no-ff "origin/$BASE_BRANCH"; then
    if ! git rev-parse -q --verify MERGE_HEAD >/dev/null; then
      echo "could not bring $BASE_BRANCH into $branch: git refused the merge (a shallow checkout gives unrelated" \
        "histories; render-log.yaml must check out with fetch-depth: 0)" >&2
      exit 1
    fi
  fi
  cp "$rendered" docs/log.md
  git add docs/log.md
  unmerged=$(git diff --name-only --diff-filter=U)
  if [ -n "$unmerged" ]; then
    echo "bringing $BASE_BRANCH into $branch left conflicts outside docs/log.md: $unmerged" >&2
    exit 1
  fi
  git "${BOT[@]}" commit -q -m "$MSG"
  # GitHub refuses a push to the branch of a pull request in the merge queue (GH006, "protected branch hook
  # declined"). Run 44291452478 found #384 queued by the run before it, pushed, was refused and failed, and the entry
  # it had rendered then waited for the next docs/log.d merge, because the bot's own merge starts no run (the paths
  # filter, the loop guard). So a queued bot pull request is taken out of the queue, updated, and put back once its
  # required checks pass on the new commit (below). It can also enter the queue between the look and the push: a
  # refused push re-reads the state and, if it is queued now, takes the same path once. Anything else fails.
  node=$(gh pr view "$number" --json id --jq .id)
  if in_merge_queue "$node"; then
    dequeue "$node"
  fi
  if ! git push origin "$branch"; then
    if in_merge_queue "$node"; then
      echo "bot pull request #$number entered the merge queue during this run; taking it out and pushing again"
      dequeue "$node"
      git push origin "$branch"
    else
      echo "the push to $branch was refused, and bot pull request #$number is not in the merge queue" >&2
      exit 1
    fi
  fi
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

# With a merge queue on main (docs/release.md, "How a pull request merges"), the bot does not merge: it adds its pull
# request to the queue, which lands it only after its merge group's lint run, like any other. It enqueues with the
# GraphQL mutation enqueuePullRequest: `gh pr merge --auto` (and `gh pr merge` alone, which falls back to it) calls
# enablePullRequestAutoMerge, and auto-merge is disabled on this repository, so it fails with "Auto merge is not
# allowed for this repository" (gh 2.86, 2026-11-18). The queue takes a pull request whose required checks passed, so
# it first waits for them on the pushed commit (wait_for_required_checks, above); a bot pull request taken out of the
# queue to be updated goes back in here, the same way. All of this needs the App token: a merge group enqueued
# with GITHUB_TOKEN would start no merge_group workflow (GitHub's documented GITHUB_TOKEN rule lists no merge_group
# exception). Without a queue it squash-merges at once, as before.
queued=$(gh api "repos/{owner}/{repo}/rules/branches/$BASE_BRANCH" --jq '[.[] | select(.type == "merge_queue")] | length')
if [ "$queued" != "0" ]; then
  wait_for_required_checks "$(git rev-parse HEAD)" || exit 1
  node=$(gh pr view "$number" --json id --jq .id)
  for attempt in 1 2 3 4 5; do
    if gh api graphql -f query='mutation($id: ID!) { enqueuePullRequest(input: {pullRequestId: $id}) { mergeQueueEntry { position } } }' \
      -f id="$node"; then
      echo "bot pull request #$number is in the merge queue"
      exit 0
    fi
    sleep $((attempt * 5))
  done
  echo "could not enqueue bot pull request #$number; the next run updates it" >&2
  exit 1
fi
for attempt in 1 2 3 4 5; do
  if gh pr merge "$number" --squash --delete-branch --subject "$MSG" --body ""; then
    echo "merged bot pull request #$number"
    exit 0
  fi
  sleep $((attempt * 5))
done
echo "could not merge bot pull request #$number; the next run updates it" >&2
exit 1
