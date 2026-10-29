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
COMPONENTS="mariadb memcached keystone glance horizon"

# Collect into a file rather than a pipeline, so a render failure can abort the run.
# Previously this piped straight into `sort -u`, which made the exit status sort's and
# left a failed render indistinguishable from a chart with no images.
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
IMAGES="$(cat "$LIST")"
echo ">> Pre-loading $(wc -l < "$LIST" | tr -d ' ') images into kind cluster '$CLUSTER' as amd64:"
for img in $IMAGES; do
  echo "   $img"
  docker pull --platform=linux/amd64 "$img"
  kind load docker-image "$img" --name "$CLUSTER"
done
echo ">> Done."
