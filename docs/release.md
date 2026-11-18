---
type: Runbook
title: krateo-openstack-blueprint — release
description: How a release ships. Green PRs merge to main, a pre-release of that commit goes to GHCR only and is verified on GKE, and then one plain X.Y.Z tag on the same commit publishes every chart to GHCR and the Marketplace index. Also covers the per-PR lint gate.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack
tags:
  - openstack
  - release
  - prerelease
  - oci
  - ghcr
  - ci
timestamp: 2026-11-18T00:00:00Z
---

# Release

Every `X.Y.Z` tag publishes to the Marketplace index, so anyone can install it. A change that
affects behaviour is therefore verified on GKE **before** its tag exists, from a pre-release of
the commit that will be tagged (Diego, 2026-11-17: "batch on main, GKE-verify, then tag").

## The flow

1. **Merge green PRs to `main`.** Batch them: each release costs a GKE pair.
2. **Publish a pre-release of that `main` commit.** Run Actions, then **prerelease**, then
   *Run workflow*, with `ref` set to the commit (the default, `main`, means the tip of `main`):

   ```console
   $ gh workflow run prerelease.yaml -R krateo-blueprints/krateo-openstack-blueprint -f ref=<sha>
   ```

   It publishes every chart to GHCR as `X.Y.Z-main.g<sha7>`, where `X.Y.Z` is the patch after the
   newest release tag (set `base` to target a minor release instead). The run's summary prints the
   version.
3. **Run the GKE pair against the pre-release:**
   - a **fresh** `full-strict` install of the pre-release;
   - an **upgrade**: a fresh install of the **last tag**, then the upgrade to the pre-release.
4. **If either one fails, fix it on `main`** and go back to step 2 with the new commit. Each fix
   gets its own pre-release, and both installs run again.
5. **Tag `X.Y.Z` on the commit whose pre-release passed both**, not on a later one:

   ```console
   $ git tag X.Y.Z <sha> && git push origin X.Y.Z
   ```

   For the same commit, the charts that tag publishes are the bytes the pair installed. The one
   difference is the version string: in `Chart.yaml`, in each stamped `CHART_VERSION`, and in the
   composition apiVersion derived from it.

   The release refuses a tag whose commit has no `X.Y.Z-main.g<sha7>` pre-release in GHCR (see
   **The release preflight** below). That stops a tag on a commit that skipped steps 2–4 or is not
   on `main`. Whether the GKE pair passed is still your call: no machine record of it exists.
6. **Pin the README:** set `BP_VERSION=X.Y.Z` in its install block, and record what the pair
   installed.

## The pre-release channel

`.github/workflows/prerelease.yaml`, run by `workflow_dispatch` only.

| | release (`release-tag.yaml`) | pre-release (`prerelease.yaml`) |
|---|---|---|
| trigger | push of a tag matching `[0-9]+.[0-9]+.[0-9]+` | `workflow_dispatch`, input `ref` (and optional `base`) |
| version | the tag, plain `X.Y.Z` | `X.Y.Z-main.g<sha7>` for a commit on `main`, `X.Y.Z-dev.g<sha7>` for any other commit |
| charts | `oci://ghcr.io/krateo-blueprints/charts/<name>:X.Y.Z` | the same path, at the pre-release version |
| CompositionDefinitions, RBAC, examples | GitHub release assets, at `/releases/download/X.Y.Z/` | workflow artifact `prerelease-<version>` on the run, with the same file names |
| Marketplace index | yes, via `publish-chart.yaml` | **no**, and it cannot write it (see below) |
| composition apiVersion | `v0-3-74` | `v0-3-74-main-gabc1234` |

Both workflows use the same scripts, so a pre-release is built exactly like the release:

- `hack/release-version.sh` defines the two version shapes and derives the pre-release version.
- `hack/require-lint-passed.sh` refuses a commit that has no successful lint run.
- `hack/publish-charts.sh` stamps, packages, checks, pushes and verifies every chart.
- `hack/stage-release-assets.sh` writes the stamped CompositionDefinitions, the two RBAC grants
  and the stamped example Compositions.

The pre-release suffix is `g` plus seven hex digits (git-describe style), not a bare short sha. A
short sha can be all digits. When it also starts with zero, it is not a valid semver identifier,
and `helm package` would refuse that one commit.

### Why a pre-release never reaches the Marketplace

