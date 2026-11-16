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
