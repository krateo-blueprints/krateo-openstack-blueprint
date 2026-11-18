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
  through a pull request of its own, from `bot/render-log-<sha7>` by `github-actions[bot]`, which
  it squash-merges at once. That bot pull request is the only one allowed to change
  `docs/log.md`. main's ruleset requires a pull request with no bypass, which is why the bot
  doesn't push to main. It needs the org and repo setting "Allow GitHub Actions to create and
  approve pull requests".
- **Gate:** add `hack/gates.d/<gate>.yaml` with `name`, `job` (an existing `lint.yaml` job, for
  the tools it installs), `order` and `run` (a block scalar), plus, if needed, `setup`,
  `working-directory` and `workflow-env`. Say in a comment above the keys what defect it
  guards. `hack/run-gates.py` documents the format. CI fails when a `hack/validate-*` script is
  registered in no gate file, or a gate file names a script that doesn't exist
  (`hack/validate-gates-registered.py`). Only a new job, or a gate needing a new `${{ }}`
  expression in its runner step's `env`, touches `lint.yaml`.

## Version pinning downstream

Consumers install by explicit `--version`; nothing tracks a mutable tag. The published chart
versions are immutable — the workflow never publishes `latest`.