Measured on 2026-11-17, with these sources:

- The Marketplace reads two things. One is the in-cluster `blueprints-catalog-index` ConfigMap,
  rendered by the `krateo-marketplace-catalog` chart. The other is the live index at
  `https://krateo-blueprints.github.io/charts/blueprints/index.yaml` (krateo-platformops/portal,
  `helm/marketplace-page/templates/_catalog.tpl`: `portal.catalogStep` and
  `portal.catalogLiveStep`).
- The ConfigMap's data comes from krateo-blueprints/marketplace `hack/build-index.py`, which
  downloads `https://krateo-blueprints.github.io/charts/{blueprints,operators}/index.yaml`.
  Both sources are therefore that one gh-pages index.
- Only krateo-blueprints/charts `.github/workflows/publish-chart.yaml` writes that index. It
  is a reusable workflow. It downloads the `.tgz` files from its caller's own run artifact and
  merges them into gh-pages using `CHARTS_PUBLISH_TOKEN`. Nothing reads GHCR, this repository's
  Releases, or another run's artifacts into an index. In this repository, only `release-tag.yaml`
  calls it, in its `publish-index` job.

`prerelease.yaml` is built so that it cannot reach that writer:

- It calls no reusable workflow.
- It references no secret except `GITHUB_TOKEN`, which can write this repository's packages and
  nothing in krateo-blueprints/charts.
- Its permissions are exactly `contents: read` and `packages: write`. That makes a tag or a GitHub
  release impossible as well, which matters for two reasons. First, there is no
  `/releases/download/` URL. Second, there is no tag for the release-notes workflow to take as the
  previous release: it finds that release with `git describe --tags` on the parent commit, so a
  pre-release tag left on an earlier `main` commit would shrink the notes of `X.Y.Z` to the last
  few commits.

`hack/validate-prerelease-channel.py` runs in lint on every PR and checks all of this. It also
checks that no other workflow names `publish-chart.yaml` and that `release-tag.yaml` publishes only
plain `X.Y.Z`. It proves each check can fire by running it against mutated copies of the
workflows and scripts.

The version adds a second guard. Run with no `--version`, helm resolves an OCI chart to the newest
version that is not a pre-release, so it installs a pre-release only when asked for that exact
version (or with `--devel`).

### Pointing the harness at a pre-release

Use the README install block, with two changes:

1. Set `BP_VERSION` to the pre-release version. The `helm upgrade --install openstack-capabilities
   ... --version ${BP_VERSION}` line then pulls the pre-release from GHCR as it is.
2. Take the files from the run's artifact, not from `/releases/download/`, because a pre-release
   has no GitHub release:

   ```console
   $ BP_VERSION=0.3.74-main.g1a2b3c4
   $ RUN=$(gh api "repos/krateo-blueprints/krateo-openstack-blueprint/actions/artifacts?name=prerelease-${BP_VERSION}" \
       --jq '.artifacts[0].workflow_run.id')
   $ BP_ASSETS=$(mktemp -d)
   $ gh run download "$RUN" -R krateo-blueprints/krateo-openstack-blueprint -n "prerelease-${BP_VERSION}" -D "$BP_ASSETS"
   $ cat "$BP_ASSETS/PRERELEASE.txt"
   ```

   In the block, replace every `https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/download/${BP_VERSION}`
   with `${BP_ASSETS}`. `kubectl apply -f` reads a local path the way it reads a URL. The file
   names are the ones the release uses (`hack/stage-release-assets.sh` writes both), including
   the stamped `example-openstack.yaml` that the block dry-runs and then applies.

Downloading an artifact needs a GitHub login, which can be read-only. Artifacts expire after the
repository's retention period (90 days by default). The charts stay in GHCR.

The upgrade run first installs the last tag with the README block as written, `BP_VERSION` set to
that tag. It then follows the README's "Upgrading an existing install" section, with the same two
changes. In practice that means `helm upgrade` of `openstack-capabilities` at the pre-release
version, then `kubectl apply` of the pre-release's `compositiondefinition-openstack.yaml` from
`$BP_ASSETS`.

