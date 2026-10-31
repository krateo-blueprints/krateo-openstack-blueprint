#!/usr/bin/env bash
#
# Pre-load every image the OpenStack identity-plane blueprints use into a kind
# cluster as linux/amd64.
#
# Why: the OpenStack-Helm images are amd64-only, and several are multi-arch
# manifest LISTS that contain only amd64, which an arm64 kind node (Apple Silicon)
# refuses at pull time. Pulling them explicitly as linux/amd64 and `kind load`-ing
# them sidesteps the platform check; the charts pull with IfNotPresent so the
# pre-loaded image is used and runs under Rosetta/qemu emulation. On a native
# amd64 cluster (e.g. GKE) this script is unnecessary.
#
# Usage: tools/kind-load-images.sh [kind-cluster-name]   (default: openstack)
set -euo pipefail
CLUSTER="${1:-openstack}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

# Identity-plane blueprints are the ones that run on kind (compute needs KVM/OVS).
#
# Overridable, because the umbrella already supports installing a subset: `enabled: [keystone]`
# expands to its dependency closure (keystone, mariadb, memcached) and never renders glance or
# horizon. Pre-loading those two anyway costs roughly 4GB of node disk for images the install
# will not reference. On a host sharing one Docker volume with other clusters that is the
# difference between a load that finishes and one that stops partway, which presents as an
# install failure rather than a capacity problem -- the loader is where the disk goes, so it is
# where the scope belongs. Default is unchanged, so every existing invocation behaves as before.
#
#   COMPONENTS="mariadb memcached keystone" tools/kind-load-images.sh openstack
COMPONENTS="${COMPONENTS:-mariadb memcached keystone glance horizon}"

# Collect into a file rather than a pipeline, so a render failure can abort the run.
# Previously this piped straight into `sort -u`, which made the exit status sort's and
# left a failed render indistinguishable from a chart with no images.
# A digest reference cannot survive this path, so refuse it loudly rather than produce a node
# that looks loaded and is not. `docker pull --platform` selects one platform and `docker save`
# re-serialises it, so the registry's multi-arch index digest -- the thing a `@sha256:` pin
# names -- no longer exists by the time the tarball is written. The image imports unnamed
# (`docker.io/library/import-<date>`, RepoTags null) and containerd, which resolves
# `repo@sha256:X` by the content's manifest digest, cannot match the pin. The pod then tries to
# pull, which is the exact failure the pre-load exists to prevent on arm64. Renaming the import
# afterwards does not help: `ctr images tag` succeeds and `crictl inspecti` still fails.
# See issue #72. Images that are side-loaded must be pinned to an immutable TAG, not a digest.
refuse_digests() {
  local f="$1" bad
  bad="$(grep -E "@sha256:" "$f" || true)"
  if [ -n "$bad" ]; then
    echo "FATAL: a side-loaded image is pinned by digest, which cannot work here (issue #72):" >&2
    echo "$bad" | sed "s/^/    /" >&2
    echo "  Pin it to an immutable tag instead -- Airship publishes per-commit tags, e.g." >&2
    echo "  quay.io/airshipit/kubernetes-entrypoint:<commit-sha>-ubuntu_noble" >&2
    exit 1
  fi
}

collect() {
  local out="$1"
  : > "$out"
  for c in $COMPONENTS; do
    local d; d="$(mktemp -d)"; cp -R "$HERE/blueprints/$c/chart" "$d/chart"
    sed -i.bak 's/CHART_VERSION/0.0.0-load/' "$d/chart/Chart.yaml"
    # helm-toolkit is vendored once at the repo root and referenced by a relative path that
    # does not survive the copy into $d. Bring it along: the copy exists to stamp
    # CHART_VERSION without mutating the tree, not to exercise dependency resolution.
    mkdir -p "$d/chart/charts"
    cp -R "$HERE/charts/helm-toolkit" "$d/chart/charts/helm-toolkit"
    if ! helm template load "$d/chart" >"$d/rendered" 2>"$d/err"; then
      echo "FATAL: helm template failed for '$c'; cannot determine which images to pre-load." >&2
      sed 's/^/    /' "$d/err" >&2
      rm -rf "$d"
      return 1
    fi
    # Three anchored substitutions rather than one regex using \s: BSD sed (macOS, which is the
    # ONLY platform this script exists for) does not treat \s as whitespace, so the original left
    # a leading space and, for quoted values, a trailing quote. `docker pull` then fails with
    # "invalid reference format". POSIX classes and anchors behave identically on BSD and GNU.
    grep -oE 'image: .*' "$d/rendered" \
      | sed -e 's/^image:[[:space:]]*//' -e 's/^"//' -e 's/"$//' >> "$out"
    rm -rf "$d"
  done
  sort -u -o "$out" "$out"
  # An empty list means the charts rendered but reference no images, which never happens
  # for these five. Treat it as a failure rather than pre-loading nothing and reporting success.
  if [ ! -s "$out" ]; then
    echo "FATAL: no images collected from: $COMPONENTS" >&2
    return 1
  fi
}

LIST="$(mktemp)"; trap 'rm -f "$LIST"' EXIT
collect "$LIST" || exit 1
refuse_digests "$LIST"
IMAGES="$(cat "$LIST")"
echo ">> Pre-loading $(wc -l < "$LIST" | tr -d ' ') images into kind cluster '$CLUSTER' as amd64:"
for img in $IMAGES; do
  echo "   $img"
  docker pull --platform=linux/amd64 "$img"
  kind load docker-image "$img" --name "$CLUSTER"
done
echo ">> Done."
