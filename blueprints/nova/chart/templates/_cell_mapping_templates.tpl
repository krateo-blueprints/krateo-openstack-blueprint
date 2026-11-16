{{/*
Krateo blueprint addition (#228): the URLs db-sync writes into nova_api.cell_mappings, as TEMPLATES.

A cell mapping stores a database URL and a transport URL, and nova caches each cell's connections per process, keyed
by the cell's UUID, until the process exits (nova/context.py CELL_CACHE; cleared only by reset_globals()). With
literal URLs the mappings held the MariaDB and RabbitMQ passwords, and a pod that started before db-sync's update_cell
cached the old ones for its whole life: refused after every password change. nova formats a template from each
process's own config instead (CellMapping._format_url, nova/objects/cell_mapping.py, stable/2026.1): {scheme},
{username}, {password}, {hostname}, {port}, {query}, {fragment} and {path} (the database without its leading "/")
come from that process's [database] connection (database_connection) or [DEFAULT] transport_url (transport_url).
So the mappings hold no password, and every process uses the password its own config holds.

  cell1     the database [database] connection names itself: {path};
  cell0     the same server and user, the cell0 database (endpoints.oslo_db_cell0.path, measured: nova_cell0);
  transport the RabbitMQ URL [DEFAULT] transport_url names.

The TLS query string is kept as {query} where configmap-etc.yaml appends it (manifests.certificates). cell0 is
formatted from [database] connection, so it must be on the same server, port, scheme and user as the main database:
anything else is refused here rather than mapped to the wrong server.
*/}}

{{- define "nova.cell_mapping_db_template" -}}
{{- $db := index . 0 -}}{{- $envAll := index . 1 -}}
{{- printf "{scheme}://{username}:{password}@{hostname}:{port}/%s%s" $db (ternary "?{query}" "" (not (empty $envAll.Values.manifests.certificates))) -}}
{{- end -}}

{{- define "nova.cell_mapping_templates" -}}
{{- $main := tuple "oslo_db" "internal" "nova" "mysql" . | include "helm-toolkit.endpoints.authenticated_endpoint_uri_lookup" -}}
{{- $cell0 := tuple "oslo_db_cell0" "internal" "nova" "mysql" . | include "helm-toolkit.endpoints.authenticated_endpoint_uri_lookup" -}}
{{- $cell0Db := .Values.endpoints.oslo_db_cell0.path | trimPrefix "/" -}}
{{- if ne (regexReplaceAll "/[^/]*$" $main "") (regexReplaceAll "/[^/]*$" $cell0 "") }}
{{- fail "endpoints.oslo_db_cell0 names another server, port, scheme, user or password than endpoints.oslo_db: nova's cell0 mapping is a template formatted from [database] connection, so cell0 must be on the same server as the same MariaDB user (#228)" }}
{{- end }}
{{- dict "cell1" (tuple "{path}" . | include "nova.cell_mapping_db_template") "cell0" (tuple $cell0Db . | include "nova.cell_mapping_db_template") "transport" "{scheme}://{username}:{password}@{hostname}:{port}/{path}" | toJson -}}
{{- end -}}
