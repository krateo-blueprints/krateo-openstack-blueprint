---
type: Runbook
title: krateo-openstack-blueprint — release
description: How a release ships — one semver tag packages every blueprints/*/chart and pushes each to GHCR as an OCI Helm artifact; plus the per-PR lint gate (determinism, hook-free, schema-in-sync, emitter conformance).
resource: oci://ghcr.io/krateo-blueprints/charts/openstack
tags: [openstack, release, oci, ghcr, ci]
timestamp: 2026-08-11T00:00:00Z
---

# Release

One plain-semver tag (`X.Y.Z`, **no** `v` prefix) publishes every blueprint chart.

## What a tag ships

`.github/workflows/release-tag.yaml` triggers on a semver tag (`[0-9]+.[0-9]+.[0-9]+`). For
each component it packages `blueprints/<c>/chart` (substituting `CHART_VERSION` → the tag) and
pushes it to GHCR as an OCI Helm artifact:

```
oci://ghcr.io/<owner>/charts/<component>:X.Y.Z
```

The owner/registry is derived from `GITHUB_REPOSITORY_OWNER` at build time
(`oci://ghcr.io/${owner}/charts`), so a `GITHUB_TOKEN` only ever writes its own namespace and
a repository move does not need a code change. For this repo that resolves to
`oci://ghcr.io/krateo-blueprints/charts/<component>`.

## Steps

```console
$ git tag X.Y.Z && git push origin X.Y.Z
```

Then verify the workflow went green and the artifacts exist, e.g.:

```console
$ helm show chart oci://ghcr.io/krateo-blueprints/charts/openstack --version X.Y.Z | head -3
$ helm show chart oci://ghcr.io/krateo-blueprints/charts/keystone  --version X.Y.Z | head -3
```

After the charts are published, bump each `blueprints/<c>/compositiondefinition.yaml`'s
`spec.chart.version` to `X.Y.Z` on `main` (the orchestrator needs no bump: it pulls the
components at its own chart version) —
those are the repo's own registrations and must point at a version that exists.

## PR-time gate

`.github/workflows/lint.yaml` runs on every PR to `main`:

1. **`componentValues` schema in sync** — runs `hack/gen-componentvalues-schema.py` and fails
   if `blueprints/openstack/chart/values.schema.json` changed (the umbrella schema must be
   regenerated from the component schemas and committed).
2. **The `hack/validate-*.py` gates** — each states, in its docstring and in the comment of its
   `hack/gates.d/<gate>.yaml` file, the defect it was written for. Each `lint.yaml` job runs its
   gates through one step, `hack/run-gates.py --job <job>`, and `hack/gates.d` is the source of
   truth; this page does not repeat it. A helm template and lint of every chart also runs, as
   information only: it surfaces warnings and never fails the build. Until 2026-11-16 this item
   claimed a byte-identical determinism render and a hook-free check. Neither had run since the
   2026-08-11 workflow rewrite, and the charts have used Helm hooks by design since then.
3. **Removed values are refused** — `hack/validate-removed-values.sh` renders the orchestrator
   with each removed value (from 0.3.68, `contractEmitter`) and asserts it is refused, not
   ignored, while a Composition that still carries `contractEmitter.enabled: false` keeps
   rendering.
4. **Service-contract SSO deep-link** — `tools/resolve-deeplink.sh --from-chart` resolves the
   chart's `serviceContract.sso.deepLink.urlTemplate` against sample org/tenant/namespace/project
   and fails on any placeholder left unresolved.
5. **`lint-docs`** — the shared Krateo documentation-standard linter (this bundle), added
   under `jobs:` in `lint.yaml`.

## How a pull request merges

main is meant to take changes only through GitHub's merge queue (Diego, 2026-11-18). The queue
needs the repo ruleset in `hack/merge-queue-ruleset.json` applied; until then, pull requests merge
as before.

1. A pull request runs lint on `pull_request`. Its checks must pass before it can join the queue.
2. **Merge when ready** (or `gh pr merge --auto --squash`) adds it to the queue. The queue builds
   a merge group: main plus every pull request queued ahead, plus this one, each squashed. It runs
   lint on that group (`merge_group`), the commit exactly as it will exist on main.
3. When the group's required checks pass, main moves to that commit. Then lint runs again on the
   push to main, under the same SHA. When a group fails, the queue removes the pull request that
   broke it (ALLGREEN grouping) and rebuilds the rest without it. The author doesn't update the
   branch by hand.

The ruleset requires the 12 checks lint.yaml reports on a merge group, and no other.
`hack/validate-merge-queue-ruleset.py` fails when a lint job is renamed or added without the
ruleset following it. The informational `lint` job isn't required. image-scan doesn't gate:
it scans every image, not the changed ones, and gating merges on a newly published CVE would turn
unrelated changes red. The security workflow doesn't gate either. Making it required means adding
`merge_group` to `security.yml` first.

A release (release-tag.yaml, and #345's prerelease.yaml) needs a successful lint run on the exact
commit it publishes, from that commit's merge group, its push to main, or a dispatch
(`hack/require-lint-passed.sh`). A pull request's run is for another commit and doesn't count.

The render-log bot enables auto-merge under a queue instead of merging. That needs the App token
from "The render-log GitHub App" below: GitHub's `GITHUB_TOKEN` rule starts no workflow for an
event the token caused, and lists no `merge_group` exception. So a queue entry made with it would
wait out the timeout with no lint run. The App comes first, then the ruleset.

To apply the ruleset, once the App is in place (an admin, with Diego's OK):

```console
$ gh api -X POST repos/krateo-blueprints/krateo-openstack-blueprint/rulesets \
    --input hack/merge-queue-ruleset.json
```

The org's `require-pull-request` ruleset stays as it is.

## Adding a change: a log.d file and a gates.d file

Branches in flight used to collide on two files every pull request edited in the same place:
the top of `docs/log.md` and the step list in `lint.yaml`. Both are now one file per change.
**Never edit or regenerate `docs/log.md` in a pull request, and never add a check step to
`lint.yaml` by hand.**

- **Log entry:** add `docs/log.d/<yyyy-mm-dd>-<nn>-<slug>.md`, and nothing else: with front
  matter (`type: Log`, `title`, `description`) and then the entry's bullet
  (`- **Title (#issue).** ...`). Write relative links from `docs/log.d/` (`../supply-chain.md`).
  On one day the higher `<nn>` is the later change, and two entries sharing a date and number
  fail (CI checks the pull request merged with current main, so take the next free number when
  an entry merged ahead took yours). **Don't run the renderer or commit
  `docs/log.md`:** a pull request whose diff touches it fails (`hack/validate-log-untouched.py`),
  and CI checks only the entry (`hack/render-log.py . --check`). To preview, run
  `python3 hack/render-log.py .` and discard the result (`git checkout -- docs/log.md`).
  After the merge, `.github/workflows/render-log.yaml` renders `docs/log.md` on main and lands it
  through a pull request of its own, from `bot/render-log-<sha7>` by the render-log App's bot
  account (`krateo-openstack-render-log[bot]`, see below), which it squash-merges at once. That bot
  pull request is the only one allowed to change `docs/log.md`. main's ruleset requires a pull
  request with no bypass, which is why the bot doesn't push to main.
- **No skip marker** in a pull request's title, body or commit messages. That means the bracketed
  "skip ci" family, or a `skip-checks: true` trailer. GitHub honours one anywhere in a commit
  message, even quoted, and skips every workflow for that commit. #347's squash message quoted one,
  and main ran no lint for it (`hack/validate-no-skip-markers.py`). To mention one, write "the skip
  marker".
- **Gate:** add `hack/gates.d/<gate>.yaml` with `name`, `job` (an existing `lint.yaml` job, for
  the tools it installs), `order` and `run` (a block scalar), plus, if needed, `setup`,
  `working-directory` and `workflow-env`. Say in a comment above the keys what defect it
  guards. `hack/run-gates.py` documents the format. CI fails when a `hack/validate-*` script is
  registered in no gate file, or a gate file names a script that doesn't exist
  (`hack/validate-gates-registered.py`). Only a new job, or a gate needing a new `${{ }}`
  expression in its runner step's `env`, touches `lint.yaml`.

## The render-log GitHub App

`.github/workflows/render-log.yaml` acts as a GitHub App, not with `GITHUB_TOKEN`: GitHub starts
no workflow for a `GITHUB_TOKEN` event, so a bot merge made with it gets no lint run on main
(d1cb62b, 2026-11-18), and prerelease and release-tag require a green lint on the exact commit.
App-token events start workflows like any other. The workflow refuses to run without the App's
secrets, and refuses an App whose slug isn't the one in `hack/render-log-app.yaml`, the only bot
`hack/validate-log-untouched.py` lets change `docs/log.md`.

Steps (an org owner, once):

1. Create the App in the krateo-blueprints organization: Settings → Developer settings → GitHub
   Apps → New GitHub App.
   - **Name:** `krateo-openstack-render-log`. The slug must match `hack/render-log-app.yaml`; if the
     name is taken, choose another and change the slug there in the same pull request.
   - **Homepage URL:** this repository's URL.
   - **Webhook:** off (clear "Active").
   - **Repository permissions:** Contents **Read and write**, Pull requests **Read and write**.
     Metadata stays read-only, which GitHub requires. Nothing else, and no organization or account
     permissions.
   - **Where can this GitHub App be installed:** Only on this account.
2. On the App's page, note the **Client ID**, and under Private keys choose **Generate a private
   key**, which downloads a `.pem` file.
3. Install the App: Install App → krateo-blueprints → **Only select repositories** →
   `krateo-openstack-blueprint` only.
4. Add two repository secrets (Settings → Secrets and variables → Actions):
   `RENDER_LOG_APP_CLIENT_ID` (the Client ID) and `RENDER_LOG_APP_PRIVATE_KEY` (the whole `.pem`
   file, header lines included). Then delete the downloaded key.
5. Check: run the render-log workflow by hand (Actions → render-log → Run workflow). With nothing to
   render it ends "already the render"; after the next merge that adds a `docs/log.d` entry, a
   pull request from `krateo-openstack-render-log[bot]` is opened and squash-merged, and lint runs
   on its merge commit.

The token it mints is scoped to this repository with contents and pull-requests write and expires
within the hour. The org setting "Allow GitHub Actions to create and approve pull requests" isn't
needed for this.

## Version pinning downstream

Consumers install by explicit `--version`; nothing tracks a mutable tag. The published chart
versions are immutable — the workflow never publishes `latest`.
