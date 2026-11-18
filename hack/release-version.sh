#!/usr/bin/env bash
# The two version shapes this repo publishes, and the one place that decides which is which.
#
#   release     X.Y.Z                    a git tag; release-tag.yaml publishes it to GHCR AND to the
#                                        Marketplace index (krateo-blueprints/charts).
#   prerelease  X.Y.Z-main.g<sha7>       a commit on main, published to GHCR only, by prerelease.yaml,
#               X.Y.Z-dev.g<sha7>        for the GKE pair to install before any tag exists. `dev` is a
#                                        commit that is not on main: installable, never taggable.
#
# Why a shape check is a mechanism and not a convention: hack/publish-charts.sh and
# hack/stage-release-assets.sh take the channel as their first argument and call `check` before
# they stamp or push anything, so release-tag.yaml (which passes `release`) cannot stamp a
# pre-release version even if a tag of that shape were pushed, and prerelease.yaml (which passes
# `prerelease`) cannot publish a plain X.Y.Z that would collide with a release.
# hack/validate-prerelease-channel.py checks both callers pass the right channel, and mutates this
# file's patterns to prove the table it runs would catch a loosened one.
#
# The pre-release suffix is `g` + seven hex digits, git-describe style, never a bare short sha: a
# short sha can be all digits, and one with a leading zero (0123abc is not, 0123456 is) is not a
# valid semver identifier, so `helm package` would refuse that commit and no other.
#
# Usage:
#   hack/release-version.sh check release|prerelease VERSION
#   hack/release-version.sh derive COMMIT [BASE]      # needs the tags and origin/main fetched
#
# derive prints BASE-main.g<sha7> (or -dev.), BASE defaulting to the patch after the newest
# release tag. A BASE given must be a plain X.Y.Z newer than every release tag: a pre-release of
# a version that was already released would sort BEFORE it and could never be the commit tagged.
set -euo pipefail

CORE='(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
RELEASE_RE="^${CORE}\$"
PRERELEASE_RE="^${CORE}-(main|dev)\.g[0-9a-f]{7}\$"

die() { echo "::error::$*" >&2; exit 1; }

check() {
  local channel="${1:-}" version="${2:-}"
  case "$channel" in
    release)
      [[ "$version" =~ $RELEASE_RE ]] \
        || die "release version must be plain X.Y.Z, got '${version}'. A pre-release (X.Y.Z-main.g<sha>) is published by prerelease.yaml, never by a tag."
      ;;
    prerelease)
      [[ "$version" =~ $PRERELEASE_RE ]] \
        || die "pre-release version must be X.Y.Z-main.g<7 hex> or X.Y.Z-dev.g<7 hex>, got '${version}'"
      ;;
    *)
      die "channel must be 'release' or 'prerelease', got '${channel}'"
      ;;
  esac
}

vnewer() { # true when $1 sorts strictly after $2 (both plain X.Y.Z)
  [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n1)" = "$1" ]
}

derive() {
  local commit="${1:-}" base="${2:-}" sha last id version
  [ -n "$commit" ] || die "derive needs a commit"
  sha=$(git rev-parse --verify "${commit}^{commit}") || die "'${commit}' is not a commit here"

  # Every plain tag, not only those merged into the commit: the pre-release must precede a version
  # no release has used yet, whichever branch that release was cut from.
  last=$(git tag --list | grep -E "$RELEASE_RE" | sort -V | tail -n1 || true)
  [ -n "$last" ] || die "no release tags visible; check out with fetch-depth: 0 so the tags are fetched"

  if [ -z "$base" ]; then
    IFS=. read -r ma mi pa <<< "$last"
    base="${ma}.${mi}.$((pa + 1))"
  fi
  check release "$base"
  vnewer "$base" "$last" || die "base ${base} is not newer than the newest release tag ${last}"

  # main or not is measured, never assumed: a missing origin/main is a failure, not "dev".
  git rev-parse --verify -q refs/remotes/origin/main >/dev/null \
    || die "origin/main is not fetched, so whether ${sha} is on main cannot be measured"
  if git merge-base --is-ancestor "$sha" refs/remotes/origin/main; then id=main; else id=dev; fi

  version="${base}-${id}.g${sha:0:7}"
  check prerelease "$version"

  # core-provider serves the chart at composition.krateo.io/v<version, dots to dashes> and names
  # the controller objects <plural>-<that>-controller (core-provider deploy.go resourceNamer).
  # Kubernetes caps a label value and a DNS-1035 name at 63. Bound it with the longest component
  # name plus "es", an upper bound for its English plural. Measured 2026-11-17: 45 of 63.
  local longest=0 n bound
  for cd in blueprints/*/compositiondefinition.yaml; do
    n=$(basename "$(dirname "$cd")"); [ "${#n}" -gt "$longest" ] && longest=${#n}
  done
  [ "$longest" -gt 0 ] || die "no blueprints/*/compositiondefinition.yaml under $(pwd); cannot bound the name length"
  local name="${version//./-}" sep="-v" suffix="-controller"
  bound=$(( longest + 2 + ${#sep} + ${#name} + ${#suffix} ))
  [ "$bound" -le 63 ] || die "${version} makes a controller name up to ${bound} characters long; Kubernetes allows 63"

  echo "$version"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  check) check "$@" ;;
  derive) derive "$@" ;;
  *) die "usage: $0 check release|prerelease VERSION | derive COMMIT [BASE]" ;;
esac
