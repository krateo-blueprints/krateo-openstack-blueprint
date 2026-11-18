#!/usr/bin/env bash
# Stamp, package and push every chart in this repo to GHCR at one version, then keep the blueprint
# tarballs for the caller. The ONE copy of this logic: release-tag.yaml calls it with `release` and
# a tag, prerelease.yaml with `prerelease` and a version derived from a commit, so a pre-release is
# built exactly the way the release on that commit will be, and only the version differs.
#
# Usage: hack/publish-charts.sh release|prerelease VERSION OCI_REPO DIST_DIR
#   OCI_REPO  e.g. oci://ghcr.io/<owner>/charts (the caller must already be logged in)
#   DIST_DIR  receives <name>-VERSION.tgz for every blueprints/*/chart, the same bytes pushed
#
# Stamps the checkout IN PLACE (CHART_VERSION in every CompositionDefinition, Chart.yaml,
# values.yaml and values.schema.json): run it on a throwaway CI checkout, never on a working tree.
#
# Moved here verbatim from release-tag.yaml's "Package and push every chart" step (2026-11-17);
# the comments below are that step's, and still say why each check exists.
set -euo pipefail

CHANNEL="${1:-}"; VERSION="${2:-}"; OCI_REPO="${3:-}"; DIST="${4:-}"
# First, before anything is stamped or pushed: the version must have the shape of its channel.
"$(dirname "$0")/release-version.sh" check "$CHANNEL" "$VERSION"
[ -n "$OCI_REPO" ] && [ -n "$DIST" ] || { echo "::error::usage: $0 release|prerelease VERSION OCI_REPO DIST_DIR" >&2; exit 1; }

# Stamp every standalone CompositionDefinition too. These sit at
# blueprints/<component>/compositiondefinition.yaml (one level ABOVE the chart dir),
# so the per-chart loop below never touches them — which is how all 32 drifted to a
# hardcoded 0.2.0 while the charts published 0.3.x. Left by hand they register a
# version that was never published; core-provider accepts it silently.
cdcount=0
while IFS= read -r -d '' cd; do
  sed -i.bak "s/CHART_VERSION/${VERSION}/g" "$cd" && rm -f "$cd.bak"; cdcount=$((cdcount+1))
done < <(find blueprints -type f -name compositiondefinition.yaml -not -path '*/charts/*' -print0)
echo "stamped ${cdcount} CompositionDefinition(s) -> ${VERSION}"
if [ "$cdcount" -eq 0 ]; then
  echo "::error::no blueprints/*/compositiondefinition.yaml under $(pwd); nothing to publish" >&2
  exit 1
fi