**Start every upgrade run from a fresh install of the last tag. Never upgrade one pre-release to
another.** The umbrella's `osh.newerVersion` treats any apiVersion that is not a plain `vX-Y-Z`
as older than every release. Suppose the cluster serves both `v0-3-73` and
`v0-3-74-main-g<old>` when the next pre-release lands. The bump-instant fallback in
`osh.servedVersion` then picks `v0-3-73`, which a real release-to-release upgrade never does.
From a fresh last-tag install, only `v0-3-73` is served, so the pre-release upgrade follows the
same path as the release upgrade.

Names have room for the longer version. core-provider names each controller object
`<plural>-v<version with dashes>-controller`. With the longest component, openvswitch, that is
45 of the 63 characters Kubernetes allows. `hack/release-version.sh derive` refuses any version
that would exceed 63.

### Pruning old pre-releases

Each pre-release adds about 39 package versions to GHCR. `.github/workflows/prune-prereleases.yaml`
deletes the old ones. It runs every Monday and keeps the newest 5 pre-releases. The count is taken
across all packages, so each kept pre-release stays installable as a whole. `hack/prune-prereleases.py`
deletes a package version only when it is tagged and every tag has the pre-release shape that
`hack/release-version.sh` defines. A plain `X.Y.Z` never has that shape, so **a release is never
deleted**. The delete function re-checks the shape before each request.
`hack/validate-prerelease-channel.py` gates the workflow (exact permissions, its one secret, the
dry-run default) and the script (the deletable table, the plan, the refusal), with mutations.

You can also run it by hand from Actions. The inputs are `keep` (default 5) and `dry-run`, which is
ticked by default: a manual run prints the plan and changes nothing until you untick it. A
scheduled run deletes.

**One-time setup (an org owner):** create a classic personal access token with `read:packages`
and `delete:packages`. The account must administer the `charts/*` packages of krateo-blueprints.
Save the token as the **repository** secret `PRERELEASE_PRUNE_TOKEN`, so it does not reach every
repository in the org:

```console
$ gh secret set PRERELEASE_PRUNE_TOKEN -R krateo-blueprints/krateo-openstack-blueprint
```

Paste the token at the prompt rather than piping it, so it gets no trailing newline. Without the
secret, the workflow refuses at its first step instead of skipping.

## The release preflight

`release-tag.yaml`'s first job, `preflight`, calls krateo-blueprints/charts
`release-preflight.yaml` (PUBLISHING.md rule 12). It is pinned to the same commit as the
publisher. `package-and-push` declares `needs: preflight`, and `publish-index` needs
`package-and-push`, so a refusal stamps, pushes, indexes and releases nothing.

The reusable workflow runs this repository's `hack/release-preflight X.Y.Z` at the tag's commit,
with no secrets. The hook asks GHCR, anonymously, whether `openstack:X.Y.Z-main.g<sha7 of the
commit>` exists, and refuses when it is missing or when GHCR cannot be asked:

- `prerelease.yaml` publishes a `-main.` version only for a commit on `origin/main` that passed
  lint. A hit therefore proves the tag is on `main`, on a commit that went through the pre-release
  channel at this version.
- It does **not** prove that the GKE pair passed.

The other release-time preconditions are already enforced elsewhere, so the hook does not repeat
them:

- the tag shape: the first step, plus `hack/release-version.sh`;
- lint, including the examples-apiVersion and first-release gates: `hack/require-lint-passed.sh`;
- the stamped CDs: `hack/publish-charts.sh`, `hack/stage-release-assets.sh` and the publisher.

The README pin is not a precondition, because it is set after the tag.

`prune-prereleases.yaml` keeps the newest 5 pre-releases. If the commit's pre-release was pruned
before you tagged, run `prerelease.yaml` on that commit again; it republishes the same version.
Then re-run the release.

`hack/validate-prerelease-channel.py` gates the wiring and the pins:

- `preflight` and `publish-index` must use full 40-hex SHAs, the same commit, each with a
  `# X.Y.Z` comment;
- the preflight gets `${{ github.ref_name }}`;
- the `needs:` chain must be in place.

It also runs the hook against a stubbed GHCR (200, 404, 500, unreachable, a failed token request,
a pre-release-shaped or empty version). Each wiring and hook check has a mutation that must be
caught.

## What a tag ships

