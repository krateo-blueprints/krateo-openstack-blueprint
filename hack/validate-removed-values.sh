#!/usr/bin/env bash
# A value this chart no longer implements is refused, never ignored (0.3.68, contractEmitter).
#
# The umbrella's contract emitter was removed in 0.3.68: the CMP's single emitter of usage and
# health is krateo-platformops/cmp-usage-collector, and both running would double-count usage.
# A removed value that silently does nothing reads as a feature that runs, so each case below
# renders the umbrella and asserts its outcome:
#   absent                                     renders, no CronJob
#   enabled: false (older Compositions)        renders, no CronJob
#   enabled: true                              refused by values.schema.json
#   enabled: true, schema skipped              refused by templates/removed-values.yaml
#   any other contractEmitter key              refused by values.schema.json
# The chart is copied and its CHART_VERSION placeholder stamped, so the checkout is untouched.
# trove's manifests.cron_job_db_purge (removed, #289) is checked the same way: absent and false render with no
# CronJob, true is refused by the schema and, with the schema skipped, by templates/removed-values.yaml.
# A case that cannot render for another reason is a failure, not a pass.
# usage: validate-removed-values.sh <repo>
set -uo pipefail
ROOT=${1:-.}
CHART="$ROOT/blueprints/openstack/chart"
[ -f "$CHART/Chart.yaml" ] || { echo "FAILED MEASUREMENT: no umbrella chart at $CHART"; exit 2; }
command -v helm >/dev/null || { echo "FAIL: helm is required by validate-removed-values.sh and is not installed"; exit 2; }
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
cp -R "$CHART" "$work/chart"
sed 's/CHART_VERSION/0.0.0-ci/g' "$work/chart/Chart.yaml" > "$work/Chart.yaml" && mv "$work/Chart.yaml" "$work/chart/Chart.yaml"

fails=0; n=0
check() { # name, expect (ok|schema|guard), helm args...
  local name=$1 expect=$2; shift 2; n=$((n + 1))
  local out rc got cron
  out=$(helm template t "$work/chart" "$@" 2>&1); rc=$?
  cron=$(grep -c '^kind: CronJob' <<<"$out")
  if [ $rc -eq 0 ]; then got=ok
  elif grep -q "values don't meet the specifications of the schema" <<<"$out" && grep -q contractEmitter <<<"$out"; then got=schema
  elif grep -q "contractEmitter was removed in 0.3.68" <<<"$out"; then got=guard
  else got="other: $(grep -m1 'Error' <<<"$out" | cut -c1-160)"; fi
  if [ "$got" = "$expect" ] && [ "$cron" = 0 ]; then echo "ok    $name -> $got"
  else echo "FAIL  $name: expected $expect with no CronJob, got $got with $cron CronJob(s)"; fails=$((fails + 1)); fi
}
check "absent"                          ok
check "enabled: false"                  ok     --set contractEmitter.enabled=false
check "enabled: true"                   schema --set contractEmitter.enabled=true
check "enabled: true, schema skipped"   guard  --set contractEmitter.enabled=true --skip-schema-validation
check "another key (clickhouse.url)"    schema --set contractEmitter.clickhouse.url=http://x

# trove's manifests.cron_job_db_purge (#289): the CronJob ran `trove-manage db_purge`, which 2026.1 does not have.
TROVE="$ROOT/blueprints/trove/chart"
[ -f "$TROVE/Chart.yaml" ] || { echo "FAILED MEASUREMENT: no trove chart at $TROVE"; exit 2; }
cp -R "$TROVE" "$work/trove"
sed 's/CHART_VERSION/0.0.0-ci/g' "$work/trove/Chart.yaml" > "$work/Chart.yaml" && mv "$work/Chart.yaml" "$work/trove/Chart.yaml"
mkdir -p "$work/trove/charts" && cp -R "$ROOT/charts/helm-toolkit" "$work/trove/charts/helm-toolkit"
check_trove() { # name, expect (ok|schema|guard), helm args...
  local name=$1 expect=$2; shift 2; n=$((n + 1))
  local out rc got cron
  out=$(helm template trove-gate1234 "$work/trove" "$@" 2>&1); rc=$?
  cron=$(grep -c '^kind: CronJob' <<<"$out")
  if [ $rc -eq 0 ]; then got=ok
  elif grep -q "values don't meet the specifications of the schema" <<<"$out" && grep -q cron_job_db_purge <<<"$out"; then got=schema
  elif grep -q "manifests.cron_job_db_purge was removed (#289)" <<<"$out"; then got=guard
  else got="other: $(grep -m1 'Error' <<<"$out" | cut -c1-160)"; fi
  if [ "$got" = "$expect" ] && [ "$cron" = 0 ]; then echo "ok    trove $name -> $got"
  else echo "FAIL  trove $name: expected $expect with no CronJob, got $got with $cron CronJob(s)"; fails=$((fails + 1)); fi
}
check_trove "cron_job_db_purge absent"                 ok
check_trove "cron_job_db_purge: false"                 ok     --set manifests.cron_job_db_purge=false
check_trove "cron_job_db_purge: true"                  schema --set manifests.cron_job_db_purge=true
check_trove "cron_job_db_purge: true, schema skipped"  guard  --set manifests.cron_job_db_purge=true --skip-schema-validation

[ $n -gt 0 ] || { echo "FAILED MEASUREMENT: no case ran"; exit 2; }
[ $fails -eq 0 ] || { echo "FAIL: $fails of $n removed-value case(s) wrong"; exit 1; }
echo "OK: $n removed-value cases: absent and enabled:false render, every other contractEmitter value is refused;" \
  "trove's cron_job_db_purge renders only absent or false"
