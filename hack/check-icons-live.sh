#!/usr/bin/env bash
# Every blueprint chart's icon must resolve, right now, to an image. Run by the release before the
# first GHCR push.
#
# The Marketplace shows `icon:` on every tile, and a published index entry is never rewritten. So a
# dead icon published once stays broken for that version forever. At 0.3.45, 23 of 37 charts
# pointed at an openstack.org directory that had moved: 22 answered 404, and freezer redirected to an
# HTML page with status 200. A status-code check alone would have passed freezer. So this requires,
# after following redirects: HTTP 200, a Content-Type of image/*, and a non-empty body.
#
# A network error is a FAILURE, not a skip. A release is rare and can be re-run, and this is the
# one moment a dead icon costs something. A check that passed whenever it couldn't reach the URL
# would turn "unchecked" into "checked and fine". The offline half, that every icon is on a host
# this organisation controls, is hack/validate-marketplace-metadata.py on every pull request.
#
# helm package copies Chart.yaml's `icon:` into the archive unchanged, and the release never stamps
# it, so the tree's value is the packaged value.
#
# Usage: hack/check-icons-live.sh [repo-root]
set -uo pipefail
ROOT="${1:-.}"
shopt -s nullglob
charts=("$ROOT"/blueprints/*/chart/Chart.yaml)
if [ "${#charts[@]}" -eq 0 ]; then
  echo "::error::no blueprints/*/chart/Chart.yaml under ${ROOT} -- nothing was checked"
  exit 1
fi
fail=0
for cf in "${charts[@]}"; do
  name=$(awk '/^name:/{print $2; exit}' "$cf")
  icon=$(awk '/^icon:/{print $2; exit}' "$cf")
  if [ -z "$icon" ]; then
    echo "::error::${name}: Chart.yaml has no icon"; fail=1; continue
  fi
  tmp=$(mktemp)
  # --retry covers a transient blip; anything still failing after that fails the release.
  meta=$(curl -sS -L --retry 2 --retry-delay 3 --max-time 30 -o "$tmp" \
              -w '%{http_code} %{content_type}' "$icon" 2>&1)
  rc=$?
  size=$(wc -c < "$tmp" | tr -d ' ')
  rm -f "$tmp"
  code=${meta%% *}
  ctype=${meta#* }
  if [ "$rc" -ne 0 ]; then
    echo "::error::${name}: icon ${icon} could not be fetched (curl exit ${rc}: ${meta})"; fail=1
  elif [ "$code" != 200 ]; then
    echo "::error::${name}: icon ${icon} answered HTTP ${code}"; fail=1
  elif [[ "$ctype" != image/* ]]; then
    echo "::error::${name}: icon ${icon} is not an image (Content-Type ${ctype})"; fail=1
  elif [ "$size" -eq 0 ]; then
    echo "::error::${name}: icon ${icon} returned an empty body"; fail=1
  else
    echo "ok ${name}: ${ctype}, ${size} bytes"
  fi
done
if [ "$fail" -ne 0 ]; then
  echo "::error::refusing to publish: a Marketplace tile would show a broken icon"
  exit 1
fi
echo "OK: ${#charts[@]} chart icon(s) resolve to images"
