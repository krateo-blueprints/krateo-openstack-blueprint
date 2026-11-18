---
type: Log
title: Pre-release channel, and pruning
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Pre-release channel: verify a commit on GKE before its tag exists (#342 follows).** Every `X.Y.Z` tag reaches the
  Marketplace, so `prerelease.yaml` (dispatch, input `ref`) publishes a commit's charts to GHCR at `X.Y.Z-main.g<sha7>`.
  It attaches the stamped CompositionDefinitions, RBAC grants and examples as the run artifact `prerelease-<version>`.
  It cannot write the Marketplace index, a tag or a release: it calls no reusable workflow, uses no secret but
  `GITHUB_TOKEN`, and holds `contents: read`. The index's only writer is krateo-blueprints/charts
  `publish-chart.yaml`, fed from its caller's own run with `CHARTS_PUBLISH_TOKEN`, measured on 2026-11-17 through the
  portal and the marketplace catalog. `release-tag.yaml` and `prerelease.yaml` now run the same scripts
  (`hack/publish-charts.sh`, `hack/stage-release-assets.sh`, `hack/require-lint-passed.sh`). `hack/release-version.sh`
  refuses a version of the wrong shape before anything is stamped. `prune-prereleases.yaml` keeps the newest 5
  pre-releases and never deletes a release. It needs the repository secret `PRERELEASE_PRUNE_TOKEN`, and a manual run
  is a dry run by default. `hack/validate-prerelease-channel.py` gates all of it, with 32 mutations. Also: releases up
  to 0.3.73 carried a stray `compositiondefinition-chart.yaml` asset (cyborg's CD), now gone. The flow is in
  [release.md](../release.md).
