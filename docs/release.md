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
krateo-blueprints/charts `publish-chart.yaml`, and that is how they reach the Marketplace. The
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
2. **The `hack/validate-*.py` gates** — each states, in its docstring and in the step comment
   in `lint.yaml`, the defect it was written for. The step list in `lint.yaml` is the source of
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

## Version pinning downstream

Consumers install by explicit `--version`; nothing tracks a mutable tag. The published chart
versions are immutable — the workflow never publishes `latest`.