`.github/workflows/release-tag.yaml` triggers on a semver tag (`[0-9]+.[0-9]+.[0-9]+`; GitHub
matches the whole tag name, so a tag like `0.3.74-main.g1a2b3c4` does not trigger it). Its first
step refuses anything that is not plain `X.Y.Z`. `hack/publish-charts.sh release` and
`hack/stage-release-assets.sh release` refuse it again before stamping. For each component it
packages `blueprints/<c>/chart` (substituting `CHART_VERSION` → the tag) and pushes it to GHCR as
an OCI Helm artifact:

```
oci://ghcr.io/<owner>/charts/<component>:X.Y.Z
```

The owner/registry is derived from `GITHUB_REPOSITORY_OWNER` at build time
(`oci://ghcr.io/${owner}/charts`), so a `GITHUB_TOKEN` only ever writes its own namespace and
a repository move does not need a code change. For this repo that resolves to
`oci://ghcr.io/krateo-blueprints/charts/<component>`.

The stamped CompositionDefinitions, the two RBAC grants and the stamped examples are uploaded to
the GitHub release of the tag. The `publish-index` job then hands the blueprint tarballs to
krateo-blueprints/charts `publish-chart.yaml` (1.1.0), and that is how they reach the Marketplace.
Since 1.1.0 the publisher refuses any version that is not plain `X.Y.Z` (its rule 13). The
committed CompositionDefinitions keep the `CHART_VERSION` placeholder: nothing on `main` is bumped
after a release.

Up to 0.3.73, every release also carried a stray asset named `compositiondefinition-chart.yaml`.
It held a copy of whichever CompositionDefinition the asset step found last (cyborg's, at 0.3.73),
because that step ran `find` after the package step had copied each CD into its chart directory.
`hack/stage-release-assets.sh` reads `blueprints/<c>/compositiondefinition.yaml` exactly, so
releases after 0.3.73 do not carry it.

## Steps

Follow **The flow** above. After the tag, confirm that the workflow went green and that the
artifacts exist, e.g.:

```console
$ helm show chart oci://ghcr.io/krateo-blueprints/charts/openstack --version X.Y.Z | head -3
$ helm show chart oci://ghcr.io/krateo-blueprints/charts/keystone  --version X.Y.Z | head -3
```

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
2. **Merge when ready** adds it to the queue. Auto-merge is disabled on this repository, so
   `gh pr merge` (with or without `--auto`) fails. From a script, use the GraphQL
   `enqueuePullRequest` mutation with the pull request's node id. The queue builds
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

A release (release-tag.yaml, and prerelease.yaml) needs a successful lint run on the exact
commit it publishes, from that commit's merge group, its push to main, or a dispatch
(`hack/require-lint-passed.sh`). A pull request's run is for another commit and doesn't count.

Under a queue, the render-log bot waits for its pull request's required checks and then enqueues
it with `enqueuePullRequest`, instead of merging. "Required" means the ruleset's own list, read
from the API, and the bot polls the check runs of exactly the commit it pushed. It handles three
states differently:

- A check that hasn't been reported yet: it keeps waiting, for up to 10 minutes for the check to
  appear and 60 minutes for it to finish, and then fails saying it timed out.
- A required check that failed: it stops at once.
- Every required check passed: it enqueues the pull request.

Enqueuing needs the App token from "The render-log GitHub App" below: GitHub's `GITHUB_TOKEN`
rule starts no workflow for an event the token caused, and lists no `merge_group` exception. So a
queue entry made with it would wait out the timeout with no lint run. The App comes first, then
the ruleset.

The bot checks out main with its full history. When a bot pull request is already open, it merges
main into that pull request's branch, and from a depth-1 checkout git refuses that merge as
unrelated histories.

GitHub refuses a push to the branch of a pull request in the merge queue (GH006). If the open bot
pull request is in the queue, the bot first takes it out (`dequeuePullRequest`), then pushes,
waits for the required checks on the new commit, and enqueues it again. This is also what happens
if the pull request entered the queue after the bot looked and before it pushed (#387).

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
- **Upstream divergence:** add a file to `hack/upstream-divergences.d/`, named
  `<component>-<slug>.yaml`, holding one divergence: `component`, `issue`, `reason` and
  `assertions`, with comments above saying why. `hack/validate-upstream-divergences.py` documents
  the keys, and refuses a file holding more than one entry, a duplicate key, a name that doesn't
  start with its component, an assertion already declared in another file, and an empty
  directory. The old single `hack/upstream-divergences.yaml` is gone, and the gate refuses it if
  it comes back.

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
