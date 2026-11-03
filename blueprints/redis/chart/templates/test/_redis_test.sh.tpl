#!/bin/bash

{{/*
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/}}

{{/*
Upstream's version of this script ran `python /tmp/python-tests.py` against
docker.io/redislabs/redis-py, which publishes exactly one tag -- `latest`. A rebuildable tag
cannot satisfy hack/preflight-refs.py --mutable-only (#69), and there is no other tag to pin,
so the checks are done here with redis-cli instead, which the server image already carries.
The coverage is the same as upstream's: reachability, a client list, and a SET/GET/DBSIZE/DEL
round trip.

Every reply is compared as TEXT. redis-cli exits 0 on an error reply -- measured in
docker.io/library/redis:7.2.17, `redis-cli NOSUCHCOMMAND; echo $?` prints the error and exits
0 -- so `set -e` alone would let this test pass against a redis that is refusing every command
with -LOADING, -MISCONF, -MASTERDOWN or -OOM. That is the same trap the readiness probe in
templates/deployment.yaml exists to avoid, one layer out.
*/}}

set -uo pipefail

CHART_PORT="{{ .Values.network.port }}"
HOST="${REDIS_HOST:-redis}"
PORT="${REDIS_PORT:-${CHART_PORT}}"
DB="${REDIS_DB:-0}"
KEY="helm-test.$(date +%s).$$"
VALUE="it is working"

rc() {
  timeout 10 redis-cli -h "${HOST}" -p "${PORT}" -n "${DB}" "$@" 2>&1
}

fail() {
  echo "FAILED: $*" >&2
  exit 1
}

echo "Start Redis Test against ${HOST}:${PORT} db ${DB}"

reply="$(rc PING)"
[ "${reply}" = "PONG" ] || fail "PING -> ${reply:-no reply at all}"
echo "Successfully connected to database"

reply="$(rc CLIENT LIST)"
[ -n "${reply}" ] || fail "CLIENT LIST returned nothing; this connection is not even listed"
case "${reply}" in
  *addr=*) : ;;
  *) fail "CLIENT LIST -> ${reply}" ;;
esac
echo "Successfully listed database clients"

reply="$(rc SET "${KEY}" "${VALUE}")"
[ "${reply}" = "OK" ] || fail "SET -> ${reply:-no reply at all}"
echo "Successfully SET keyvalue pair"

reply="$(rc GET "${KEY}")"
[ "${reply}" = "${VALUE}" ] || fail "GET -> ${reply:-no reply at all}"
echo "Successfully GET keyvalue pair"

reply="$(rc DBSIZE)"
case "${reply}" in
  ''|*[!0-9]*) fail "DBSIZE -> ${reply:-no reply at all}" ;;
esac
[ "${reply}" -gt 0 ] || fail "DBSIZE is ${reply} with the key just written still present"
echo "Database size is ${reply}"

reply="$(rc DEL "${KEY}")"
[ "${reply}" = "1" ] || fail "DEL -> ${reply:-no reply at all}"
echo "Successfully DELETED keyvalue pair"

echo "Redis Test passed"