# Ship each stamped CompositionDefinition inside its own chart, at the chart root where
# helm packages it but never renders it. The Marketplace index refuses a chart whose
# archive has no compositiondefinition.yaml, and an index entry is what the Marketplace
# installs from. The copy is the stamped file, so the chart and the release asset agree.
for dir in blueprints/*/chart; do
  cp "$(dirname "$dir")/compositiondefinition.yaml" "$dir/compositiondefinition.yaml"
done

find . -type f -name Chart.yaml -not -path '*/charts/*' -print0 | while IFS= read -r -d '' cf; do
  dir=$(dirname "$cf")
  name=$(awk '/^name:/{print $2; exit}' "$cf")
  echo "::group::${name} (${dir})"
  # Stamp the CHART_VERSION placeholder, and pin the package version to the tag either way.
  sed -i.bak "s/CHART_VERSION/${VERSION}/g" "$cf" && rm -f "$cf.bak"
  # Also stamp values.yaml and values.schema.json. The openstack umbrella references its
  # OWN version there to pull its component charts, and the schema default becomes the
  # generated CRD's default — so a literal left unstamped makes every component 404 while
  # the composition still reports Ready=True. Harmless where the placeholder is absent.
  # `if` rather than `[ -f ] && ...`: under `set -e` a false test would abort the
  # whole release for any chart that simply has no values.schema.json.
  for extra in "$dir/values.yaml" "$dir/values.schema.json"; do
    if [ -f "$extra" ]; then
      sed -i.bak "s/CHART_VERSION/${VERSION}/g" "$extra" && rm -f "$extra.bak"
    fi
  done
  # No `|| true`: the only dependency is the vendored charts/helm-toolkit, so a
  # failure here means the chart is about to be packaged WITHOUT its toolkit and
  # every template would fail at install. --skip-refresh because no remote repo
  # is involved and refreshing them is pure latency.
  # `update`, not `build`: build requires a committed Chart.lock, and a lock over a
  # single file:// path carries no information worth maintaining in 35 copies.
  helm dependency update "$dir" --skip-refresh
  helm package "$dir" --version "${VERSION}" --destination .
  # Assert the archive, not the copy above. helm package exits 0 whether or not the
  # CompositionDefinition is in the chart directory, so a copy that stopped working would
  # give a green release here and a refusal at the Marketplace index after GHCR already
  # held this version. Blueprint charts only; helm-toolkit and the PoC carry none.
  case "$dir" in
    ./blueprints/*/chart)
      members=$(tar tzf "${name}-${VERSION}.tgz")
      for want in "${name}/compositiondefinition.yaml" "${name}/values.schema.json"; do
        if ! grep -qxF "$want" <<< "$members"; then
          echo "::error::${name}-${VERSION}.tgz lacks ${want}; refusing to push"
          exit 1
        fi
      done
      ;;
  esac
  # Retry: this push fails transiently. The 0.3.13 release died on
  #   failed to perform "Tag" on destination: sha256:405b...: not found
  # for one chart after others had pushed fine, and an unchanged re-run succeeded.
  # Without a retry, one registry hiccup leaves the registry holding this version for
  # some components and not others (#76).
  pushed=""
  for attempt in 1 2 3; do
    if helm push "${name}-${VERSION}.tgz" "${OCI_REPO}"; then pushed=yes; break; fi
    echo "::warning::helm push ${name}:${VERSION} failed (attempt ${attempt}/3); retrying"
    sleep $(( attempt * 10 ))
  done
  if [ -z "$pushed" ]; then
    echo "::error::helm push ${name}:${VERSION} failed after 3 attempts"
    exit 1
  fi
  echo "Published ${OCI_REPO}/${name}:${VERSION}"
  echo "::endgroup::"
done

# Every chart must resolve at this version before anything is published that claims
# it does. A partial push is the dangerous state: a CompositionDefinition stamped
# ${VERSION} referencing a chart that never pushed installs nothing while reporting a
# version that looks valid -- the same shape as #50, a reference that resolves in form
# but not in fact.
#
# `helm show chart` rather than curling the registry API, deliberately. An OCI Helm
# chart's manifest is returned only for
# `Accept: application/vnd.oci.image.manifest.v1+json`; ask ghcr without it and every
# chart answers 404. A check written the obvious way would fail every release while
# reporting that nothing had been published -- a guard worse than no guard. helm
# speaks the right protocol by construction.
echo "::group::verify every chart resolves at ${VERSION}"
missing=""
for dir in ./blueprints/*/chart; do
  name=$(basename "$(dirname "$dir")")
  if ! helm show chart "${OCI_REPO}/${name}" --version "${VERSION}" >/dev/null 2>&1; then
    missing="${missing} ${name}"
  fi
done
# The per-cluster capabilities chart is not a blueprint (no CompositionDefinition, not
# in the Marketplace index), so the loop above does not name it. The find loop pushed
# it like every other Chart.yaml; the README installs it from GHCR before any Openstack
# instance, so a release without it would leave the roles and the compute-tier guard
# uninstallable at this version.
if ! helm show chart "${OCI_REPO}/openstack-capabilities" --version "${VERSION}" >/dev/null 2>&1; then
  missing="${missing} openstack-capabilities"
fi
if [ -n "$missing" ]; then
  echo "::error::published incompletely at ${VERSION}; missing:${missing}"
  exit 1
fi
echo "all charts resolve at ${VERSION}"
echo "::endgroup::"

# The same bytes that went to GHCR, for the index. Blueprint charts only: helm-toolkit
# and the proof-of-concept chart are not blueprints and the index gate would refuse them.
mkdir -p "$DIST"
for dir in ./blueprints/*/chart; do
  name=$(awk '/^name:/{print $2; exit}' "$dir/Chart.yaml")
  mv "${name}-${VERSION}.tgz" "$DIST/"
done
echo "kept $(find "$DIST" -name '*.tgz' | wc -l | tr -d ' ') blueprint tarball(s) in ${DIST}"
