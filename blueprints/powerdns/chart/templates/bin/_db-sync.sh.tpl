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

set -euo pipefail

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

# The tables the schema declares, read from the schema so this cannot drift from the image.
# Compared whole because no single table can decide: `domains` is the FIRST statement in the
# file, so an apply cut off after it would look finished to a check on that table, and the job
# would go green over a database with no `records` table.
EXPECTED="$(sed -nE 's/^CREATE TABLE ([a-z_]+).*/\1/p' "${SCHEMA}" | sort)"
if [ -z "${EXPECTED}" ]; then
  echo "FATAL: no CREATE TABLE statement found in ${SCHEMA}, so this script cannot tell whether" >&2
  echo "the schema is present. Its format changed; this script must change with it." >&2
  exit 1
fi
EXISTING="$(mysql_do -e "SHOW TABLES;" | sort)"
MISSING="$(comm -23 <(echo "${EXPECTED}") <(echo "${EXISTING}"))"

if [ -z "${MISSING}" ]; then
  echo "powerdns schema already present in ${DB_NAME}; nothing to do."
  exit 0
fi

# Some tables but not all is an interrupted apply. It cannot be resumed, because the statements
# are bare CREATE TABLE and fail on the tables that exist. Stop here, naming what is missing: a
# green job would start pdns_server against a schema with holes in it.
if [ -n "$(comm -12 <(echo "${EXPECTED}") <(echo "${EXISTING}"))" ]; then
  exit 0

  echo "FATAL: ${DB_NAME} holds part of the powerdns schema, which is an interrupted apply." >&2
  echo "Missing tables: $(echo ${MISSING})" >&2
  echo "Inspect the database, then drop the powerdns tables so this job can apply it whole." >&2
  exit 1
fi

echo "applying powerdns schema to ${DB_NAME} on ${DB_HOST}:${DB_PORT}"
mariadb \
  --host="${DB_HOST}" \
  --port="${DB_PORT}" \
  --user="${DB_USER}" \
  --password="${DB_PASSWORD}" \
  --database="${DB_NAME}" \
  < "${SCHEMA}"

# Assert the apply actually produced every table, rather than trusting the exit code.
MISSING="$(comm -23 <(echo "${EXPECTED}") <(mysql_do -e "SHOW TABLES;" | sort))"
if [ -n "${MISSING}" ]; then
  echo "FATAL: the schema was applied without error but these tables do not exist: $(echo ${MISSING})" >&2
  exit 1
fi

echo "powerdns schema applied to ${DB_NAME}"
