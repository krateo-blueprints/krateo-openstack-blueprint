{{/*
MariaDB's other built-in users (Krateo blueprint addition, issue #228): sst, audit and exporter.
Each one shipped with OpenStack-Helm's published default password:
  sst       PROCESS, RELOAD, LOCK TABLES, REPLICATION CLIENT from 127.0.0.1 (Galera state transfer);
            its password was also written in plain text into the mariadb-etc ConfigMap
            (wsrep_sst_auth in 00-base.cnf);
  audit     SELECT ON *.* from '%': any pod can read every database, Keystone's included;
  exporter  SLAVE MONITOR, PROCESS, REPLICATION CLIENT, SELECT ON *.* from 127.0.0.1, created only
            when monitoring.prometheus.enabled.

The same mechanism as root (_root_credential.tpl). Without endpoints.oslo_db.auth.<user>.passwordFrom:
OpenStack-Helm's Secret from values, under its usual name. With it: an ExternalSecret projecting the
root ESO generated for the install, under a NEW name. start.py re-creates sst and audit on every
start of a server with an existing data directory (CREATE OR REPLACE USER through mysqld
--init-file), and the exporter's create-user Job is an upgrade hook. A new name changes the pod spec
of the StatefulSet (and the Job), so rotating happens exactly at the switch and needs no old
password.

wsrep_sst_auth is no longer in the ConfigMap in either mode. It is a drop-in, 10-sst-auth.cnf, in the
sst Secret, mounted into /etc/mysql/conf.d/ (my.cnf includes the directory).
*/}}

{{- define "mariadb.user_from" -}}
{{- $envAll := index . 0 -}}{{- $userClass := index . 1 -}}
{{- with (index $envAll.Values.endpoints.oslo_db.auth $userClass).passwordFrom }}{{ if .secret }}true{{ end }}{{ end -}}
{{- end -}}

{{- define "mariadb.dbsst_secret_name" -}}
{{- if include "mariadb.user_from" (list . "sst") }}mariadb-dbsst-generated{{ else }}mariadb-dbsst-password{{ end -}}
{{- end -}}

{{- define "mariadb.dbaudit_secret_name" -}}
{{- if include "mariadb.user_from" (list . "audit") }}mariadb-dbaudit-generated{{ else }}mariadb-dbaudit-password{{ end -}}
{{- end -}}

{{- define "mariadb.exporter_secret_name" -}}
{{- if include "mariadb.user_from" (list . "exporter") }}mysql-exporter-generated{{ else }}mysql-exporter-secrets{{ end -}}
{{- end -}}

{{/* The wsrep_sst_auth drop-in, with $password as the password (a value, or an ESO template action). */}}
{{- define "mariadb.sst_auth_cnf" -}}
{{- $envAll := index . 0 -}}{{- $password := index . 1 -}}
[mysqld]
wsrep_sst_auth={{ $envAll.Values.endpoints.oslo_db.auth.sst.username }}:{{ $password }}
{{ end -}}
