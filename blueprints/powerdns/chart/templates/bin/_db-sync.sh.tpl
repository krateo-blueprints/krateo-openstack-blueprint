#!/bin/bash

{{/*
Apply PowerDNS's gmysql schema, idempotently.

THE SCHEMA IS NOT TRANSCRIBED INTO THIS CHART. An init container running the powerdns image
copies /usr/local/share/doc/pdns/schema.mysql.sql into a shared emptyDir, and this script --
running the mariadb image, the only one of the two with a client -- applies it. So the DDL and
the server that will read the database always come from the same build, and an image that
moves or renames the file fails loudly in the init container rather than silently leaving this
job to apply a stale copy the chart had been carrying.

TWO MEASURED FACTS THIS SCRIPT DEPENDS ON, both the opposite of what I first assumed:

  * The client binary is `mariadb`. There is NO `mysql` binary in that image at all. A script
    calling `mysql` fails with command-not-found at run time -- the same shape as #147/#158,
    where a start script named a file nobody delivered.

  * PowerDNS's schema is NOT idempotent. All 7 CREATE TABLE statements are bare; `IF NOT
    EXISTS` appears zero times in the file. Re-applying it to a populated database fails with
    "table already exists". That matters because this job is not a one-shot: this repo's charts
    are hook-free by gate, so db-sync runs on every release of the chart, including upgrades.
    OpenStack's own db-sync jobs get away with re-running because alembic is idempotent; this
    one has to check for itself, which is what the SHOW TABLES guard below is.
*/}}

set -eu

SCHEMA=/tmp/pdns-schema/schema.mysql.sql

mysql_do() {
  mariadb \
    --host="${DB_HOST}" \
    --port="${DB_PORT}" \
    --user="${DB_USER}" \
    --password="${DB_PASSWORD}" \
    --database="${DB_NAME}" \
    --batch --skip-column-names \
    "$@"
}

if [ ! -s "${SCHEMA}" ]; then
  echo "FATAL: ${SCHEMA} is missing or empty, so the init container that copies it out of the" >&2
  echo "powerdns image did not produce it. Applying no schema would leave pdns_server to fail" >&2
  echo "later against an empty database, which is a worse failure than this one because it" >&2
  echo "surfaces as a DNS outage rather than as a job that did not finish." >&2
  exit 1
fi

# `domains` is the table pdns_server reads first, so its presence is the thing that decides
# whether this database has been initialised -- not a marker row this chart would have to keep
# in step with the schema it does not own.
EXISTING="$(mysql_do -e "SHOW TABLES LIKE 'domains';" || true)"

if [ -n "${EXISTING}" ]; then
  echo "powerdns schema already present in ${DB_NAME} (table 'domains' exists); nothing to do."
  exit 0
fi

echo "applying powerdns schema to ${DB_NAME} on ${DB_HOST}:${DB_PORT}"
mariadb \
  --host="${DB_HOST}" \
  --port="${DB_PORT}" \
  --user="${DB_USER}" \
  --password="${DB_PASSWORD}" \
  --database="${DB_NAME}" \
  < "${SCHEMA}"

# Assert the apply actually produced the table, rather than trusting the exit code. A partial
# apply that failed between statements would otherwise leave the next run to take the
# "already present" branch above and skip the rest of the schema forever.
CONFIRM="$(mysql_do -e "SHOW TABLES LIKE 'domains';" || true)"
if [ -z "${CONFIRM}" ]; then
  echo "FATAL: the schema was applied without error but table 'domains' does not exist." >&2
  exit 1
fi

echo "powerdns schema applied to ${DB_NAME}"
