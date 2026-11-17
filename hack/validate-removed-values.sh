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

# mariadb's Swift remote-backup path and ks-user Job (#302): absent or false render with no ks-user Job and no RGW
# Secret; anything else is refused by the schema and, with the schema skipped, by templates/removed-values.yaml.
MARIADB="$ROOT/blueprints/mariadb/chart"
[ -f "$MARIADB/Chart.yaml" ] || { echo "FAILED MEASUREMENT: no mariadb chart at $MARIADB"; exit 2; }
cp -R "$MARIADB" "$work/mariadb"
sed 's/CHART_VERSION/0.0.0-ci/g' "$work/mariadb/Chart.yaml" > "$work/Chart.yaml" && mv "$work/Chart.yaml" "$work/mariadb/Chart.yaml"
mkdir -p "$work/mariadb/charts" && cp -R "$ROOT/charts/helm-toolkit" "$work/mariadb/charts/helm-toolkit"
check_mariadb() { # name, expect (ok|schema|guard), helm args...
  local name=$1 expect=$2; shift 2; n=$((n + 1))
  local out rc got left
  out=$(helm template mariadb-gate1234 "$work/mariadb" "$@" 2>&1); rc=$?
  left=$(grep -cE '^  name: (mariadb-ks-user|mariadb-backup-user|.*-rgw)$|mariadb-backup-user' <<<"$out")
  if [ $rc -eq 0 ]; then got=ok
  elif grep -q "values don't meet the specifications of the schema" <<<"$out" \
    && grep -qE 'job_ks_user|remote_backup' <<<"$out"; then got=schema
  elif grep -q "was removed (#302)" <<<"$out"; then got=guard
  else got="other: $(grep -m1 'Error' <<<"$out" | cut -c1-160)"; fi
  if [ "$got" = "$expect" ] && [ "$left" = 0 ]; then echo "ok    mariadb $name -> $got"
  else echo "FAIL  mariadb $name: expected $expect with no ks-user or backup user, got $got with $left"
    fails=$((fails + 1)); fi
}
BK=(--set manifests.cron_job_mariadb_backup=true --set conf.backup.enabled=true --set manifests.secret_backup_restore=true)
check_mariadb "local backups, nothing remote"            ok     "${BK[@]}"
check_mariadb "job_ks_user: false"                       ok     "${BK[@]}" --set manifests.job_ks_user=false
check_mariadb "remote_backup.enabled: false"             ok     "${BK[@]}" --set conf.backup.remote_backup.enabled=false
check_mariadb "job_ks_user: true"                        schema --set manifests.job_ks_user=true
check_mariadb "job_ks_user: true, schema skipped"        guard  --set manifests.job_ks_user=true --skip-schema-validation
check_mariadb "remote_backup.enabled: true"              schema --set conf.backup.remote_backup.enabled=true
check_mariadb "remote_backup.enabled: true, no schema"   guard  --set conf.backup.remote_backup.enabled=true \
  --skip-schema-validation
check_mariadb "another remote_backup key"                schema --set conf.backup.remote_backup.container_name=x
check_mariadb "another remote_backup key, no schema"     guard  --set conf.backup.remote_backup.container_name=x \
  --skip-schema-validation

[ $n -gt 0 ] || { echo "FAILED MEASUREMENT: no case ran"; exit 2; }
[ $fails -eq 0 ] || { echo "FAIL: $fails of $n removed-value case(s) wrong"; exit 1; }
echo "OK: $n removed-value cases: absent and enabled:false render, every other contractEmitter value is refused;" \
  "mariadb renders local backups only, and its remote-backup values are refused"
