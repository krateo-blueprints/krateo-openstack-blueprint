#!/usr/bin/env bash
# Write the files a reader applies by URL from a release, stamped for VERSION, into OUT_DIR:
#   compositiondefinition-<component>.yaml   every blueprints/*/compositiondefinition.yaml
#   node-reader-rbac.yaml                    copied: no placeholder
#   portal-visibility-rbac.yaml              copied: no placeholder
#   example-<name>.yaml                      every examples/*.yaml, COMPOSITION_API_VERSION stamped
#
# The ONE copy of this logic. release-tag.yaml uploads OUT_DIR to the GitHub release of the tag;
# prerelease.yaml uploads it as a workflow artifact, with the same file names, so the README
# install block applies a pre-release's files the way it applies a release's.
#
# Usage: hack/stage-release-assets.sh release|prerelease VERSION OUT_DIR
#
# Reads the committed files and writes stamped copies; it does not depend on
# hack/publish-charts.sh having stamped the tree first (a stamped file has no placeholder left).
#
# Moved here from release-tag.yaml's "Publish the stamped CompositionDefinitions" step
# (2026-11-17); the comments below are that step's.
set -euo pipefail

CHANNEL="${1:-}"; VERSION="${2:-}"; OUT="${3:-}"
# First, before anything is written: the version must have the shape of its channel.
"$(dirname "$0")/release-version.sh" check "$CHANNEL" "$VERSION"
[ -n "$OUT" ] || { echo "::error::usage: $0 release|prerelease VERSION OUT_DIR" >&2; exit 1; }

# The CompositionDefinitions committed to the repo carry the CHART_VERSION placeholder by
# design -- #50 moved them off hardcoded versions that had drifted to a release never
# published. But the docs tell a reader to `kubectl apply` them, and a placeholder is not
# a version, so the documented path could not work from a clean checkout. The stamped
# copies are the installable artifact, so publish them where the docs can point at them.
#
# blueprints/<component>/compositiondefinition.yaml exactly, never a `find`. Up to 0.3.73 this
# step ran `find blueprints -name compositiondefinition.yaml` AFTER the package step had copied
# each CD into blueprints/<component>/chart/, so it also found those 37 copies, named each
# "compositiondefinition-chart.yaml", and every release from then on carried one stray asset of
# that name holding whichever CD came last (cyborg's, at 0.3.73). Measured 2026-11-17 by
# diffing this script's output for 0.3.73 against the published 0.3.73 assets.
mkdir -p "$OUT"
shopt -s nullglob
cds=0
for cd in blueprints/*/compositiondefinition.yaml; do
  comp=$(basename "$(dirname "$cd")")
  sed "s/CHART_VERSION/${VERSION}/g" "$cd" > "$OUT/compositiondefinition-${comp}.yaml"
  cds=$((cds+1))
done
shopt -u nullglob
echo "prepared ${cds} CompositionDefinition asset(s)"
if [ "$cds" -eq 0 ]; then
  echo "::error::no blueprints/*/compositiondefinition.yaml under $(pwd); nothing to stage" >&2
  exit 1
fi

# The node-read grant, published beside the CompositionDefinition it serves. The
# orchestrator refuses to install components whose pods no node can run, which means
# listing nodes, which the controller's generated role does not permit -- so without
# this applied the check skips itself. It is a separate, administrator-applied asset
# rather than something the chart creates for itself: the chart is able to
# (`escalate`/`bind` are in that role) and a chart widening its own permissions should
# be a person's decision. Carries no placeholder, so it is copied, not stamped.
cp blueprints/openstack/node-reader-rbac.yaml "$OUT/node-reader-rbac.yaml"

# The portal-visibility grant: without it the portal's Blueprints page, which filters
# CompositionDefinitions by `list` permission in their namespace, shows these to nobody
# but cluster administrators. No placeholder, so copied, not stamped.
cp blueprints/openstack/portal-visibility-rbac.yaml "$OUT/portal-visibility-rbac.yaml"

# The example Compositions carry COMPOSITION_API_VERSION for the same reason the CDs
# carry CHART_VERSION: core-provider derives the served group version from the chart
# version (0.3.8 -> v0-3-8, full major.minor.patch), so any literal in the tree is wrong
# for every release but one. These were pinned to v0-2-0 -- a version dead under the
# current org -- so `kubectl apply -f examples/openstack.yaml` could not work at all.
# A pre-release maps the same way: 0.3.74-main.gabc1234 -> v0-3-74-main-gabc1234
# (core-provider chartfs/gvr.go: "v" + version with every "." replaced by "-").
API_VERSION="v${VERSION//./-}"
echo "stamping examples with composition.krateo.io/${API_VERSION}"
for ex in examples/*.yaml; do
  [ -f "$ex" ] || continue
  sed "s/COMPOSITION_API_VERSION/${API_VERSION}/g" "$ex" > "$OUT/example-$(basename "$ex")"
done
if grep -rlE 'COMPOSITION_API_VERSION|CHART_VERSION' "$OUT" >/dev/null 2>&1; then
  echo "placeholder survived stamping in: $(grep -rlE 'COMPOSITION_API_VERSION|CHART_VERSION' "$OUT")" >&2
  exit 1
fi
echo "prepared $(ls "$OUT"/example-* 2>/dev/null | wc -l | tr -d ' ') example asset(s)"
