---
type: Log
title: The render-log bot acts as a GitHub App; no skip markers
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The render-log bot acts as a GitHub App; no skip markers.** The first bot render landed (d1cb62b) with two
  defects, measured 2026-11-18:
  - **Skipped workflows.** #347's squash message quoted the bot's skip marker, so GitHub skipped every push workflow
    on main for that commit, lint included.
  - **Unlinted bot commits.** The bot merged with `GITHUB_TOKEN`, and GitHub starts no workflow for a `GITHUB_TOKEN`
    event, so its render commits on main got no lint run. prerelease and release-tag require a green lint on the
    exact commit.

  Diego chose a GitHub App:
  - `render-log.yaml` mints the App's token with `actions/create-github-app-token`, pinned to a commit. It refuses
    without `RENDER_LOG_APP_CLIENT_ID` and `RENDER_LOG_APP_PRIVATE_KEY`, and gives `GITHUB_TOKEN` no write
    permission.
  - `hack/render-log-pr.sh` refuses an App whose slug isn't `hack/render-log-app.yaml`'s, commits as
    `<slug>[bot]`, and drops the skip marker. The loop guard is the paths filter alone: the bot's commit changes
    `docs/log.md`, which the filter leaves out.
  - `hack/validate-log-untouched.py` now exempts only `krateo-openstack-render-log[bot]` on a `bot/render-log-*`
    branch. The retired `github-actions[bot]` login is refused.
  - New gate `hack/validate-no-skip-markers.py` refuses any skip marker GitHub honours in a pull request's title,
    body or commit messages. That covers the bracketed "skip ci" family and the `skip-checks: true` trailer, in any
    case, quoted or not.
  - `hack/validate-render-log-pr.py` adds two cases. One reads the workflow: the paths leave `docs/log.md` out, the
    action is pinned, the job refuses without the App's secrets, and nothing has write permission. The other checks
    that another App's token is refused.
  - [release.md](../release.md) lists the App's permissions and the steps to create it.
